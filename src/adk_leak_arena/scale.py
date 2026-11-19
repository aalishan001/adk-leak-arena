"""Stress-test scaling (synthetic rows) and scenario-scoped fixtures for the arena tables.

Synthetic rows are bulk-loaded with an admin `COPY` because they are test scaffolding, not
the security path under test; every read the arena measures still goes through the
`memshield_app` role and RLS. Fixture rows (Scenarios 6 and 7) are written through each
service's normal write path so the RLS INSERT policy is genuinely exercised.

Sizes up to 2,000 rows use the 0.1.0 generator unchanged (same rows, so 0.1.0 numbers
reproduce). The 10,000 and 100,000 tiers are generated and loaded in chunks (bounded
memory) from a separate seeded stream, then the tables are ANALYZEd. Qdrant targets get
the same rows and vectors; Mem0 embeds every add itself and is limited to the baseline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
import psycopg
from adk_memshield import MemShieldMemoryService
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from .config import ArenaConfig, load_pre_write_hook
from .dataset import BASELINE_SEED_SIZE, SCENARIO_FIXTURES, SeedRecord
from .embedding import get_cached_embed_fn
from .unprotected_service import UnprotectedPgVectorMemoryService

logger = logging.getLogger(__name__)

SEED_SIZE_OPTIONS: tuple[int, ...] = (BASELINE_SEED_SIZE, 250, 500, 1000, 2000, 10_000, 100_000)
LEGACY_MAX_SEED_SIZE = 2000  # sizes up to here use the 0.1.0 generator unchanged
LARGE_SEED_SIZE = 10_000  # from here on the CLI and console show a load-time notice
_CHUNK_ROWS = 5000
TOP_K_OPTIONS: tuple[int, ...] = (1, 3, 5, 10, 20)
TABLES: tuple[str, ...] = ("memories", "memories_unprotected")

_SYNTH_RNG_SEED = 20261023
_SYNTH_NOISE_SIGMA = 0.03  # per-dimension; keeps cosine to the template around 0.85-0.9

_TEMPLATES: tuple[str, ...] = (
    "{team} weekly sync: sprint {n} burndown is on track; retro moved to {day}.",
    "{team} incident review: pager alert storm on {svc} traced to a misconfigured health check.",
    "{team} vendor note: renewal for {vendor} is due in {month}; procurement ticket PRC-{n}.",
    "{team} hiring update: two open requisitions for {role} are interviewing this month.",
    "{team} roadmap draft: Q{q} milestones include the {proj} beta and a pricing experiment.",
    "{team} access review: quarterly entitlement audit for {svc} completed with {n} revocations.",
    "{team} budget note: travel spend is {pct}% under plan; conference budget released.",
    "{team} customer escalation: {cust} reported latency on the reporting API; workaround shared.",
    "{team} on-call handoff: {svc} disk usage at {pct}%; cleanup job scheduled overnight.",
    "{team} design review: proposal to migrate {svc} to managed Postgres was approved.",
    "{team} training reminder: complete the annual security awareness module by {month}.",
    "{team} release notes: version {n}.{q} ships dark mode and CSV export fixes.",
    "{team} data request: analytics needs a {month} cohort extract for churn modeling.",
    "{team} partner update: integration certification with {vendor} passed staging tests.",
    "{team} facilities note: {office} office badge readers were upgraded over the weekend.",
    "{team} OKR check-in: key result {n} at {pct}% with risk flagged on {proj}.",
    "{team} contract note: NDA with {cust} countersigned and archived by legal.",
    "{team} postmortem action: add rate limiting to the {svc} login endpoint by {month}.",
    "{team} capacity plan: the {svc} cluster needs {n} more nodes before peak season.",
    "{team} expense question: are coworking day passes reimbursable in the {office} office?",
    "{team} feature flag cleanup: {n} stale flags removed from {proj}.",
    "{team} customer success: {cust} renewal forecast moved to commit at {pct}% confidence.",
    "{team} security ticket: rotate the staging service account for {svc} (non-production).",
    "{team} meeting notes: planning offsite agenda covers {proj} and hiring.",
    "{team} analytics note: dashboard refresh for {proj} moved to an hourly cadence.",
)
_SLOTS: dict[str, tuple[str, ...]] = {
    "team": ("Platform", "Data", "Payments", "Growth", "SRE", "Support", "Finance", "Legal", "Sales Ops", "Security"),
    "svc": ("billing-api", "search", "auth-gateway", "etl-runner", "notifications", "ledger", "warehouse"),
    "vendor": ("Datadog", "Snowflake", "Zendesk", "Okta", "Twilio", "Atlassian"),
    "month": ("January", "March", "May", "July", "September", "November"),
    "day": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
    "role": ("backend engineers", "data analysts", "SREs", "account executives"),
    "proj": ("Atlas", "Beacon", "Comet", "Delta", "Ember", "Falcon"),
    "cust": ("Northwind", "Contoso", "Fabrikam", "Tailspin", "Wingtip"),
    "office": ("Austin", "Berlin", "Dublin", "Singapore", "Toronto"),
}
_SYNTH_TENANTS: tuple[tuple[str, str, float], ...] = (
    ("acme_corp", "acme", 0.5),
    ("globex_inc", "globex", 0.25),
    ("initech_labs", "initech", 0.25),
)
_USERS_PER_TENANT = 40

_STATE_LOCK = threading.Lock()
# Rows per table, per run namespace ("" = the shared console tables).
_SEED_SIZES: dict[str, int] = {}
_FIXTURE_LOCK = threading.Lock()
_FIXTURE_REFS: dict[tuple[str, str], int] = {}  # per (database DSN, scenario)
_EXTERNAL_REFS: dict[tuple[int, str], int] = {}  # per (external target, scenario)


def _ns(config: ArenaConfig | None) -> str:
    return config.schema if config is not None else ""


def current_seed_size(config: ArenaConfig | None = None) -> int:
    """Rows per table in ``config``'s namespace (the shared tables when omitted)."""
    with _STATE_LOCK:
        return _SEED_SIZES.get(_ns(config), BASELINE_SEED_SIZE)


