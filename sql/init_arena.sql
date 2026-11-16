-- =============================================================================
-- adk-leak-arena (v0.2.0) PostgreSQL + pgvector Arena Schema
-- =============================================================================
-- 1. The adk-memshield 0.2.0 RLS-protected `memories` table and policies (kept in sync with
--    adk-memshield's sql/01_schema.sql; also upgrades a 0.1.0 arena database in place).
-- 2. The baseline `memories_unprotected` table (NO RLS) for side-by-side red-team
--    comparison against application-layer SQL filtering mistakes.
-- Tables are created in the first schema on the search_path, so a run-scoped schema
-- (`options=-csearch_path=arena_run_x,public`) gets its own copies.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Create non-superuser role to enforce Row-Level Security (RLS)
--    (PostgreSQL superusers bypass RLS even with FORCE ROW LEVEL SECURITY)
--    'memshield_pass' is a local development default: rotate it in any real deployment.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'memshield_app') THEN
    CREATE ROLE memshield_app LOGIN PASSWORD 'memshield_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO memshield_app;

-- 2. RLS-Protected Semantic Memory Table
--    Identity columns reject empty strings: once a pooled connection has used
--    set_config(..., true), current_setting(..., true) returns '' (not NULL) after the
--    transaction ends, so an empty-string owner would otherwise match a context-free query.
CREATE TABLE IF NOT EXISTS memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_name TEXT NOT NULL CHECK (app_name <> ''),
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    session_id TEXT,
    author TEXT DEFAULT 'user',
    scope TEXT NOT NULL DEFAULT 'private' CHECK (scope IN ('private', 'tenant_shared')),
    min_clearance INTEGER NOT NULL DEFAULT 1 CHECK (min_clearance BETWEEN 1 AND 5),
    -- Horizontal access on top of clearance. Empty = no compartment restriction.
    compartments TEXT[] NOT NULL DEFAULT '{}' CHECK (array_position(compartments, NULL) IS NULL),
    content TEXT NOT NULL,
    custom_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(384) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Arena databases created by 0.1.0 already have `memories` without `compartments`: add it
-- (and its constraint) before the indexes and policies below reference it.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS compartments TEXT[] NOT NULL DEFAULT '{}';
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memories'::regclass AND conname = 'memories_compartments_check'
  ) THEN
    ALTER TABLE memories
      ADD CONSTRAINT memories_compartments_check CHECK (array_position(compartments, NULL) IS NULL);
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_memories_scope_lookup
    ON memories (app_name, tenant_id, user_id);

-- Idempotent session ingestion: ADK may call add_session_to_memory() repeatedly for
-- the same session, so each (owner, session, event) is stored at most once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_session_event
    ON memories (app_name, tenant_id, user_id, session_id, (custom_metadata->>'event_id'))
    WHERE custom_metadata ? 'event_id';

CREATE INDEX IF NOT EXISTS idx_memories_compartments
    ON memories USING GIN (compartments);

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON memories TO memshield_app;

-- 3. Enable & Force Row-Level Security on `memories`
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;


-- Caller compartments are bound as a comma-separated list in app.agent_compartments.
-- adk-memshield validates names against ^[a-z0-9_-]{1,64}$ before binding, so a comma or
-- brace inside a name can never change how the list is parsed here.

-- Read: own rows (any scope, any compartments), plus tenant-shared rows at or below the
-- agent's clearance that carry no compartments or share one with the caller.
DROP POLICY IF EXISTS memories_isolation_select ON memories;
CREATE POLICY memories_isolation_select ON memories
    FOR SELECT
    TO memshield_app
    USING (
        app_name = current_setting('app.current_app_name', true)
        AND tenant_id = current_setting('app.current_tenant_id', true)
        AND (
            user_id = current_setting('app.current_user_id', true)
            OR (
                scope = 'tenant_shared'
                AND min_clearance <= COALESCE(NULLIF(current_setting('app.agent_clearance', true), '')::int, 1)
                AND (
                    cardinality(compartments) = 0
                    OR compartments && COALESCE(string_to_array(NULLIF(current_setting('app.agent_compartments', true), ''), ','), '{}'::text[])
                )
            )
        )
    );

