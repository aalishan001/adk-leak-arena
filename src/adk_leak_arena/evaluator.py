"""Automated red-team benchmark evaluator over N memory targets.

S1-S5 (+ S7, S8, S9 when selected) are attacks, S6 is reported separately, and the
Control row measures recall and latency with a correct application filter."""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .config import ArenaConfig
from .dataset import (
    ALL_PRESETS,
    ATTACK_PRESETS,
    BOUNDARY_PRESETS,
    V020_PRESETS,
    CONTROL_QUERIES,
    SEED_RECORDS,
    TENANTS,
    WARMUP_QUERIES,
    EvalQuery,
)
from .runner_pair import EngineMode, get_target_set, run_side_by_side_comparison
from .targets import TargetSet
from .scale import current_seed_size, scenario_fixture

TARGET_LABELS = {"unprotected": "Unprotected pgvector", "memshield": "adk-memshield (RLS)"}
NOT_APPLICABLE: dict[str, Any] = {"applicable": False}
ATTACK_IDS: tuple[str, ...] = tuple(p.id for p in ATTACK_PRESETS + V020_PRESETS)
SCENARIO_IDS: tuple[str, ...] = tuple(p.id for p in ALL_PRESETS) + ("CONTROL",)
# The 0.1.0 scenario set (S1-S6 + CONTROL); S7-S9 are opt-in so default numbers reproduce.
DEFAULT_SCENARIO_IDS: tuple[str, ...] = tuple(p.id for p in ATTACK_PRESETS + BOUNDARY_PRESETS) + ("CONTROL",)

ProgressFn = Callable[[int, int, str], None]
LogFn = Callable[[str], None]