def _set_seed_size(config: ArenaConfig, size: int) -> None:
    with _STATE_LOCK:
        _SEED_SIZES[_ns(config)] = size


def mark_baseline_reseeded(config: ArenaConfig | None = None) -> None:
    """Called after a full reseed (TRUNCATE + 36 rows) of one namespace: no synthetic or
    fixture rows remain there. Other namespaces' state is untouched."""
    with _STATE_LOCK:
        _SEED_SIZES.pop(_ns(config), None)
    with _FIXTURE_LOCK:
        if config is None:
            _FIXTURE_REFS.clear()
        else:
            for key in [k for k in _FIXTURE_REFS if k[0] == config.db_dsn]:
                del _FIXTURE_REFS[key]


def forget_namespace(config: ArenaConfig) -> None:
    """Drop a finished run namespace's state."""
    mark_baseline_reseeded(config)


def _render(template: str, rng: np.random.Generator, i: int) -> str:
    fills = {k: v[int(rng.integers(len(v)))] for k, v in _SLOTS.items()}
    fills.update(n=str(10 + i % 90), q=str(1 + i % 4), pct=str(5 + (i * 7) % 90))
    return template.format(**fills)


def generate_synthetic_rows(config: ArenaConfig, count: int) -> list[tuple[Any, ...]]:
    """Deterministically generate `count` background rows as COPY tuples.

    Only the 25 templates are embedded with FastEmbed (cached); each row's vector is its
    template vector plus seeded Gaussian noise, re-normalized to unit length.
    """
    if count <= 0:
        return []
    rng = np.random.default_rng(_SYNTH_RNG_SEED)
    embed_fn = get_cached_embed_fn(config.embedding_model, config.embedding_dim)
    canonical = [_render(t, np.random.default_rng(0), 0) for t in _TEMPLATES]
    template_vecs = np.asarray(embed_fn(canonical), dtype=np.float64)
    template_vecs /= np.linalg.norm(template_vecs, axis=1, keepdims=True)

    tmpl_idx = rng.integers(len(_TEMPLATES), size=count)
    vecs = template_vecs[tmpl_idx] + rng.normal(0.0, _SYNTH_NOISE_SIGMA, size=(count, config.embedding_dim))
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

    weights = np.array([w for _, _, w in _SYNTH_TENANTS])
    tenant_idx = rng.choice(len(_SYNTH_TENANTS), size=count, p=weights / weights.sum())
    user_idx = rng.integers(_USERS_PER_TENANT, size=count)
    shared = rng.random(count) < 0.15
    private_clr = rng.integers(1, 4, size=count)
    shared_clr = rng.integers(1, 6, size=count)

    vec_fmt = "[" + ",".join(["%.6f"] * config.embedding_dim) + "]"
    rows: list[tuple[Any, ...]] = []
    for i in range(count):
        tenant, prefix, _ = _SYNTH_TENANTS[int(tenant_idx[i])]
        t = int(tmpl_idx[i])
        is_shared = bool(shared[i])
        content = f"{_render(_TEMPLATES[t], rng, i)} [syn-{i:05d}]"
        meta = {"seed_key": f"syn_{i:05d}", "synthetic": True, "template": t, "adversarial": False}
        rows.append(
            (
                config.app_name,
                tenant,
                f"{prefix}_emp_{int(user_idx[i]):03d}",
                None,
                "user",
                "tenant_shared" if is_shared else "private",
                int(shared_clr[i] if is_shared else private_clr[i]),
                content,
                json.dumps(meta),
                vec_fmt % tuple(vecs[i]),
            )
        )
    return rows