-- Write: own rows only. A tenant_shared row may not be labelled above the writing
-- agent's clearance ("no write-up"), and no row may carry a compartment the writer does
-- not hold ("no write-across").
DROP POLICY IF EXISTS memories_isolation_insert ON memories;
CREATE POLICY memories_isolation_insert ON memories
    FOR INSERT
    TO memshield_app
    WITH CHECK (
        app_name = current_setting('app.current_app_name', true)
        AND tenant_id = current_setting('app.current_tenant_id', true)
        AND user_id = current_setting('app.current_user_id', true)
        AND (
            scope = 'private'
            OR min_clearance <= COALESCE(NULLIF(current_setting('app.agent_clearance', true), '')::int, 1)
        )
        AND compartments <@ COALESCE(string_to_array(NULLIF(current_setting('app.agent_compartments', true), ''), ','), '{}'::text[])
    );

DROP POLICY IF EXISTS memories_isolation_update ON memories;
CREATE POLICY memories_isolation_update ON memories
    FOR UPDATE
    TO memshield_app
    USING (
        app_name = current_setting('app.current_app_name', true)
        AND tenant_id = current_setting('app.current_tenant_id', true)
        AND user_id = current_setting('app.current_user_id', true)
        -- An agent may only modify a shared row labelled within its own clearance and
        -- compartments, so a low-clearance agent acting for the owner cannot declassify a
        -- row a higher-clearance agent wrote.
        AND (
            scope = 'private'
            OR (
                min_clearance <= COALESCE(NULLIF(current_setting('app.agent_clearance', true), '')::int, 1)
                AND compartments <@ COALESCE(string_to_array(NULLIF(current_setting('app.agent_compartments', true), ''), ','), '{}'::text[])
            )
        )
    )
    WITH CHECK (
        app_name = current_setting('app.current_app_name', true)
        AND tenant_id = current_setting('app.current_tenant_id', true)
        AND user_id = current_setting('app.current_user_id', true)
        AND (
            scope = 'private'
            OR min_clearance <= COALESCE(NULLIF(current_setting('app.agent_clearance', true), '')::int, 1)
        )
        AND compartments <@ COALESCE(string_to_array(NULLIF(current_setting('app.agent_compartments', true), ''), ','), '{}'::text[])
    );

DROP POLICY IF EXISTS memories_isolation_delete ON memories;
CREATE POLICY memories_isolation_delete ON memories
    FOR DELETE
    TO memshield_app
    USING (
        app_name = current_setting('app.current_app_name', true)
        AND tenant_id = current_setting('app.current_tenant_id', true)
        AND user_id = current_setting('app.current_user_id', true)
    );

-- =============================================================================
-- LEFT TARGET: `memories_unprotected` (Standard Shared pgvector Table, NO RLS)
-- =============================================================================
CREATE TABLE IF NOT EXISTS memories_unprotected (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_name TEXT NOT NULL CHECK (app_name <> ''),
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    user_id TEXT NOT NULL CHECK (user_id <> ''),
    session_id TEXT,
    author TEXT DEFAULT 'user',
    scope TEXT NOT NULL DEFAULT 'private' CHECK (scope IN ('private', 'tenant_shared')),
    min_clearance INTEGER NOT NULL DEFAULT 1 CHECK (min_clearance BETWEEN 1 AND 5),
    compartments TEXT[] NOT NULL DEFAULT '{}',
    content TEXT NOT NULL,
    custom_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(384) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE memories_unprotected ADD COLUMN IF NOT EXISTS compartments TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_memories_unprotected_scope_lookup
    ON memories_unprotected (app_name, tenant_id, user_id);

-- Same per-event idempotency as `memories`, so session ingestion semantics match on both sides.
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_unprotected_session_event
    ON memories_unprotected (app_name, tenant_id, user_id, session_id, (custom_metadata->>'event_id'))
    WHERE custom_metadata ? 'event_id';

GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON memories_unprotected TO memshield_app;
