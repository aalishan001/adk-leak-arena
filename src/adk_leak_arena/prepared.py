"""Prepared-connection mode: one query repeated on one pinned connection, recall per run.

psycopg 3 prepares a statement after 5 executions on a connection; after 5 more,
PostgreSQL may switch to a cached *generic* plan. With an HNSW index, the generic plan for
``ORDER BY embedding <=> $1 LIMIT k`` can be the graph scan with every filter applied
afterwards, so a caller who can see only a few rows of a large table gets fewer (or zero)
results. Isolation holds; recall drops silently.

This mode seeds a run namespace (default 10,000 rows per table), builds the HNSW index,
then for each SQL target runs a light user's query ``repeats`` times on a pool of one
connection and reports recall@k per execution against the exact answer (the caller's
authorized top-k computed with index scans off), plus PostgreSQL's ``generic_plans`` /
``custom_plans`` counters for the statement.

The default query resembles one of the synthetic background templates, so its nearest
neighbours in the whole table belong mostly to other users: the case where an index scan
followed by a filter runs out of candidates. alice_analyst can read roughly 1.5% of the
synthetic rows (Clearance-1 tenant-shared rows of acme_corp) plus her baseline rows.

Shapes:

* ``default``: each target's normal search (the app WHERE clause, plus RLS for memshield);
* ``rls_only``: memshield searched with no WHERE clause (``simulate_missing_app_filter``),
  the shape of custom SQL run on ``rls_transaction()``'s cursor. Not applicable to the
  unprotected table (it would return every row).

The memory services are called directly (no ADK Runner): this mode measures the store.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from psycopg_pool import ConnectionPool

from .config import ArenaConfig
from .dataset import CALLER_PERSONAS, as_compartments, is_row_authorized
from .embedding import get_cached_embed_fn
from .namespaces import run_namespace
from .scale import apply_seed_size, set_hnsw

PREPARED_TARGETS: tuple[str, ...] = ("unprotected", "memshield", "memshield_unpinned")
DEFAULT_QUERY = "Platform weekly sync: sprint burndown is on track; retro moved to Friday."

_SHAPES: dict[str, tuple[str, ...]] = {
    "default": PREPARED_TARGETS,
    "rls_only": ("memshield", "memshield_unpinned"),
}


def _service(name: str, cfg: ArenaConfig, pool: ConnectionPool, *, top_k: int, rls_only: bool,
             persona: Any, embed_fn: Callable[[Sequence[str]], list[list[float]]]) -> Any:
    if name == "unprotected":
        from .unprotected_service import UnprotectedPgVectorMemoryService

        return UnprotectedPgVectorMemoryService(
            dsn=cfg.db_dsn, tenant_id=persona.tenant_id, agent_clearance=persona.clearance,
            agent_compartments=persona.compartments, top_k=top_k, failure_mode="control",
            embed_fn=embed_fn, pool=pool,
        )
    from adk_memshield import MemShieldConfig, MemShieldMemoryService

    service = MemShieldMemoryService(
        config=MemShieldConfig(
            dsn=cfg.db_dsn, tenant_id=persona.tenant_id, agent_clearance=persona.clearance,
            compartments=list(persona.compartments), top_k=top_k, table_name="memories",
            simulate_missing_app_filter=rls_only,
        ),
        embed_fn=embed_fn,
        pool=pool,
    )
    if name == "memshield_unpinned":
        service.store.plan_cache_mode = "auto"
    return service


def _plan_counters(pool: ConnectionPool) -> dict[str, int]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT coalesce(sum(generic_plans), 0), coalesce(sum(custom_plans), 0) "
            "FROM pg_prepared_statements WHERE statement LIKE '%<=>%'"
        ).fetchone()
    return {"generic_plans": int(row[0]), "custom_plans": int(row[1])}


def exact_truth(cfg: ArenaConfig, caller_id: str, query: str, top_k: int) -> list[str]:
    """The caller's authorized top-k by exact distance (index scans off), as seed keys."""
    from adk_memshield import MemShieldMemoryService
    from adk_memshield.service import _format_vector

    persona = CALLER_PERSONAS[caller_id]
    svc = MemShieldMemoryService(
        dsn=cfg.db_dsn, tenant_id=persona.tenant_id, agent_clearance=persona.clearance,
        compartments=list(persona.compartments),
        embed_fn=get_cached_embed_fn(cfg.embedding_model, cfg.embedding_dim),
    )
    vec = _format_vector(svc.store.embedder.embed_query(query))
    with svc.rls_transaction(app_name=cfg.app_name, user_id=persona.user_id) as cur:
        cur.execute("SET LOCAL enable_indexscan = off")
        cur.execute(
            "SELECT custom_metadata->>'seed_key' FROM memories ORDER BY embedding <=> %s::vector LIMIT %s",
            (vec, top_k),
        )
        return [str(r[0]) for r in cur.fetchall()]