def iter_synthetic_chunks(
    config: ArenaConfig, count: int, *, chunk_rows: int = _CHUNK_ROWS
) -> Iterator[list[tuple[tuple[Any, ...], np.ndarray]]]:
    """Yield ``(copy_row, unit_vector)`` chunks for the 10k/100k tiers (bounded memory).

    Same templates, tenants and label mix as :func:`generate_synthetic_rows`, from its own
    seeded stream, so a given size always produces the same rows.
    """
    embed_fn = get_cached_embed_fn(config.embedding_model, config.embedding_dim)
    canonical = [_render(t, np.random.default_rng(0), 0) for t in _TEMPLATES]
    template_vecs = np.asarray(embed_fn(canonical), dtype=np.float64)
    template_vecs /= np.linalg.norm(template_vecs, axis=1, keepdims=True)
    weights = np.array([w for _, _, w in _SYNTH_TENANTS])
    weights = weights / weights.sum()
    vec_fmt = "[" + ",".join(["%.6f"] * config.embedding_dim) + "]"
    rng = np.random.default_rng(_SYNTH_RNG_SEED + count)
    for start in range(0, count, chunk_rows):
        n = min(chunk_rows, count - start)
        tmpl_idx = rng.integers(len(_TEMPLATES), size=n)
        vecs = template_vecs[tmpl_idx] + rng.normal(0.0, _SYNTH_NOISE_SIGMA, size=(n, config.embedding_dim))
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        tenant_idx = rng.choice(len(_SYNTH_TENANTS), size=n, p=weights)
        user_idx = rng.integers(_USERS_PER_TENANT, size=n)
        shared = rng.random(n) < 0.15
        private_clr = rng.integers(1, 4, size=n)
        shared_clr = rng.integers(1, 6, size=n)
        out: list[tuple[tuple[Any, ...], np.ndarray]] = []
        for j in range(n):
            i = start + j
            tenant, prefix, _ = _SYNTH_TENANTS[int(tenant_idx[j])]
            t = int(tmpl_idx[j])
            is_shared = bool(shared[j])
            meta = {"seed_key": f"syn_{i:06d}", "synthetic": True, "template": t, "adversarial": False}
            row = (
                config.app_name,
                tenant,
                f"{prefix}_emp_{int(user_idx[j]):03d}",
                None,
                "user",
                "tenant_shared" if is_shared else "private",
                int(shared_clr[j] if is_shared else private_clr[j]),
                f"{_render(_TEMPLATES[t], rng, i)} [syn-{i:06d}]",
                json.dumps(meta),
                vec_fmt % tuple(vecs[j]),
            )
            out.append((row, vecs[j]))
        yield out