class BenchmarkCancelled(Exception):
    """Raised between queries when the operator kills a run; carries the partial report."""

    def __init__(self, partial: dict[str, Any]):
        super().__init__("benchmark cancelled by operator")
        self.partial = partial


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _summarize_target_runs(runs: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
    """Aggregate per-query verdicts (one per query) and SQL latencies (one per repeat)."""
    n = len(runs)
    lat_sorted = sorted(latencies)
    latency = {
        "latency_samples": len(lat_sorted),
        "p50_latency_ms": round(statistics.median(lat_sorted), 2) if lat_sorted else 0.0,
        "p95_latency_ms": round(_percentile(lat_sorted, 95.0), 2),
    }
    if n == 0:
        return {
            "queries_evaluated": 0,
            "queries_leaking_pct": 0.0,
            "leaked_row_share_pct": 0.0,
            "rank_1_hijack_pct": 0.0,
            "canary_exfiltration_pct": 0.0,
            "same_tier_poison_pct": 0.0,
            "recall_eligible_queries": 0,
            "authorized_recall_pct": None,
            "retrieval_errors": 0,
            **latency,
        }

    verdicts = [r["verdict"] for r in runs]
    queries_leaking = sum(1 for v in verdicts if v["leaked_row_count"] > 0)
    total_rows = sum(v["total_rows"] for v in verdicts)
    total_leaked_rows = sum(v["leaked_row_count"] for v in verdicts)
    rank_1_hijacks = sum(1 for v in verdicts if v["rank_1_hijacked"])
    canary_exfils = sum(1 for v in verdicts if v["leaked_canaries_in_output"])
    poisoned = sum(1 for v in verdicts if v["boundary_warning_count"] > 0)
    eligible = [v["recall_at_k"] for v in verdicts if v["recall_eligible"]]
    errors = sum(1 for v in verdicts if v["retrieval_error"])

    return {
        "queries_evaluated": n,
        "queries_leaking_pct": round((queries_leaking / n) * 100.0, 1),
        "leaked_row_share_pct": (
            round((total_leaked_rows / total_rows) * 100.0, 1) if total_rows > 0 else 0.0
        ),
        "rank_1_hijack_pct": round((rank_1_hijacks / n) * 100.0, 1),
        "canary_exfiltration_pct": round((canary_exfils / n) * 100.0, 1),
        "same_tier_poison_pct": round((poisoned / n) * 100.0, 1),
        "recall_eligible_queries": len(eligible),
        "authorized_recall_pct": (
            round(statistics.fmean(eligible) * 100.0, 1) if eligible else None
        ),
        "retrieval_errors": errors,
        **latency,
    }


@dataclass(frozen=True)
class _ScenarioSpec:
    id: str
    short_label: str
    title: str
    vulnerability_class: str
    failure_mode: str
    simulate_missing_app_filter: bool
    queries: tuple[EvalQuery, ...]
    boundary: bool


def _scenario_specs(selected: Sequence[str] | None) -> list[_ScenarioSpec]:
    wanted = set(selected) if selected else set(DEFAULT_SCENARIO_IDS)
    specs = [
        _ScenarioSpec(
            id=p.id,
            short_label=p.short_label,
            title=p.title,
            vulnerability_class=p.vulnerability_class,
            failure_mode=p.failure_mode,
            simulate_missing_app_filter=p.simulate_missing_app_filter,
            queries=p.benchmark_queries,
            boundary=p.id not in ATTACK_IDS,
        )
        for p in ALL_PRESETS
        if p.id in wanted
    ]
    if "CONTROL" in wanted:
        specs.append(
            _ScenarioSpec(
                id="CONTROL",
                short_label="CONTROL: Benign Baseline",
                title="Control — Benign Analyst Queries (Intact App Filter vs RLS)",
                vulnerability_class="Baseline Recall & Latency Overhead Check",
                failure_mode="control",
                simulate_missing_app_filter=False,
                queries=CONTROL_QUERIES,
                boundary=False,
            )
        )
    return specs


def build_performance_summary(
    sql_ms: dict[str, list[float]], turn_ms: dict[str, list[float]], eval_seconds: float
) -> dict[str, Any]:
    """Run-level SQL/search latency per target, the RLS delta, and evaluation throughput.

    Throughput counts query executions (each runs every target) per second of evaluation
    wall time, excluding the untimed warmup.
    """

    def _stats(vals: list[float]) -> tuple[float, float]:
        ordered = sorted(vals)
        return (
            round(statistics.median(ordered), 3) if ordered else 0.0,
            round(_percentile(ordered, 95.0), 3),
        )

    per_target = {}
    for name, vals in sql_ms.items():
        if not vals:
            continue  # refused or n/a in every scenario
        p50, p95 = _stats(vals)
        per_target[name] = {"p50_ms": p50, "p95_ms": p95, "samples": len(vals)}
    executions = max((len(v) for v in sql_ms.values()), default=0)
    turns = [x for vals in turn_ms.values() for x in vals]
    out: dict[str, Any] = {
        "targets": per_target,
        "mean_agent_turn_ms": round(statistics.fmean(turns), 2) if turns else 0.0,
        "query_executions": executions,
        "eval_seconds": round(eval_seconds, 3),
        "throughput_qps": round(executions / eval_seconds, 2) if eval_seconds > 0 else 0.0,
    }
    if "unprotected" in per_target and "memshield" in per_target:
        u50, u95 = per_target["unprotected"]["p50_ms"], per_target["unprotected"]["p95_ms"]
        m50, m95 = per_target["memshield"]["p50_ms"], per_target["memshield"]["p95_ms"]
        out.update(
            {
                "unprotected_p50_ms": u50,
                "unprotected_p95_ms": u95,
                "memshield_p50_ms": m50,
                "memshield_p95_ms": m95,
                "p50_delta_ms": round(m50 - u50, 3),
                "p95_delta_ms": round(m95 - u95, 3),
                "p50_delta_pct": round((m50 - u50) / u50 * 100.0, 1) if u50 > 0 else None,
                "rls_equal_or_faster_p50": m50 <= u50,
            }
        )
    return out


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v}%"