def _with_startup_option(dsn: str, setting: str) -> str:
    """Append ``-c<setting>`` to the DSN's ``options`` (percent-encoded; libpq does not read '+')."""
    from urllib.parse import parse_qsl, quote, urlparse, urlunparse

    parsed = urlparse(dsn)
    pairs = parse_qsl(parsed.query)
    options = " ".join([v for k, v in pairs if k == "options"] + [f"-c{setting}"])
    rest = [(k, v) for k, v in pairs if k != "options"] + [("options", options)]
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in rest)
    return urlunparse(parsed._replace(query=query))


_POLICY_WHERE = (
    "WHERE app_name = %s AND tenant_id = %s AND (user_id = %s OR (scope = 'tenant_shared' "
    "AND min_clearance <= %s AND (cardinality(compartments) = 0 OR compartments && %s::text[])))"
)


def _walk(node: dict[str, Any], out: dict[str, Any]) -> None:
    kind = node.get("Node Type", "")
    if "Scan" in kind and "scan" not in out:
        out["scan"] = f"{kind} using {node['Index Name']}" if node.get("Index Name") else kind
    out["rows_removed_by_filter"] += int(node.get("Rows Removed by Filter", 0) or 0)
    for child in node.get("Plans", []) or []:
        _walk(child, out)


def explain_shape(cfg: ArenaConfig, name: str, shape: str, caller_id: str, query: str, top_k: int) -> dict[str, Any]:
    """EXPLAIN ANALYZE the target's search once with literal values (the custom plan)."""
    import psycopg
    from adk_memshield import MemShieldMemoryService
    from adk_memshield.service import _format_vector

    persona = CALLER_PERSONAS[caller_id]
    svc = MemShieldMemoryService(
        dsn=cfg.db_dsn, tenant_id=persona.tenant_id, agent_clearance=persona.clearance,
        compartments=list(persona.compartments),
        embed_fn=get_cached_embed_fn(cfg.embedding_model, cfg.embedding_dim),
    )
    vec = _format_vector(svc.store.embedder.embed_query(query))
    where_params = (cfg.app_name, persona.tenant_id, persona.user_id, persona.clearance, list(persona.compartments))
    table = "memories_unprotected" if name == "unprotected" else "memories"
    where = "" if shape == "rls_only" else _POLICY_WHERE
    sql = (f"EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM {table} {where} "
           "ORDER BY embedding <=> %s::vector LIMIT %s")
    params = (() if shape == "rls_only" else where_params) + (vec, top_k)
    if name == "unprotected":
        with psycopg.connect(cfg.db_dsn) as conn:
            plan = conn.execute(sql, params).fetchone()[0]
    else:
        with svc.rls_transaction(app_name=cfg.app_name, user_id=persona.user_id) as cur:
            if name == "memshield_unpinned":
                cur.execute("SELECT set_config('plan_cache_mode', 'auto', true)")
            cur.execute(sql, params)
            plan = cur.fetchone()[0]
    root = plan[0]["Plan"]
    out: dict[str, Any] = {"rows_removed_by_filter": 0}
    _walk(root, out)
    out["actual_rows"] = int(root.get("Actual Rows", 0))
    out["execution_ms"] = round(float(plan[0].get("Execution Time", 0.0)), 3)
    return out