def _synthetic_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    meta = json.loads(row[8])
    return {
        "tenant_id": row[1],
        "user_id": row[2],
        "scope": row[5],
        "min_clearance": int(row[6]),
        "compartments": [],
        "content": row[7],
        **meta,
    }


def _vector_of(row: tuple[Any, ...]) -> list[float]:
    return [float(x) for x in str(row[9]).strip("[]").split(",")]


_COPY_COLS = (
    "app_name, tenant_id, user_id, session_id, author, scope, min_clearance, "
    "content, custom_metadata, embedding"
)


def _copy_rows_admin(config: ArenaConfig, rows: list[tuple[Any, ...]]) -> None:
    with psycopg.connect(config.admin_dsn, connect_timeout=5) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for table in TABLES:
                    with cur.copy(f"COPY {table} ({_COPY_COLS}) FROM STDIN") as copy:
                        for row in rows:
                            copy.write_row(row)


def _load_large(config: ArenaConfig, count: int, external: Sequence[Any]) -> None:
    """COPY the 10k/100k tiers chunk by chunk into both tables (and external targets)."""
    with psycopg.connect(config.admin_dsn, connect_timeout=5) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for chunk in iter_synthetic_chunks(config, count):
                    for table in TABLES:
                        with cur.copy(f"COPY {table} ({_COPY_COLS}) FROM STDIN") as copy:
                            for row, _ in chunk:
                                copy.write_row(row)
                    for target in external:
                        target.seed_vectors([(_synthetic_payload(row), vec.tolist()) for row, vec in chunk])
        conn.execute("ANALYZE memories")
        conn.execute("ANALYZE memories_unprotected")
        conn.commit()