def _md_row(label: str, target_label: str, m: dict[str, Any], *, bold_security: bool) -> str:
    if m.get("configuration_refused"):
        return f"| **{label}** | `{target_label}` | refused (config) | | | | | | |"
    if not m.get("applicable", True):
        return f"| **{label}** | `{target_label}` | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
    b = "**" if bold_security else ""
    recall = _fmt_pct(m["authorized_recall_pct"])
    return (
        f"| **{label}** | `{target_label}` | {b}{m['queries_leaking_pct']}%{b} | "
        f"{b}{m['leaked_row_share_pct']}%{b} | {b}{m['rank_1_hijack_pct']}%{b} | "
        f"{b}{m['canary_exfiltration_pct']}%{b} | {m['same_tier_poison_pct']}% | "
        f"{recall} (n={m['recall_eligible_queries']}) | "
        f"{m['p50_latency_ms']} / {m['p95_latency_ms']} ms |"
    )


def build_markdown_table(
    scenarios: list[dict[str, Any]],
    aggregate: dict[str, dict[str, Any]] | None,
    top_k: int,
    target_info: list[dict[str, Any]] | None = None,
) -> str:
    info = target_info or [
        {"name": "unprotected", "label": TARGET_LABELS["unprotected"], "protected": False},
        {"name": "memshield", "label": TARGET_LABELS["memshield"], "protected": True},
    ]
    lines = [
        "| Scenario | Target | Queries Leaking ≥1 Row | Leaked Row Share | Rank-1 Hijack | "
        f"Canary Exfil (Output) | Same-Tier Poison Retrieved | Authorized Recall@{top_k} (eligible n) | "
        "p50 / p95 Search Latency |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for s in scenarios:
        label = s["short_label"] + (" (boundary, excluded from aggregate)" if s.get("boundary") else "")
        for t in info:
            m = s.get("targets", {}).get(t["name"]) or s.get(t["name"]) or NOT_APPLICABLE
            lines.append(_md_row(label, t["label"], m, bold_security=t["protected"]))
    if aggregate:
        for t in info:
            m = aggregate.get(t["name"])
            if m is not None:
                lines.append(_md_row("AGGREGATE (attacks)", t["label"], m, bold_security=True))
    return "\n".join(lines)


def _verdict_line(res: dict[str, Any]) -> str:
    def side(pane: dict[str, Any]) -> str:
        if pane.get("configuration_refused"):
            return "refused (configuration)"
        if not pane.get("applicable", True):
            return "n/a"
        v = pane["verdict"]
        lat = pane["telemetry"].get("latency_ms", 0.0)
        return f"{v['status']} {v['leaked_row_count']}/{v['total_rows']} leaked, {lat:.2f}ms"

    return " | ".join(f"{name}: {side(pane)}" for name, pane in res["targets"].items())


async def run_full_benchmark(
    config: ArenaConfig,
    *,
    caller_id: str = "alice_analyst",
    engine: EngineMode = "deterministic_agent",
    repeats: int = 3,
    warmup: bool = True,
    top_k: int | None = None,
    scenarios: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
    log: LogFn | None = None,
    cancel_event: asyncio.Event | None = None,
    targets: TargetSet | Sequence[str] | None = None,
) -> dict[str, Any]:
    """Execute the selected scenarios against every target and compute the matrix.

    `cancel_event` is checked before every query; when set, `BenchmarkCancelled` is raised
    with a partial report of the scenarios that finished.
    """
    tset = targets if isinstance(targets, TargetSet) else get_target_set(config, targets)
    names = tset.names
    target_info = [tset.targets[n].describe() for n in names]
    effective_top_k = max(1, min(int(top_k or config.top_k), 20))
    repeats = max(1, int(repeats))
    if engine != "deterministic_agent" and repeats > 1:
        # Extra repeats only add latency samples; do not multiply LLM inferences for that.
        repeats = 1
        if log:
            log(f"engine={engine}: forcing repeats=1 (latency samples do not need extra LLM inferences)")
    specs = _scenario_specs(scenarios)
    total_steps = (len(WARMUP_QUERIES) if warmup else 0) + sum(len(s.queries) * repeats for s in specs)
    step = 0
    say = log or (lambda _msg: None)
    scenario_rows: list[dict[str, Any]] = []
    agg: dict[str, tuple[list[dict[str, Any]], list[float]]] = {n: ([], []) for n in names}
    agg_applicable: dict[str, bool] = {n: False for n in names}
    all_sql: dict[str, list[float]] = {n: [] for n in names}
    all_turn: dict[str, list[float]] = {n: [] for n in names}
    eval_started: list[float] = []

    def _report(elapsed_ms: float, warmup_info: dict[str, Any] | None) -> dict[str, Any]:
        has_attacks = any(s["id"] in ATTACK_IDS for s in scenario_rows)
        aggregate = (
            {n: _summarize_target_runs(*agg[n]) for n in names if agg_applicable[n]}
            if has_attacks
            else None
        )
        return {
            "caller_id": caller_id,
            "engine": engine,
            "top_k": effective_top_k,
            "seed_size": current_seed_size(config),
            "elapsed_ms": round(elapsed_ms, 2),
            "steps_completed": step,
            "total_steps": total_steps,
            "targets": target_info,
            "methodology": {
                "seed_records": len(SEED_RECORDS),
                "seed_size": current_seed_size(config),
                "tenants": list(TENANTS),
                "top_k": effective_top_k,
                "targets": list(names),
                "namespace": config.schema or "default",
                "scenarios": [s.id for s in specs],
                "queries_per_scenario": {s.id: len(s.queries) for s in specs},
                "latency_repeats_per_query": repeats,
                "warmup": warmup_info,
                "latency_definition": (
                    "Wall time of the target's search call (for PostgreSQL targets: pooled "
                    "connection checkout + transaction + search SQL, adk-memshield including its "
                    "set_config round-trip; for Qdrant: the client search call), query embedding "
                    "excluded; for Mem0: the arena adapter's two Mem0 searches, which embed the query "
                    "themselves, so embedding is included."
                ),
                "recall_definition": (
                    "Recall@K over queries whose relevant seed records include at least one the "
                    "caller may read; pure cross-user/cross-tenant exfiltration probes are excluded."
                ),
                "boundary_definition": (
                    "S6 is reported separately: authorized same-tier shared rows written by an "
                    "adversary are retrieved by every correctly filtered target and excluded from "
                    "the attack aggregate."
                ),
                "not_applicable": (
                    "A target that cannot express a scenario's application mistake through its own "
                    "API reports n/a for that scenario and is left out of its aggregate."
                ),
            },
            "scenarios": list(scenario_rows),
            "aggregate_attacks": aggregate,
            "performance": build_performance_summary(
                all_sql, all_turn, (time.perf_counter() - eval_started[0]) if eval_started else 0.0
            ),
            "markdown_table": build_markdown_table(scenario_rows, aggregate, effective_top_k, target_info),
        }

    def _tick(label: str) -> None:
        nonlocal step
        if cancel_event is not None and cancel_event.is_set():
            raise BenchmarkCancelled(_report((time.perf_counter() - t0) * 1000.0, warmup_info))
        step += 1
        if progress:
            progress(step, total_steps, label)

    t0 = time.perf_counter()
    warmup_info: dict[str, Any] | None = None
    if warmup:
        # Untimed warmup so the first scenario does not absorb one-off cold-start costs
        # (ONNX embedder load, pooled connections, ADK Runner imports, client warm-up).
        tw = time.perf_counter()
        for q in WARMUP_QUERIES:
            _tick(f"warmup: {q}")
            await run_side_by_side_comparison(
                config=config,
                query=q,
                caller_id=caller_id,
                preset_id="CONTROL",
                failure_mode="control",
                simulate_missing_app_filter=False,
                engine="deterministic_agent",
                relevant_keys=(),
                top_k=effective_top_k,
                targets=tset,
            )
        warmup_info = {
            "queries": len(WARMUP_QUERIES),
            "targets": list(names),
            "elapsed_ms": round((time.perf_counter() - tw) * 1000.0, 2),
        }
        say(f"warmup: {len(WARMUP_QUERIES)} untimed queries on {len(names)} targets ({warmup_info['elapsed_ms']} ms)")

    eval_started.append(time.perf_counter())
    for spec in specs:
        say(f"scenario {spec.id}: {spec.short_label} ({len(spec.queries)} queries x {repeats})")
        out: dict[str, tuple[list[dict[str, Any]], list[float]]] = {n: ([], []) for n in names}
        applicable = {n: tset.targets[n].capabilities.supports(spec.failure_mode) for n in names}
        refused: dict[str, str] = {}
        # Searches memshield refused on identity (S8) answer before any SQL runs; their
        # timings stay in the scenario row but not in the aggregate or run-level latency.
        timed: dict[str, list[float]] = {n: [] for n in names}
        identity_refusals: dict[str, int] = {n: 0 for n in names}
        async with scenario_fixture(config, tset, spec.id):
            for qi, q in enumerate(spec.queries, start=1):
                for rep in range(repeats):
                    _tick(f"{spec.id} {qi}/{len(spec.queries)} rep {rep + 1}/{repeats}: {q.text}")
                    res = await run_side_by_side_comparison(
                        config=config,
                        query=q.text,
                        caller_id=q.caller_id or caller_id,
                        preset_id=spec.id if spec.id != "CONTROL" else "CONTROL",
                        failure_mode=spec.failure_mode,  # type: ignore[arg-type]
                        simulate_missing_app_filter=spec.simulate_missing_app_filter,
                        spoofed_user_id=None,
                        engine=engine,
                        relevant_keys=q.relevant_keys,
                        top_k=effective_top_k,
                        targets=tset,
                    )
                    if rep == 0:
                        say(f"  [{spec.id} {qi}/{len(spec.queries)}] {_verdict_line(res)}")
                    for name in names:
                        pane = res["targets"][name]
                        if pane.get("configuration_refused"):
                            refused[name] = str(pane.get("reason", ""))
                            continue
                        if not pane.get("applicable", True):
                            continue
                        runs, lats = out[name]
                        # Retrieval is deterministic, so the security verdict is taken from the
                        # first repeat; extra repeats only add latency samples.
                        if rep == 0:
                            runs.append(pane)
                        lat = float(pane["telemetry"].get("latency_ms", 0.0))
                        lats.append(lat)
                        if pane["telemetry"].get("identity_refused"):
                            if rep == 0:
                                identity_refusals[name] += 1
                        else:
                            timed[name].append(lat)
                            all_sql[name].append(lat)
                        all_turn[name].append(float(pane.get("total_runner_ms", 0.0)))

        for name in refused:
            applicable[name] = False
        if spec.id in ATTACK_IDS:
            for name in names:
                if applicable[name]:
                    agg_applicable[name] = True
                    agg[name][0].extend(out[name][0])
                    agg[name][1].extend(timed[name])
        summaries = {
            name: (
                _summarize_target_runs(*out[name])
                | {"applicable": True, "identity_refusals": identity_refusals[name]}
                if applicable[name]
                else {"applicable": False, "configuration_refused": True, "reason": refused[name]}
                if name in refused
                else NOT_APPLICABLE
            )
            for name in names
        }
        row: dict[str, Any] = {
            "id": spec.id,
            "short_label": spec.short_label,
            "title": spec.title,
            "vulnerability_class": spec.vulnerability_class,
            "failure_mode": spec.failure_mode,
            "simulate_missing_app_filter": spec.simulate_missing_app_filter,
            "boundary": spec.boundary,
            "targets": summaries,
        }
        for legacy in ("unprotected", "memshield"):
            if legacy in summaries:
                row[legacy] = summaries[legacy]
        unp, ms = summaries.get("unprotected"), summaries.get("memshield")
        if unp and ms and unp.get("applicable") and ms.get("applicable") and not ms.get("identity_refusals"):
            row["rls_delta_p50_ms"] = round(ms["p50_latency_ms"] - unp["p50_latency_ms"], 2)
            row["rls_delta_p95_ms"] = round(ms["p95_latency_ms"] - unp["p95_latency_ms"], 2)
        scenario_rows.append(row)

    return _report((time.perf_counter() - t0) * 1000.0, warmup_info)
