"""Per-query ledger behind the headline benchmark percentages.

Runs every benchmark query once (deterministic_agent, alice_analyst, top_k=3) on a freshly
seeded 36-row baseline and prints one line per query plus the exact numerators and
denominators for the S1-S5 aggregate. Retrieval is deterministic, so the counts must match
`leak-arena eval`.

Usage (inside the web container, with no server benchmark job running):
    python benchmarks/query_ledger.py
"""

from __future__ import annotations

import asyncio

from adk_leak_arena.config import ArenaConfig
from adk_leak_arena.dataset import ALL_PRESETS, ATTACK_PRESETS, CONTROL_QUERIES
from adk_leak_arena.runner_pair import ensure_schema_and_seed, run_side_by_side_comparison

ATTACK_IDS = {p.id for p in ATTACK_PRESETS}


async def main() -> None:
    cfg = ArenaConfig()
    ensure_schema_and_seed(cfg)
    specs = [(p.id, p.failure_mode, p.simulate_missing_app_filter, p.benchmark_queries) for p in ALL_PRESETS]
    specs.append(("CONTROL", "control", False, CONTROL_QUERIES))

    totals = {t: {"queries": 0, "leaking": 0, "recall_sum": 0.0, "recall_n": 0} for t in ("unprotected", "memshield")}
    errors = 0
    print("scenario q# kind  | unprotected leaked/total recall | memshield leaked/total recall | query")
    for sid, mode, chaos, queries in specs:
        for i, q in enumerate(queries, 1):
            res = await run_side_by_side_comparison(
                config=cfg,
                query=q.text,
                caller_id="alice_analyst",
                preset_id=sid,
                failure_mode=mode,  # type: ignore[arg-type]
                simulate_missing_app_filter=chaos,
                engine="deterministic_agent",
                relevant_keys=q.relevant_keys,
                top_k=3,
            )
            cells = []
            for t in ("unprotected", "memshield"):
                v = res[t]["verdict"]
                errors += bool(v["retrieval_error"])
                r = "n/a" if v["recall_at_k"] is None else f"{v['recall_at_k']:.2f}"
                cells.append(f"{v['leaked_row_count']}/{v['total_rows']} {r:>4}")
                if sid in ATTACK_IDS:
                    tt = totals[t]
                    tt["queries"] += 1
                    tt["leaking"] += v["leaked_row_count"] > 0
                    if v["recall_eligible"]:
                        tt["recall_sum"] += v["recall_at_k"]
                        tt["recall_n"] += 1
            kind = "mixed" if res["unprotected"]["verdict"]["recall_eligible"] else "pure"
            print(f"{sid:<8} {i:>2} {kind:<5} | {cells[0]:>19} | {cells[1]:>17} | {q.text}")

    print("\nS1-S5 aggregate (alice_analyst, top_k=3, 36 rows):")
    for t, tt in totals.items():
        recall = tt["recall_sum"] / tt["recall_n"] * 100 if tt["recall_n"] else float("nan")
        print(
            f"  {t:<11} queries leaking >=1 row: {tt['leaking']}/{tt['queries']} = "
            f"{tt['leaking'] / tt['queries'] * 100:.1f}% | Recall@3: {tt['recall_sum']:.1f}/{tt['recall_n']} "
            f"eligible = {recall:.1f}%"
        )
    print(f"  retrieval errors across all queries and both targets: {errors}")


if __name__ == "__main__":
    asyncio.run(main())