def set_hnsw(config: ArenaConfig, enabled: bool) -> dict[str, Any]:
    """Create (or drop) an HNSW cosine index on both arena tables.

    The 0.1.0 tables have no ANN index, so every search is exact. With the index, the
    planner may answer ``ORDER BY embedding <=> $1 LIMIT k`` from the HNSW graph and apply
    the remaining filters (app WHERE or RLS policy) afterwards.
    """
    t0 = time.perf_counter()
    with psycopg.connect(config.admin_dsn, autocommit=True, connect_timeout=5) as conn:
        for table in TABLES:
            name = f"idx_{table}_embedding_hnsw"
            if enabled:
                conn.execute("SET maintenance_work_mem = '256MB'")
                conn.execute("SET max_parallel_maintenance_workers = 0")  # no /dev/shm needs in containers
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING hnsw (embedding vector_cosine_ops)"
                )
            else:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.execute(f"ANALYZE {table}")
    return {"hnsw": enabled, "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1)}


def _insert_rows_rls(config: ArenaConfig, rows: list[tuple[Any, ...]]) -> None:
    """Fallback without admin credentials: insert per owner under that owner's RLS identity."""
    insert = f"INSERT INTO {{table}} ({_COPY_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::vector)"
    by_owner: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for row in rows:
        by_owner.setdefault((row[1], row[2]), []).append(row)
    with psycopg.connect(config.db_dsn) as conn:
        with conn.transaction():
            conn.cursor().executemany(insert.format(table="memories_unprotected"), rows)
    for (tenant, user), owner_rows in by_owner.items():
        svc = MemShieldMemoryService(dsn=config.db_dsn, tenant_id=tenant, agent_clearance=5)
        with svc.rls_transaction(app_name=config.app_name, user_id=user) as cur:
            cur.executemany(insert.format(table="memories"), owner_rows)


def _delete_tagged(config: ArenaConfig, json_key: str, value: str) -> int:
    """Delete rows whose custom_metadata[json_key] == value from both tables."""
    where = "custom_metadata->>%s = %s"
    try:
        with psycopg.connect(config.admin_dsn, autocommit=True, connect_timeout=5) as conn:
            deleted = 0
            for table in TABLES:
                deleted += conn.execute(f"DELETE FROM {table} WHERE {where}", (json_key, value)).rowcount
            return deleted
    except psycopg.OperationalError:
        pass
    # Fallback: unprotected table has no RLS; `memories` is cleaned per owner under RLS.
    with psycopg.connect(config.db_dsn, autocommit=True) as conn:
        owners = conn.execute(
            f"SELECT DISTINCT tenant_id, user_id FROM memories_unprotected WHERE {where}",
            (json_key, value),
        ).fetchall()
        deleted = conn.execute(
            f"DELETE FROM memories_unprotected WHERE {where}", (json_key, value)
        ).rowcount
    for tenant, user in owners:
        svc = MemShieldMemoryService(dsn=config.db_dsn, tenant_id=tenant)
        with svc.rls_transaction(app_name=config.app_name, user_id=user) as cur:
            cur.execute(f"DELETE FROM memories WHERE {where}", (json_key, value))
            deleted += cur.rowcount or 0
    return deleted


def delete_synthetic(config: ArenaConfig) -> int:
    """Strip every synthetic stress row; the table returns to the 36-row baseline (+ any CTF rows)."""
    deleted = _delete_tagged(config, "synthetic", "true")
    _set_seed_size(config, BASELINE_SEED_SIZE)
    return deleted


def apply_seed_size(config: ArenaConfig, seed_size: int, targets: Any = None) -> dict[str, Any]:
    """Scale both tables to `seed_size` rows (36 baseline + synthetic background rows).

    ``targets`` (a TargetSet) also scales its non-SQL targets; a target that cannot take
    synthetic rows (Mem0) raises ``ValueError`` before anything is loaded.
    """
    if seed_size not in SEED_SIZE_OPTIONS:
        raise ValueError(f"seed_size must be one of {SEED_SIZE_OPTIONS}")
    external = targets.external() if targets is not None else []
    if seed_size > BASELINE_SEED_SIZE:
        unable = [t.name for t in external if not getattr(t, "supports_synthetic", False)]
        if unable:
            raise ValueError(f"target(s) {unable} support only the {BASELINE_SEED_SIZE}-row baseline seed size")
    t0 = time.perf_counter()
    removed = _delete_tagged(config, "synthetic", "true")
    for target in external:
        target.delete_tagged("synthetic", True)
    count = seed_size - BASELINE_SEED_SIZE
    gen_ms = 0.0
    t_load = time.perf_counter()
    method = "none"
    if seed_size > LEGACY_MAX_SEED_SIZE:
        _load_large(config, count, external)
        method = "admin COPY (chunked) + ANALYZE"
    else:
        t_gen = time.perf_counter()
        rows = generate_synthetic_rows(config, count)
        gen_ms = (time.perf_counter() - t_gen) * 1000.0
        t_load = time.perf_counter()
        if rows:
            try:
                _copy_rows_admin(config, rows)
                method = "admin COPY"
            except psycopg.OperationalError:
                _insert_rows_rls(config, rows)
                method = "per-owner RLS INSERT (no admin DSN)"
            for target in external:
                target.seed_vectors([(_synthetic_payload(r), _vector_of(r)) for r in rows])
    load_ms = (time.perf_counter() - t_load) * 1000.0
    _set_seed_size(config, seed_size)
    return {
        "seed_size": seed_size,
        "synthetic_rows_per_table": count,
        "synthetic_rows_removed": removed,
        "generate_ms": round(gen_ms, 1),
        "load_ms": round(load_ms, 1),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
        "method": method,
    }


# ---------------------------------------------------------------------------
# Scenario fixtures (S6 lures, S7 compartment rows), reference-counted so overlapping
# runs share one planting
# ---------------------------------------------------------------------------


def _fixture_entry(rec: SeedRecord, tag: str) -> MemoryEntry:
    meta: dict[str, Any] = {
        "scope": rec.scope,
        "min_clearance": rec.min_clearance,
        "seed_key": rec.key,
        "persona_label": rec.persona_label,
        "adversarial": rec.is_adversarial,
        "fixture": tag,
    }
    if rec.compartments:
        meta["compartments"] = list(rec.compartments)
    return MemoryEntry(
        content=types.Content(parts=[types.Part(text=rec.content)], role="user"),
        author="user",
        custom_metadata=meta,
    )


def _plant_records(config: ArenaConfig, records: Iterable[SeedRecord], tag: str) -> None:
    embed_fn = get_cached_embed_fn(config.embedding_model, config.embedding_dim)
    pre_write = load_pre_write_hook(config)

    async def _plant() -> None:
        for rec in records:
            # Each row is written by its owner at the owner's own clearance and compartments,
            # through the same public add_memory API an agent would use; RLS WITH CHECK accepts it.
            ms = MemShieldMemoryService(
                dsn=config.db_dsn,
                tenant_id=rec.tenant_id,
                agent_clearance=rec.min_clearance,
                compartments=list(rec.compartments),
                pre_write=pre_write,
                embed_fn=embed_fn,
            )
            await ms.add_memory(app_name=config.app_name, user_id=rec.user_id, memories=[_fixture_entry(rec, tag)])
            unp = UnprotectedPgVectorMemoryService(
                dsn=config.db_dsn,
                tenant_id=rec.tenant_id,
                agent_clearance=rec.min_clearance,
                embed_fn=embed_fn,
            )
            await unp.add_memory(app_name=config.app_name, user_id=rec.user_id, memories=[_fixture_entry(rec, tag)])

    asyncio.run(_plant())


def _acquire(config: ArenaConfig, tag: str, external: list[Any]) -> None:
    records = SCENARIO_FIXTURES[tag]
    with _FIXTURE_LOCK:
        key = (config.db_dsn, tag)
        refs = _FIXTURE_REFS.get(key, 0)
        if refs == 0:
            _delete_tagged(config, "fixture", tag)
            _plant_records(config, records, tag)
        _FIXTURE_REFS[key] = refs + 1
    # Non-SQL targets get the same rows through their own write path, planted by the first
    # holder and removed by the last (runs may nest or overlap on one cached target).
    for target in external:
        with _FIXTURE_LOCK:
            key = (id(target), tag)
            refs = _EXTERNAL_REFS.get(key, 0)
            _EXTERNAL_REFS[key] = refs + 1
            if refs == 0:
                target.delete_tagged("fixture", tag)
                target.seed(records, extra={"fixture": tag})


def _release(config: ArenaConfig, tag: str, external: list[Any]) -> None:
    for target in external:
        with _FIXTURE_LOCK:
            key = (id(target), tag)
            refs = max(0, _EXTERNAL_REFS.get(key, 0) - 1)
            _EXTERNAL_REFS[key] = refs
            if refs == 0:
                target.delete_tagged("fixture", tag)
    with _FIXTURE_LOCK:
        key = (config.db_dsn, tag)
        refs = max(0, _FIXTURE_REFS.get(key, 0) - 1)
        _FIXTURE_REFS[key] = refs
        if refs == 0:
            _delete_tagged(config, "fixture", tag)


@asynccontextmanager
async def scenario_fixture(config: ArenaConfig, targets: Any = None, scenario_id: str | None = None) -> AsyncIterator[None]:
    """Plant a scenario's fixture rows (S6, S7) for the duration of the block, then remove them.

    ``targets`` (a TargetSet) adds the rows to its non-SQL targets too. Scenarios without a
    fixture are a no-op.
    """
    if scenario_id not in SCENARIO_FIXTURES:
        yield
        return
    external = targets.external() if targets is not None else []
    await asyncio.to_thread(_acquire, config, scenario_id, external)
    try:
        yield
    finally:
        await asyncio.to_thread(_release, config, scenario_id, external)


def s6_fixture(config: ArenaConfig, targets: Any = None) -> Any:
    """Scenario 6 fixture (kept for callers of 0.1.0)."""
    return scenario_fixture(config, targets, "S6")
