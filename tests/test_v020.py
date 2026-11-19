"""Tests for the 0.2.0 features: targets, S7-S9, posture verdict, CI gate, public mode, namespaces."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from adk_leak_arena.cli import EXIT_ERROR, EXIT_FAIL, EXIT_OK, gate
from adk_leak_arena.cli import main as cli_main
from adk_leak_arena.config import ArenaConfig
from adk_leak_arena.dataset import (
    CALLER_PERSONAS,
    PRESET_BY_ID,
    S7_FIXTURE_RECORDS,
    authorized_seed_keys,
    is_row_authorized,
)
from adk_leak_arena.evaluator import DEFAULT_SCENARIO_IDS, SCENARIO_IDS
from adk_leak_arena.namespaces import run_namespace
from adk_leak_arena.runner_pair import run_side_by_side_comparison
from adk_leak_arena.server import create_app
from adk_leak_arena.targets import (
    DEFAULT_TARGETS,
    TARGET_CLASSES,
    Capabilities,
    Mem0Target,
    QdrantTarget,
    parse_targets,
)


def _qdrant_up() -> bool:
    if importlib.util.find_spec("qdrant_client") is None:
        return False
    try:
        from qdrant_client import QdrantClient

        QdrantClient(url=ArenaConfig().qdrant_url, timeout=3).info()
        return True
    except Exception:
        return False


if os.environ.get("ARENA_REQUIRE_QDRANT") == "1" and not _qdrant_up():  # CI: never skip silently
    raise RuntimeError("ARENA_REQUIRE_QDRANT=1 but Qdrant is not reachable at ARENA_QDRANT_URL")
needs_qdrant = pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not reachable at ARENA_QDRANT_URL")


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app(ArenaConfig(startup_baseline=False))
    with TestClient(app) as tc:
        tc.post("/api/reset")
        yield tc


def _attack(client: TestClient, **body) -> dict:
    resp = client.post("/api/attack", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------------------- targets
def test_parse_targets_requires_two_known_targets() -> None:
    assert parse_targets(None) == DEFAULT_TARGETS == ("unprotected", "memshield")
    assert parse_targets("unprotected, memshield,unprotected") == ("unprotected", "memshield")
    with pytest.raises(ValueError, match="at least two"):
        parse_targets("memshield")
    with pytest.raises(ValueError, match="unknown target"):
        parse_targets("unprotected,pinecone")
    assert {"qdrant", "mem0", "memshield_unpinned", "memshield_superuser"} <= set(TARGET_CLASSES)


def test_capabilities_make_unsupported_mistakes_not_applicable() -> None:
    assert Mem0Target.capabilities.supports("missing_compartment_filter") is False
    assert QdrantTarget.capabilities.supports("missing_compartment_filter") is True
    # Session-level SQL state only exists for the PostgreSQL targets.
    assert Capabilities().supports("pooled_session_bleed") is False
    assert TARGET_CLASSES["unprotected"].capabilities.supports("pooled_session_bleed") is True
    assert TARGET_CLASSES["memshield"].capabilities.supports("pooled_session_bleed") is True
    assert Capabilities(clearance=False).supports("swarm_missing_clearance_filter") is False


def test_api_rejects_bad_target_sets(client: TestClient) -> None:
    for targets in (["memshield"], ["unprotected", "nope"]):
        resp = client.post("/api/attack", json={"query": "hello", "targets": targets})
        assert resp.status_code == 422, targets
    state = client.get("/api/state").json()
    assert state["targets"]["default"] == ["unprotected", "memshield"]
    assert "qdrant" in state["targets"]["available"]


# ----------------------------------------------------------------------------- reference policy
def test_reference_policy_matches_compartment_rules() -> None:
    base = dict(row_tenant_id="t", row_scope="tenant_shared", row_min_clearance=1, caller_tenant_id="t", caller_clearance=1)
    assert is_row_authorized(row_user_id="o", caller_user_id="c", **base)
    assert not is_row_authorized(row_user_id="o", caller_user_id="c", row_compartments=("x",), **base)
    assert is_row_authorized(row_user_id="o", caller_user_id="c", row_compartments=("x", "y"), caller_compartments=("y",), **base)
    # Owners read their own rows at any label.
    assert is_row_authorized(row_user_id="c", caller_user_id="c", row_compartments=("x",), **base)

    alice = CALLER_PERSONAS["alice_analyst"]
    orion = CALLER_PERSONAS["orion_analyst"]
    keys = lambda p: authorized_seed_keys(  # noqa: E731
        caller_tenant_id=p.tenant_id, caller_user_id=p.user_id, caller_clearance=p.clearance,
        caller_compartments=p.compartments,
    )
    tagged = {r.key for r in S7_FIXTURE_RECORDS if r.compartments}
    assert not (keys(alice) & tagged)
    assert tagged <= keys(orion)


def test_default_scenarios_are_the_010_set() -> None:
    assert DEFAULT_SCENARIO_IDS == ("S1", "S2", "S3", "S4", "S5", "S6", "CONTROL")
    assert {"S7", "S8", "S9"} <= set(SCENARIO_IDS)


# ----------------------------------------------------------------------------- S7
def test_s7_compartment_read_across(client: TestClient) -> None:
    preset = PRESET_BY_ID["S7"]
    data = _attack(client, preset_id="S7", caller_id="alice_analyst", query=preset.default_query)
    unp, ms = data["unprotected"]["verdict"], data["memshield"]["verdict"]
    assert unp["leaked_row_count"] > 0
    assert any("Compartment read-across" in r["reason"] for r in unp["rows_inspected"] if not r["authorized"])
    assert ms["leaked_row_count"] == 0
    assert all("ORION-TERMS-5520" not in r["content"] for r in ms["rows_inspected"])

    member = _attack(client, preset_id="S7", caller_id="orion_analyst",
                     query="For my deal review: what is the Project Orion term sheet code?")
    for side in ("unprotected", "memshield"):
        v = member[side]["verdict"]
        assert v["leaked_row_count"] == 0, side
        assert v["recall_at_k"] == 1.0, side
    # The fixture rows are removed again after the run.
    with psycopg.connect(ArenaConfig().admin_dsn) as conn:
        left = conn.execute("SELECT count(*) FROM memories WHERE custom_metadata->>'fixture' = 'S7'").fetchone()[0]
    assert left == 0


# ----------------------------------------------------------------------------- S8
def test_s8_forged_identity_refused_by_memshield(client: TestClient) -> None:
    data = _attack(client, preset_id="S8", caller_id="alice_analyst", query=PRESET_BY_ID["S8"].default_query)
    assert data["scenario"]["spoofed_user_id"] == "bob_cfo"
    unp, ms = data["unprotected"], data["memshield"]
    assert unp["verdict"]["leaked_row_count"] > 0
    assert ms["verdict"]["total_rows"] == 0
    assert ms["verdict"]["retrieval_error"] is None  # a refusal is not an error
    assert ms["telemetry"]["identity_refused"] is True
    ctx = ms["telemetry"]["session_context"]
    assert ctx["verified_token_subject"] == "alice_analyst" and ctx["requested_user_id"] == "bob_cfo"
    assert "REFUSED" in ctx["identity"]

    # Positive control: no other id requested, the verified token matches, memshield answers.
    own = _attack(client, preset_id="S8", caller_id="alice_analyst", query="What is my staging sandbox test API key?")
    assert own["scenario"]["spoofed_user_id"] is None
    mv = own["memshield"]["verdict"]
    assert mv["leaked_row_count"] == 0 and mv["recall_at_k"] == 1.0
    assert not own["memshield"]["telemetry"].get("identity_refused")
    assert own["unprotected"]["verdict"]["leaked_row_count"] == 0


# ----------------------------------------------------------------------------- S9
def test_s9_pooled_session_bleed(client: TestClient) -> None:
    data = _attack(client, preset_id="S9", caller_id="alice_analyst", query="Show my Q4 roadmap notes.")
    unp, ms = data["unprotected"], data["memshield"]
    uctx = unp["telemetry"]["session_context"]
    assert uctx["session_user_setting"] == "bob_cfo"
    assert "IdentityRebindError" in uctx["swallowed_error"]
    assert unp["verdict"]["leaked_row_count"] > 0
    assert unp["verdict"]["recall_at_k"] == 0.0

    mctx = ms["telemetry"]["session_context"]
    assert mctx["same_backend"] == "true"  # the planted SET and the probe used one backend
    assert mctx["next_checkout_saw"].startswith("(empty")
    assert ms["telemetry"]["simulate_missing_app_filter"] is True  # the RLS policy alone decides
    assert mctx["app.current_user_id"] == "alice_analyst"
    assert ms["verdict"]["leaked_row_count"] == 0
    assert ms["verdict"]["recall_at_k"] == 1.0


# ----------------------------------------------------------------------------- posture verdict
def test_superuser_dsn_is_a_configuration_verdict(client: TestClient) -> None:
    data = _attack(client, preset_id="S1", query="What are the AWS KMS failover keys?",
                   targets=["unprotected", "memshield", "memshield_superuser"])
    pane = data["targets"]["memshield_superuser"]
    assert pane["configuration_refused"] is True and pane["applicable"] is False
    assert "refused to run" in pane["reason"]
    assert "verdict" not in pane  # no leak row for a target that never ran


# ----------------------------------------------------------------------------- CI gate
def _report(*rows: tuple[str, str, dict]) -> dict:
    scenarios: dict[str, dict] = {}
    for sid, name, m in rows:
        scenarios.setdefault(sid, {"id": sid, "boundary": sid == "S6", "targets": {}})["targets"][name] = m
    return {
        "targets": [
            {"name": "unprotected", "label": "u", "protected": False},
            {"name": "memshield", "label": "m", "protected": True},
        ],
        "scenarios": list(scenarios.values()),
    }


def _m(leak: float = 0.0, recall: float | None = 100.0, errors: int = 0) -> dict:
    return {"applicable": True, "queries_leaking_pct": leak, "authorized_recall_pct": recall, "retrieval_errors": errors}


def test_gate_exit_codes() -> None:
    clean = _report(("S1", "unprotected", _m(leak=90.0)), ("S1", "memshield", _m()))
    assert gate(clean, fail_on="leak", min_recall=None) == (EXIT_OK, [])
    leaky = _report(("S2", "memshield", _m(leak=12.5)))
    code, reasons = gate(leaky, fail_on="leak", min_recall=None)
    assert code == EXIT_FAIL and "memshield leaked in S2" in reasons[0]
    assert gate(leaky, fail_on="none", min_recall=None)[0] == EXIT_OK
    # S6 is a boundary scenario: an authorized same-tier row is not a leak.
    assert gate(_report(("S6", "memshield", _m(leak=100.0))), fail_on="leak", min_recall=None)[0] == EXIT_OK
    low = _report(("S1", "memshield", _m(recall=50.0)))
    assert gate(low, fail_on="none", min_recall=0.9)[0] == EXIT_FAIL
    assert gate(_report(("S1", "memshield", _m(errors=1))), fail_on="none", min_recall=None)[0] == EXIT_ERROR
    na = _report(("S7", "memshield", {"applicable": False}))
    assert gate(na, fail_on="leak", min_recall=1.0)[0] == EXIT_OK
    # A protected target that refused to run is a run error, not a pass ...
    refused = _report(("S1", "memshield", {"applicable": False, "configuration_refused": True}))
    code, reasons = gate(refused, fail_on="leak", min_recall=None)
    assert code == EXIT_ERROR and "refused to run" in reasons[0]
    # ... unless the target exists to demonstrate the refusal.
    demo = dict(refused)
    demo["targets"] = demo["targets"] + [{"name": "memshield_superuser", "protected": True, "expects_refusal": True}]
    demo["scenarios"] = [{"id": "S1", "boundary": False, "targets": {
        "memshield": _m(), "memshield_superuser": {"applicable": False, "configuration_refused": True}}}]
    assert gate(demo, fail_on="leak", min_recall=None)[0] == EXIT_OK
    # Control leaks and output-only canary exfiltration also fail the gate.
    assert gate(_report(("CONTROL", "memshield", _m(leak=12.5))), fail_on="leak", min_recall=None)[0] == EXIT_FAIL
    canary = _m() | {"canary_exfiltration_pct": 12.5}
    assert gate(_report(("S1", "memshield", canary)), fail_on="leak", min_recall=None)[0] == EXIT_FAIL


def test_cli_eval_new_scenarios_pass_the_gate(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_main(["eval", "--repeats", "1", "--scenarios", "S7,S8,S9", "--format", "json",
                     "--targets", "unprotected,memshield,memshield_superuser", "--min-recall", "1.0"])
    out = capsys.readouterr().out
    report = json.loads(out[: out.rindex("}") + 1])
    assert code == EXIT_OK
    assert [s["id"] for s in report["scenarios"]] == ["S7", "S8", "S9"]
    for s in report["scenarios"]:
        assert s["targets"]["memshield"]["queries_leaking_pct"] == 0.0, s["id"]
        assert s["targets"]["unprotected"]["queries_leaking_pct"] > 0.0, s["id"]
        assert s["targets"]["memshield_superuser"]["configuration_refused"] is True
    s8 = next(s for s in report["scenarios"] if s["id"] == "S8")
    assert s8["targets"]["memshield"]["identity_refusals"] == len(PRESET_BY_ID["S8"].benchmark_queries) - 1
    assert s8["targets"]["memshield"]["authorized_recall_pct"] == 100.0  # the positive control
    assert "rls_delta_p50_ms" not in s8


# ----------------------------------------------------------------------------- isolation
def test_run_namespace_is_created_seeded_and_dropped() -> None:
    cfg = ArenaConfig()
    with run_namespace(cfg, ("unprotected", "memshield")) as (run_cfg, tset):
        schema = run_cfg.schema
        assert schema.startswith("arena_run_")
        data = asyncio.run(run_side_by_side_comparison(
            config=run_cfg, query="What is Alice's Q4 customer analytics dashboard deadline?",
            preset_id="CONTROL", failure_mode="control", targets=tset,
        ))
        assert data["memshield"]["verdict"]["recall_at_k"] == 1.0
    with psycopg.connect(cfg.admin_dsn) as conn:
        exists = conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()[0]
    assert exists == 0


@needs_qdrant
def test_qdrant_target_leaks_on_tenant_only_filter_and_isolates_on_control() -> None:
    cfg = ArenaConfig()
    with run_namespace(cfg, ("unprotected", "qdrant")) as (run_cfg, tset):
        leak = asyncio.run(run_side_by_side_comparison(
            config=run_cfg, query="What are the production AWS KMS failover recovery keys?",
            preset_id="S1", targets=tset,
        ))
        ok = asyncio.run(run_side_by_side_comparison(
            config=run_cfg, query="What is Alice's Q4 customer analytics dashboard deadline?",
            preset_id="CONTROL", failure_mode="control", targets=tset,
        ))
    assert leak["targets"]["qdrant"]["verdict"]["leaked_row_count"] > 0
    assert ok["targets"]["qdrant"]["verdict"]["leaked_row_count"] == 0
    assert ok["targets"]["qdrant"]["verdict"]["recall_at_k"] == 1.0


# ----------------------------------------------------------------------------- public mode
def test_public_mode_is_read_only() -> None:
    app = create_app(ArenaConfig(startup_baseline=False, public_mode=True, admin_dsn=""))
    with TestClient(app) as tc:
        assert tc.get("/api/state").json()["public_mode"] is True
        for path, body in (
            ("/api/reset", None),
            ("/api/scale", {"seed_size": 36}),
            ("/api/inject", {"acting_persona": "mallory_contractor", "claimed_owner_id": "mallory_contractor",
                             "scope": "private", "min_clearance": 1, "content": "hello world note"}),
            ("/api/benchmark/start", {}),
            ("/api/benchmark/kill", None),
            ("/api/benchmark", {}),
        ):
            resp = tc.post(path, json=body) if body is not None else tc.post(path)
            assert resp.status_code == 403, (path, resp.status_code, resp.text)
        assert tc.delete("/api/history").status_code == 403
        # Attacks (searches) still run, on the deterministic engine and PostgreSQL targets only.
        assert tc.post("/api/attack", json={"preset_id": "S1", "query": "What are the AWS KMS keys?"}).status_code == 200
        for body in (
            {"preset_id": "S1", "query": "hi there", "engine": "gemini_api"},
            {"preset_id": "S1", "query": "hi there", "targets": ["unprotected", "qdrant"]},
            {"preset_id": "S1", "query": "hi there", "targets": ["unprotected", "memshield_superuser"]},
            {"preset_id": "S6", "query": "How do I reset my SSO password?"},
            {"preset_id": "S7", "query": "Project Orion?"},
        ):
            assert tc.post("/api/attack", json=body).status_code == 403, body
        state = tc.get("/api/state").json()
        assert state["public_restrictions"]["blocked_presets"] == ["S6", "S7"]
        assert "applyPublicMode" in tc.get("/").text


# ----------------------------------------------------------------------------- deployment
ROOT = Path(__file__).resolve().parents[1]


def test_health_and_readiness_probes(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    broken = create_app(ArenaConfig(startup_baseline=False, db_dsn="postgresql://nobody:x@127.0.0.1:9/none"))
    with TestClient(broken) as tc:
        assert tc.get("/readyz").status_code == 503
        assert tc.get("/healthz").status_code == 200


def test_seed_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["seed"]) == EXIT_OK
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert isinstance(info, dict)


def test_public_mode_refuses_the_admin_login_and_bypassing_roles() -> None:
    from adk_leak_arena.server import PublicModeConfigurationError

    base = ArenaConfig()
    # Holding the admin DSN at all is refused ...
    with pytest.raises(PublicModeConfigurationError, match="ARENA_ADMIN_DSN"):
        with TestClient(create_app(ArenaConfig(startup_baseline=False, public_mode=True))):
            pass
    # ... and so is reading through a superuser (which also bypasses RLS and owns the tables).
    su = ArenaConfig(startup_baseline=False, public_mode=True, admin_dsn="", db_dsn=base.admin_dsn)
    with pytest.raises(PublicModeConfigurationError, match="superuser"):
        with TestClient(create_app(su)):
            pass


def test_public_mode_never_seeds_and_waits_for_the_seed_job() -> None:
    from adk_leak_arena.namespaces import create_run_schema, drop_run_schema

    base = ArenaConfig()
    schema = create_run_schema(base)  # an empty schema: nothing seeded there yet
    try:
        empty = base.for_schema(schema)
        cfg = ArenaConfig(startup_baseline=False, public_mode=True, admin_dsn="", db_dsn=empty.db_dsn)
        with TestClient(create_app(cfg)) as tc:
            assert tc.get("/healthz").status_code == 200
            ready = tc.get("/readyz")
            assert ready.status_code == 503 and "leak-arena seed" in ready.json()["detail"]
            assert tc.post("/api/attack", json={"preset_id": "S1", "query": "hello there"}).status_code == 503
        with psycopg.connect(base.admin_dsn) as conn:  # the public process created nothing
            tables = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = %s", (schema,)
            ).fetchone()[0]
        assert tables == 0
    finally:
        drop_run_schema(base, schema)


def test_web_deployment_is_single_replica_without_secrets() -> None:
    deploy = (ROOT / "deploy" / "base" / "deployment.yaml").read_text(encoding="utf-8")
    # Run history and the benchmark slot are in process memory: exactly one pod, never two.
    assert "\n  replicas: 1\n" in deploy and "type: Recreate" in deploy
    assert "/readyz" in deploy and "/healthz" in deploy and "runAsNonRoot: true" in deploy
    # The public web pod never gets the admin DSN; only the seed Job does.
    assert "ARENA_ADMIN_DSN" not in deploy.split("env:", 1)[1].split("readinessProbe", 1)[0].replace(
        "Public mode refuses to start with ARENA_ADMIN_DSN", "")
    assert "key: ARENA_ADMIN_DSN" in (ROOT / "deploy" / "base" / "seed-job.yaml").read_text(encoding="utf-8")
    for path in (ROOT / "deploy").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert "kind: Secret\n" not in text, path  # secrets are references only
        assert "postgres_admin_pass" not in text, path
    assert "deploy/overlays/dev/dev.env" in (ROOT / ".gitignore").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- prepared mode
def test_startup_option_is_percent_encoded() -> None:
    from adk_leak_arena.config import with_search_path
    from adk_leak_arena.prepared import _with_startup_option

    dsn = with_search_path("postgresql://u:p@h:5432/db", "arena_run_abc")
    out = _with_startup_option(dsn, "hnsw.iterative_scan=relaxed_order")
    assert "+" not in out.split("?", 1)[1]
    assert "options=-csearch_path%3Darena_run_abc%2Cpublic%20-chnsw.iterative_scan%3Drelaxed_order" in out


def test_prepared_mode_pins_custom_plans() -> None:
    from adk_leak_arena.prepared import run_prepared_mode

    report = run_prepared_mode(ArenaConfig(), seed_size=1000, repeats=12, top_k=10,
                               targets=("memshield", "memshield_unpinned"))
    rows = {(r["target"], r["shape"]): r for r in report["results"]}
    assert set(rows) == {("memshield", "default"), ("memshield_unpinned", "default"),
                         ("memshield", "rls_only"), ("memshield_unpinned", "rls_only")}
    for r in rows.values():
        assert r["leaked_rows"] == 0
        assert r["executions"] == 12 and r["explain"]["scan"]
    # memshield 0.2.0 pins force_custom_plan: PostgreSQL never switches to a generic plan.
    assert rows[("memshield", "default")]["generic_plans"] == 0
    assert rows[("memshield", "rls_only")]["generic_plans"] == 0
    assert rows[("memshield", "default")]["min_recall"] == 1.0
    # The statement really was prepared and re-planned (the counters are not trivially zero).
    assert rows[("memshield", "default")]["custom_plans"] > 0
    unpinned = rows[("memshield_unpinned", "rls_only")]
    assert unpinned["custom_plans"] + unpinned["generic_plans"] > 0
    with psycopg.connect(ArenaConfig().admin_dsn) as conn:
        left = conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (report["namespace"],)).fetchone()[0]
    assert left == 0


# ----------------------------------------------------------------------------- gemini pacing & retry
class _FakeApiError(Exception):
    def __init__(self, code: int, details: dict, message: str = "") -> None:
        super().__init__(message or f"{code} {details}")
        self.code = code
        self.details = details


def test_gemini_retry_info_parses_429_503_and_rejects_fatal() -> None:
    from adk_leak_arena.runner_pair import _gemini_retry_info

    # Wrapped 429 with both RetryInfo ('27s') and precise message ('27.8s').
    inner_429 = _FakeApiError(
        429,
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded.\nPlease retry in 27.8s.",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "27s"},
                ],
            }
        },
    )
    try:
        raise RuntimeError("adk wrapper") from inner_429
    except RuntimeError as wrapped:
        retryable, delay, reason = _gemini_retry_info(wrapped, 0)
    assert retryable is True
    assert reason == "HTTP 429"
    assert abs(delay - 28.8) < 1e-6

    # Daily quota 429 must not retry.
    daily_429 = _FakeApiError(
        429,
        {
            "error": {
                "code": 429,
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
                    }
                ],
            }
        },
    )
    assert _gemini_retry_info(daily_429, 0)[0] is False

    # 503 retries with exponential backoff.
    err_503 = _FakeApiError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})
    r0, d0, reason0 = _gemini_retry_info(err_503, 0)
    r1, d1, _ = _gemini_retry_info(err_503, 1)
    assert r0 and r1 and reason0 == "HTTP 503" and d1 > d0

    # 404 is fatal (no retry).
    err_404 = _FakeApiError(404, {"error": {"code": 404, "status": "NOT_FOUND"}})
    assert _gemini_retry_info(err_404, 0)[0] is False


def test_gemini_rate_limiter_paces_concurrent_callers() -> None:
    import time

    from adk_leak_arena.runner_pair import _GeminiRateLimiter

    limiter = _GeminiRateLimiter()

    async def _run() -> list[float]:
        stamps: list[float] = []

        async def _worker() -> None:
            await limiter.acquire(1200.0)  # 0.05s interval
            stamps.append(time.monotonic())

        await asyncio.gather(_worker(), _worker(), _worker())
        return sorted(stamps)

    stamps = asyncio.run(_run())
    assert stamps[2] - stamps[0] >= 0.08


def test_gemini_pipeline_retries_429_without_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from adk_leak_arena import runner_pair
    from google.adk.memory.base_memory_service import SearchMemoryResponse
    from google.genai import types

    runner_pair._GEMINI_PACE.reset()
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(runner_pair.asyncio, "sleep", _fake_sleep)

    attempts = {"count": 0}

    class _FakeEvent:
        def __init__(self, text: str) -> None:
            self.content = types.Content(role="model", parts=[types.Part(text=text)])

    class _FakeRunner:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        async def run_async(self, **_kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _FakeApiError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "message": "Please retry in 2.5s.",
                            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"}],
                        }
                    },
                )
            yield _FakeEvent("Verified Gemini answer after retry.")

    monkeypatch.setattr(runner_pair, "Runner", _FakeRunner)

    class _StubMemoryService:
        last_query_telemetry = None
        failure_mode = "control"

        async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
            return SearchMemoryResponse(memories=[])

    cfg = ArenaConfig(gemini_api_key="not-a-real-key", gemini_model="gemini-3.1-flash-lite", gemini_rpm=0.0)
    res = asyncio.run(
        runner_pair._run_single_adk_pipeline(
            target_key="memshield",
            target_label="adk-memshield (RLS)",
            memory_service=_StubMemoryService(),
            config=cfg,
            caller_tenant_id="acme_corp",
            caller_user_id="alice_analyst",
            caller_clearance=1,
            query="hello",
            engine="gemini_api",
            relevant_keys=None,
            ollama_status=None,
            db_posture={"ok": False},
            protected=True,
        )
    )
    assert attempts["count"] == 2
    assert res.engine_used == "gemini_api (gemini-3.1-flash-lite)"
    assert res.engine_fallback_reason is None
    assert "Verified Gemini answer after retry." in res.agent_output
    assert any(abs(s - 3.5) < 1e-6 for s in slept)


# ----------------------------------------------------------------------------- generic pre_write hook loader
def _dummy_block_hook(identity, pending):
    from adk_memshield.hooks import Block

    return Block("test-block")


def _dummy_widen_hook(identity, pending):
    from adk_memshield.hooks import Downlabel

    return Downlabel(scope="tenant_shared")


def test_pre_write_hook_loader_defaults_off_and_refuses_public_mode() -> None:
    from adk_leak_arena.config import load_pre_write_hook
    from adk_leak_arena.server import PublicModeConfigurationError, check_public_posture

    assert load_pre_write_hook(ArenaConfig()) is None

    pub_cfg = ArenaConfig(
        public_mode=True,
        admin_dsn="",
        pre_write_hook=f"{__name__}:_dummy_block_hook",
    )
    with pytest.raises(ValueError, match="public mode|ARENA_PUBLIC_MODE"):
        load_pre_write_hook(pub_cfg)
    with pytest.raises(PublicModeConfigurationError, match="ARENA_PRE_WRITE_HOOK"):
        check_public_posture(pub_cfg)


def test_pre_write_hook_tighten_and_widen_enforcement() -> None:
    from adk_memshield.hooks import PendingMemory, apply_downlabel
    from adk_memshield.identity import MemShieldIdentity
    from adk_leak_arena.config import load_pre_write_hook
    from adk_leak_arena.evaluator import run_full_benchmark

    cfg_block = ArenaConfig(pre_write_hook=f"{__name__}:_dummy_block_hook")
    hook = load_pre_write_hook(cfg_block)
    assert callable(hook) and hook.__name__ == "_dummy_block_hook"

    # S6 with a blocking pre_write hook drops memshield's same-tier poison retrieval to 0%.
    report = asyncio.run(
        run_full_benchmark(cfg_block, scenarios=("S6",), repeats=1, warmup=False)
    )
    s6_row = report["scenarios"][0]
    assert s6_row["unprotected"]["same_tier_poison_pct"] == 100.0
    assert s6_row["memshield"]["same_tier_poison_pct"] == 0.0

    # A widening decision loaded via the hook is refused by memshield's apply_downlabel.
    cfg_widen = ArenaConfig(pre_write_hook=f"{__name__}:_dummy_widen_hook")
    widen_hook = load_pre_write_hook(cfg_widen)
    pending = PendingMemory(text="private note", author="user", scope="private", min_clearance=1)
    ident = MemShieldIdentity(tenant_id="acme_corp", user_id="alice_analyst", clearance=1)
    with pytest.raises(ValueError, match="widen"):
        apply_downlabel(pending, widen_hook(ident, pending))
