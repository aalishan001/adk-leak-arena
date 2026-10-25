"""Capture EXPLAIN (ANALYZE, BUFFERS) for the protected and unprotected searches at 2,000 rows.

Scales both tables to 2,000 rows with the deterministic synthetic generator, then explains
the exact SQL each service issues (taken from the services themselves) for the S5 query as
alice_analyst (acme_corp, clearance 1):

  P1  adk-memshield, S5 chaos path (no WHERE; RLS only), under rls_transaction
  U1  unprotected,   S5 chaos path (no WHERE)
  P2  adk-memshield, default path (app WHERE mirroring RLS + RLS)
  U2  unprotected,   S1 path (tenant-only WHERE)

Restores the 36-row baseline afterwards. The planner's choice depends on table statistics,
so `--analyze` runs `ANALYZE` on both tables after scaling (the arena's own seeding flow
does not). Usage (inside the web container, with no server benchmark job running):
    python benchmarks/explain_plans.py             # statistics as left by the bulk COPY
    python benchmarks/explain_plans.py --analyze   # fresh statistics
"""

from __future__ import annotations

import argparse

import psycopg
from adk_memshield import MemShieldMemoryService
from adk_memshield.service import _format_vector

from adk_leak_arena.config import ArenaConfig
from adk_leak_arena.dataset import PRESET_BY_ID
from adk_leak_arena.embedding import get_cached_embed_fn
from adk_leak_arena.runner_pair import ensure_schema_and_seed
from adk_leak_arena.scale import apply_seed_size, delete_synthetic
from adk_leak_arena.unprotected_service import UnprotectedPgVectorMemoryService

CALLER, TENANT, CLEARANCE, TOP_K = "alice_analyst", "acme_corp", 1, 3


def _explain(cur: psycopg.Cursor, sql: str, params: tuple) -> str:
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
    return "\n".join(row[0] for row in cur.fetchall())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyze", action="store_true", help="ANALYZE both tables before explaining")
    args = parser.parse_args()
    cfg = ArenaConfig()
    ensure_schema_and_seed(cfg)
    scale = apply_seed_size(cfg, 2000)
    print(f"scale: {scale}")
    if args.analyze:
        with psycopg.connect(cfg.admin_dsn, autocommit=True) as admin:
            admin.execute("ANALYZE memories, memories_unprotected")
        print("statistics: ANALYZE memories, memories_unprotected (run after scaling)")
    else:
        print("statistics: as left by the bulk COPY (no explicit ANALYZE)")
    try:
        query = PRESET_BY_ID["S5"].default_query
        embed_fn = get_cached_embed_fn(cfg.embedding_model, cfg.embedding_dim)
        vec = _format_vector(embed_fn([query])[0])
        print(f"query: {query}")

        with psycopg.connect(cfg.admin_dsn) as admin:
            print(admin.execute("SELECT version()").fetchone()[0])
            print("pgvector", admin.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0])
            for row in admin.execute(
                "SELECT relname, n_live_tup, last_analyze, last_autoanalyze FROM pg_stat_user_tables "
                "WHERE relname IN ('memories','memories_unprotected') ORDER BY relname"
            ):
                print("stats", row)
            tenant_rows = admin.execute(
                "SELECT count(*) FROM memories WHERE app_name=%s AND tenant_id=%s", (cfg.app_name, TENANT)
            ).fetchone()[0]
            print(f"memories rows in ({cfg.app_name}, {TENANT}): {tenant_rows}")

        def ms_sql(chaos: bool) -> str:
            svc = MemShieldMemoryService(
                dsn=cfg.db_dsn, tenant_id=TENANT, agent_clearance=CLEARANCE, top_k=TOP_K,
                simulate_missing_app_filter=chaos, embed_fn=embed_fn,
            )
            svc._search_memory_sync(app_name=cfg.app_name, user_id=CALLER, query=query)
            return svc.last_query_telemetry.sql, svc

        p1_sql, ms = ms_sql(True)
        p2_sql, _ = ms_sql(False)
        with ms.rls_transaction(app_name=cfg.app_name, user_id=CALLER) as cur:
            visible = cur.execute("SELECT count(*) FROM memories").fetchone()[0]
            print(f"memories rows visible to {CALLER} under RLS: {visible}")
            plans = {
                "P1 adk-memshield S5 (no WHERE, RLS only)": (p1_sql, _explain(cur, p1_sql, (vec, vec, TOP_K))),
                "P2 adk-memshield default (app WHERE + RLS)": (
                    p2_sql,
                    _explain(cur, p2_sql, (vec, cfg.app_name, TENANT, CALLER, CLEARANCE, vec, TOP_K)),
                ),
            }

        def unp_sql(mode: str) -> tuple[str, tuple]:
            svc = UnprotectedPgVectorMemoryService(
                dsn=cfg.db_dsn, tenant_id=TENANT, agent_clearance=CLEARANCE, top_k=TOP_K,
                failure_mode=mode, simulate_missing_app_filter=(mode == "strip_all_where_filters"),
            )
            sql, params, _ = svc._build_query_and_params(
                app_name=cfg.app_name, user_id=CALLER, query=query, vec_literal=vec
            )
            return sql, params

        with psycopg.connect(cfg.db_dsn) as conn, conn.transaction(), conn.cursor() as cur:
            for label, mode in (
                ("U1 unprotected S5 (no WHERE)", "strip_all_where_filters"),
                ("U2 unprotected S1 (tenant-only WHERE)", "shared_tenant_no_user_filter"),
            ):
                sql, params = unp_sql(mode)
                plans[label] = (sql, _explain(cur, sql, params))

        for label in sorted(plans):
            sql, plan = plans[label]
            print(f"\n===== {label}\n--- SQL\n{sql.strip()}\n--- PLAN\n{plan}")
    finally:
        removed = delete_synthetic(cfg)
        print(f"\ncleanup: removed {removed} synthetic rows; baseline restored")


if __name__ == "__main__":
    main()