async def _measure(name: str, shape: str, cfg: ArenaConfig, *, query: str, truth: Sequence[str],
                   caller_id: str, top_k: int, repeats: int) -> dict[str, Any]:
    from adk_memshield import reset_connection

    persona = CALLER_PERSONAS[caller_id]
    expected = list(truth)
    embed_fn = get_cached_embed_fn(cfg.embedding_model, cfg.embedding_dim)
    # One pinned connection; memshield's own reset callback, as its shared pool uses.
    pool = ConnectionPool(cfg.db_dsn, min_size=1, max_size=1, open=True, reset=reset_connection,
                          name=f"arena-prepared-{name}")
    try:
        service = _service(name, cfg, pool, top_k=top_k, rls_only=shape == "rls_only",
                           persona=persona, embed_fn=embed_fn)
        runs: list[dict[str, Any]] = []
        for i in range(1, repeats + 1):
            t0 = time.perf_counter()
            resp = await service.search_memory(app_name=cfg.app_name, user_id=persona.user_id, query=query)
            wall = (time.perf_counter() - t0) * 1000.0
            tel = service.last_query_telemetry
            keys = [str((m.custom_metadata or {}).get("seed_key", "")) for m in resp.memories]
            leaked = sum(
                1
                for m in resp.memories
                if not is_row_authorized(
                    row_tenant_id=str((m.custom_metadata or {}).get("tenant_id", "")),
                    row_user_id=str((m.custom_metadata or {}).get("user_id", "")),
                    row_scope=str((m.custom_metadata or {}).get("scope", "private")),
                    row_min_clearance=int((m.custom_metadata or {}).get("min_clearance", 1)),
                    row_compartments=as_compartments((m.custom_metadata or {}).get("compartments")),
                    caller_tenant_id=persona.tenant_id,
                    caller_user_id=persona.user_id,
                    caller_clearance=persona.clearance,
                    caller_compartments=persona.compartments,
                )
            )
            recalled = [k for k in expected if k in keys]
            runs.append({
                "execution": i,
                "rows": len(resp.memories),
                "recall_at_k": round(len(recalled) / len(expected), 3) if expected else None,
                "leaked_rows": leaked,
                "sql_ms": round(float(tel.latency_ms), 3) if tel is not None else None,
                "wall_ms": round(wall, 3),
            })
        counters = _plan_counters(pool)
    finally:
        pool.close()
    recalls = [r["recall_at_k"] for r in runs if r["recall_at_k"] is not None]
    sql = [r["sql_ms"] for r in runs if r["sql_ms"] is not None]
    return {
        "target": name,
        "shape": shape,
        "executions": repeats,
        "min_recall": min(recalls) if recalls else None,
        "mean_recall": round(statistics.fmean(recalls), 3) if recalls else None,
        "first_execution_below_full": next((r["execution"] for r in runs if (r["recall_at_k"] or 0) < 1.0), None),
        "empty_results": sum(1 for r in runs if r["rows"] == 0),
        "leaked_rows": sum(r["leaked_rows"] for r in runs),
        "p50_sql_ms": round(statistics.median(sql), 3) if sql else None,
        **counters,
        "runs": runs,
    }


