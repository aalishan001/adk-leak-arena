"""Tests for the 0.2.0 features: targets, S7-S9, posture verdict, CI gate, public mode, namespaces."""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
    assert mctx["next_checkout_saw"].startswith("(empty")
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
    assert s8["targets"]["memshield"]["identity_refusals"] == len(PRESET_BY_ID["S8"].benchmark_queries)
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
    app = create_app(ArenaConfig(startup_baseline=False, public_mode=True))
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
        # Attacks (searches) still run.
        assert tc.post("/api/attack", json={"preset_id": "S1", "query": "What are the AWS KMS keys?"}).status_code == 200
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


def test_web_deployment_is_single_replica_without_secrets() -> None:
    deploy = (ROOT / "deploy" / "base" / "deployment.yaml").read_text(encoding="utf-8")
    # Run history and the benchmark slot are in process memory: exactly one pod, never two.
    assert "\n  replicas: 1\n" in deploy and "type: Recreate" in deploy
    assert "/readyz" in deploy and "/healthz" in deploy and "runAsNonRoot: true" in deploy
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
    with psycopg.connect(ArenaConfig().admin_dsn) as conn:
        left = conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (report["namespace"],)).fetchone()[0]
    assert left == 0