def run_prepared_mode(
    config: ArenaConfig,
    *,
    seed_size: int = 10_000,
    hnsw: bool = True,
    repeats: int = 12,
    top_k: int = 10,
    caller_id: str = "alice_analyst",
    query: str = DEFAULT_QUERY,
    targets: Sequence[str] = PREPARED_TARGETS,
    iterative_scan: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """``iterative_scan=True`` connects with ``hnsw.iterative_scan=relaxed_order``
    (pgvector 0.8+): the index scan keeps fetching candidates until the filter has passed
    ``LIMIT`` rows or ``hnsw.max_scan_tuples`` is reached. It is a connection startup
    option, so memshield's ``RESET ALL`` on pool return keeps it."""
    say = log or (lambda _m: None)
    unknown = [t for t in targets if t not in PREPARED_TARGETS]
    if unknown:
        raise ValueError(f"prepared mode supports {list(PREPARED_TARGETS)}, not {unknown}")
    t0 = time.perf_counter()
    with run_namespace(config, ("unprotected", "memshield")) as (run_cfg, _tset):
        scale = apply_seed_size(run_cfg, seed_size) if seed_size > 36 else None
        say(f"namespace {run_cfg.schema}: {seed_size} rows/table loaded")
        index = set_hnsw(run_cfg, hnsw)
        say(f"HNSW {'built' if hnsw else 'off'} ({index['elapsed_ms']} ms)")
        truth = exact_truth(run_cfg, caller_id, query, top_k)
        say(f"exact authorized top-{top_k} for {caller_id}: {len(truth)} rows")
        measure_cfg = (
            replace(run_cfg, db_dsn=_with_startup_option(run_cfg.db_dsn, "hnsw.iterative_scan=relaxed_order"))
            if iterative_scan
            else run_cfg
        )
        if iterative_scan:
            say("connections use hnsw.iterative_scan=relaxed_order")
        results = []
        for shape, names in _SHAPES.items():
            for name in names:
                if name not in targets:
                    continue
                res = asyncio.run(_measure(name, shape, measure_cfg, query=query, truth=truth,
                                           caller_id=caller_id, top_k=top_k, repeats=repeats))
                res["explain"] = explain_shape(measure_cfg, name, shape, caller_id, query, top_k)
                say(f"{name} [{shape}]: min recall {res['min_recall']}, generic plans {res['generic_plans']}, "
                    f"empty results {res['empty_results']}/{repeats}, plan {res['explain']['scan']}, "
                    f"rows removed by filter {res['explain']['rows_removed_by_filter']}")
                results.append(res)
        namespace = run_cfg.schema
    return {
        "mode": "prepared_connection",
        "seed_size": seed_size,
        "hnsw": hnsw,
        "iterative_scan": "relaxed_order" if iterative_scan else "off",
        "repeats": repeats,
        "top_k": top_k,
        "caller_id": caller_id,
        "query": query,
        "exact_truth": truth,
        "namespace": namespace,
        "scale": scale,
        "index": index,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "results": results,
    }


def render_prepared_table(report: dict[str, Any]) -> str:
    head = (f"Prepared-connection mode: {report['seed_size']:,} rows/table, HNSW {'on' if report['hnsw'] else 'off'} "
            f"(iterative scan {report.get('iterative_scan', 'off')}), "
            f"{report['repeats']} executions on one pinned connection, caller={report['caller_id']}, top_k={report['top_k']}")
    lines = [head, "",
             f"{'target':<20} {'shape':<9} {'min recall':>10} {'mean':>6} {'first<1':>8} {'empty':>6} "
             f"{'generic':>8} {'custom':>7} {'leaked':>7} {'p50 ms':>8}  plan (rows removed by filter)"]
    for r in report["results"]:
        def f(v: Any, w: int) -> str:
            return f"{'n/a' if v is None else v:>{w}}"
        lines.append(
            f"{r['target']:<20} {r['shape']:<9} {f(r['min_recall'], 10)} {f(r['mean_recall'], 6)} "
            f"{f(r['first_execution_below_full'], 8)} {r['empty_results']:>6} {r['generic_plans']:>8} "
            f"{r['custom_plans']:>7} {r['leaked_rows']:>7} {f(r['p50_sql_ms'], 8)}  "
            f"{(r.get('explain') or {}).get('scan', '?')} ({(r.get('explain') or {}).get('rows_removed_by_filter', '?')})"
        )
    lines.append("")
    lines.append("per-execution recall@k: " + " | ".join(
        f"{r['target']}[{r['shape']}] " + ",".join("-" if x["recall_at_k"] is None else f"{x['recall_at_k']:g}" for x in r["runs"])
        for r in report["results"]
    ))
    return "\n".join(lines)
