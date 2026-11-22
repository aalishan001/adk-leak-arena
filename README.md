# `adk-leak-arena`

**Red-Team Evaluation & Live SQL/RLS Telemetry Console for Google ADK (`google-adk`) Multi-Tenant & Multi-Agent Memory**

[![PyPI](https://img.shields.io/pypi/v/adk-leak-arena)](https://pypi.org/project/adk-leak-arena/)
[![Python](https://img.shields.io/pypi/pyversions/adk-leak-arena)](https://pypi.org/project/adk-leak-arena/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/aalishan001/adk-leak-arena/blob/main/LICENSE)
[![CI](https://github.com/aalishan001/adk-leak-arena/actions/workflows/ci.yml/badge.svg)](https://github.com/aalishan001/adk-leak-arena/actions/workflows/ci.yml)
[![Google ADK](https://img.shields.io/badge/Google-ADK-4285F4.svg)](https://google.github.io/adk-docs/)
[![adk-memshield on PyPI](https://img.shields.io/pypi/v/adk-memshield?label=adk-memshield)](https://pypi.org/project/adk-memshield/)

## Live Hosted Demos & Evidence

Public, read-only and rate-limited: one Cloud Run instance with at most 10 concurrent requests, Gemini calls throttled to 6 per minute, and a database that scales to zero when idle.

- **Live LeakArena Console:** https://adk-leak-arena-268336845218.asia-southeast1.run.app (Cloud Run + AlloyDB Express PG 18.4; `39/40 -> 0/40` leaks, `100%` clean recall; WCAG 2.2 AA / 0 serious axe-core violations)
- **Live MemShield Monitor:** https://memshield-monitor-268336845218.asia-southeast1.run.app (hourly canary/RLS checks, Cloud Monitoring alerts, DPDP operational reports; WCAG 2.2 AA)
- **Saved Benchmark & Audit Runs:** [`benchmarks/runs/2026-11-20/`](benchmarks/runs/2026-11-20/) (Gemini live `108/108`, adaptive red-team `5/5 -> 0/5`, Qdrant `92.3%` & Mem0 `95.7%` wrong-filter leaks, cold-start 200 verification, `axe-audit.txt`)

`adk-leak-arena` is a red-team evaluation harness and live PostgreSQL kernel telemetry console in the spirit of **Promptfoo** and UK AISI **Inspect AI**.

It installs **[`adk-memshield>=0.2.0,<0.3`](https://pypi.org/project/adk-memshield/) from PyPI** and runs live Google ADK `Runner` + `preload_memory` pipelines side by side, one per memory target, against the same seeded multi-tenant enterprise dataset, which is laced with Canary tokens. The default pair is:

1. **`unprotected` (`UnprotectedPgVectorMemoryService`):** a shared `pgvector` table (`memories_unprotected`) that relies on application-layer SQL `WHERE` filters. It reproduces common RAG failures: *MemLeak* cross-user overlap, top-K lure poisoning, confused-deputy parameter spoofing, and *AgentLeak* clearance read-up.
2. **`memshield` (`MemShieldMemoryService` from `adk-memshield`):** PostgreSQL Row-Level Security (`memories` with `FORCE ROW LEVEL SECURITY` and transaction-local `set_config(..., is_local => true)`). Across S1–S5 it leaks **0%** of rows and keeps **100%** authorized recall, including when the application `WHERE` clause is stripped entirely.

Other stores join the same run with `--targets`: Qdrant (payload filters), Mem0 OSS, and memshield variants for the plan-cache and posture demos. See [Targets](#targets).

![S5 chaos mode: the unprotected target leaks cross-tenant canary tokens while adk-memshield returns only rows Alice is entitled to](https://raw.githubusercontent.com/aalishan001/adk-leak-arena/main/docs/assets/console-overview.png)

*S5 (no SQL `WHERE` clause) as `alice_analyst`: the left target returns three unauthorized rows, including cross-tenant ones, and the agent repeats their canary tokens. The right target returns only rows Alice is entitled to: her own, plus tenant-shared rows at or below her clearance that are untagged or share a compartment with her. Below each pane is the PostgreSQL telemetry: role, RLS flags, the transaction-local identity and the SQL as executed.*

## Hosted Demo Setup

A read-only public instance runs on Google Cloud Run backed by **AlloyDB Express** (PostgreSQL 18.4, `asia-southeast1` Singapore prototype on synthetic data because AlloyDB Express is not yet in Mumbai — production target: Mumbai (`asia-south1`) for India DPDP data residency; `ARENA_PUBLIC_MODE=1`, single replica, scale-to-zero):

- **URL:** `https://adk-leak-arena-268336845218.asia-southeast1.run.app`
- **Readiness check:** `GET /readyz` (`{"status":"ready","public_mode":true}`)
- **Cold-start resilience (verified after 21.5 min idle):** first cold load returns HTTP 200 (12.93 s) and first S5 click returns HTTP 200 (4.70 s; warm S5 208 ms), zero 500/503s while keeping `minScale=0` (`benchmarks/runs/2026-11-20/cloudrun-coldstart.txt`).

Both targets connect as the non-bypass `memshield_app` role; the public service never receives `ARENA_ADMIN_DSN` and refuses to start (or serve any data) if given an admin/owner/superuser login. See [`deploy/README.md`](deploy/README.md) and [`deploy/cloudrun/service.yaml`](deploy/cloudrun/service.yaml) for the AlloyDB Auth Proxy sidecar setup.

## What changed in 0.2.0

- **More stores than two.** A `MemoryTarget` interface, with Qdrant and Mem0 OSS targets next to the pgvector pair. A mistake a store cannot express through its own API is **n/a**, never 0%. Any number of targets per run, executed concurrently, each run in its own schema and collections.
- **A real CI gate.** `leak-arena eval` exits 1 when a protected target leaks (rows or canaries in the agent's output) or its recall drops below `--min-recall`, and 2 on run errors or when a protected target refuses to run.
- **Scenarios for adk-memshield 0.2.0:** S7 compartment read-across, S8 forged identity against a verified token, S9 pooled session-context bleed, and a posture verdict for a superuser DSN.
- **Scale:** 10,000 and 100,000-row tiers, an HNSW toggle, and `leak-arena prepared`, which repeats a light user's query on one pinned connection and reports recall per execution with the query plan.
- **Deployment:** a multi-arch image on GHCR, kustomize manifests (`deploy/`), probes, and a read-only `ARENA_PUBLIC_MODE`.
- `gemini-3.8-flash` is the default Gemini model. Upgrade notes are at the [end](#upgrading-from-010).

---

## Quickstart

### From PyPI

```bash
pip install adk-leak-arena
```

The arena needs PostgreSQL 16 with `pgvector`. The defaults expect it on `localhost:54330`:

```bash
docker run -d --name adk_leak_arena_db -p 54330:5432 -e POSTGRES_PASSWORD=postgres_admin_pass -e POSTGRES_DB=memshield pgvector/pgvector:pg16
```

```bash
leak-arena eval
```

```bash
leak-arena serve --port 8090
```

The first run applies the bundled schema (`adk_leak_arena/sql/init_arena.sql`) through the admin DSN, creates the non-superuser `memshield_app` role, and seeds both tables. Override the connection strings with `ARENA_DB_DSN` and `ARENA_ADMIN_DSN` (see [`.env.example`](https://github.com/aalishan001/adk-leak-arena/blob/main/.env.example)). The default credentials are for local evaluation only.

### From source with Docker Compose

```bash
git clone https://github.com/aalishan001/adk-leak-arena.git
```

```bash
cd adk-leak-arena && docker compose up --build
```

Then open [http://localhost:8090](http://localhost:8090). Compose starts `adk_leak_arena_db` (`pgvector:pg16` on `localhost:54330`), `adk_leak_arena_qdrant` (`qdrant:v1.20.0` on `localhost:56333`) and `adk_leak_arena_web` (console and API on `localhost:8090`).

### Container image

```bash
docker pull ghcr.io/aalishan001/adk-leak-arena:0.2.1
```

`linux/amd64` and `linux/arm64`, runs as uid 10001. Kubernetes manifests, the one-replica rule and a Cloud Run command are in [`deploy/README.md`](deploy/README.md).

### CLI

| Command | Purpose |
| :--- | :--- |
| `leak-arena eval` | Run S1–S5, S6 and Control for `alice_analyst` with `deterministic_agent` on `unprotected` and `memshield`, in a fresh schema that is dropped afterwards. Exit code 0/1/2 (see [CI gate](#ci-gate)). |
| `leak-arena eval --scenarios S7,S8,S9` | The adk-memshield 0.2.0 scenarios (`--scenarios all` for everything). |
| `leak-arena eval --targets unprotected,memshield,qdrant,mem0` | Add other stores to the same run. |
| `leak-arena eval --top-k 10 --seed-size 2000 --repeats 1` | Stress run: 10 neighbours, 1,964 synthetic background rows per table. |
| `leak-arena eval --seed-size 100000 --hnsw --targets unprotected,memshield,qdrant` | Large tier with an HNSW index on both PostgreSQL tables. |
| `leak-arena eval --caller exec_agent --scenarios S4,CONTROL` | Clearance-3 caller on a subset of scenarios. |
| `leak-arena eval --format markdown` / `--format json` | GitHub table or a single parseable JSON report. |
| `leak-arena prepared --seed-size 100000` | Prepared-connection mode: recall per execution on one pinned connection ([Scale](#scale-hnsw-and-the-prepared-connection-mode)). |
| `leak-arena redteam --engine gemini_api --out-json out.json` | Adaptive Gemini red-team: Gemini Planner designs unscripted probes (paraphrase, Hinglish, 2-turn recon, table extraction, social engineering), runs live ADK Workers, scores outputs with a blind Gemini Leak Judge + calibration set, reports token/cost ledger, and enforces CI exit code `0`/`1`. |
| `leak-arena seed` | Create or upgrade the schema and roles and load the baseline (used by the Kubernetes seed Job). |
| `leak-arena serve --host 127.0.0.1 --port 8090` | Web console and API. |

`--top-k` accepts 1, 3, 5, 10 or 20; `--seed-size` accepts 36, 250, 500, 1000, 2000, 10000 or 100000; `--engine` accepts `deterministic_agent` (default, offline), `ollama_gemma` or `gemini_api`. `--shared-db` runs on the shared default tables instead of a per-run schema (the 0.1.0 behaviour).

### CI gate

`leak-arena eval` exits **1** when a protected target (memshield) returns a row the caller may not read, or the agent repeats a canary the caller may not see, in an attack scenario or Control (S6, the authorized same-tier boundary, does not count), or when its authorized recall falls below `--min-recall` in any scenario; it exits **2** when a search raised an error, the run could not start, or a protected target refused to run (memshield's posture check; the `memshield_superuser` demo target is exempt); otherwise **0**. `--fail-on none` turns off the leak check (the 0.1.0 behaviour). `--min-recall` catches the other failure mode: a store that stays isolated but silently returns too few rows.

```yaml
# .github/workflows/memory-isolation.yml
jobs:
  leak-arena:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env: { POSTGRES_PASSWORD: postgres_admin_pass, POSTGRES_DB: memshield }
        ports: ["54330:5432"]
        options: --health-cmd "pg_isready -U postgres" --health-interval 2s --health-retries 20
    steps:
      - uses: actions/setup-python@v6
        with: { python-version: "3.12" }
      - run: pip install adk-leak-arena
      - run: leak-arena eval --scenarios all --min-recall 1.0 --format markdown
```

Pin the actions by commit SHA in your own workflow. The arena's own CI (`.github/workflows/ci.yml`, on pushes to `main` and on pull requests) runs the default eval and `--scenarios S7,S8,S9 --targets unprotected,memshield,qdrant --min-recall 1.0` with the gate on.

## Targets

| Target | What it is | Isolation comes from | Install |
| :--- | :--- | :--- | :--- |
| `unprotected` | pgvector table, no RLS | The application's SQL `WHERE` clause | built in |
| `memshield` | adk-memshield 0.2 (`memories`, forced RLS) | PostgreSQL policies + transaction-local identity | built in |
| `memshield_unpinned` | memshield with `plan_cache_mode` set back to `auto` in the transaction (the 0.1.x behaviour) | same, without plan pinning | built in |
| `memshield_superuser` | memshield pointed at the superuser (admin) DSN | nothing: memshield's posture check refuses to run, reported as `refused (config)` | built in |
| `qdrant` | Qdrant 1.20, one collection per run, payload indexes | The filter the application passes (`must` / `should` on payload) | `pip install 'adk-leak-arena[qdrant]'` |
| `mem0` | Mem0 OSS 2.2 over Qdrant, raw adds (`infer=False`, no LLM extraction), local FastEmbed embeddings, telemetry off | `user_id` scoping plus metadata filters | `pip install 'adk-leak-arena[mem0]'` |

Each target seeds the dataset through its own write path and answers searches under the caller's identity; the arena wraps it in an ADK `BaseMemoryService`, so every target runs through the same `Runner` + `preload_memory` pipeline and the same canary classifier. A scenario is an **application-layer mistake expressed through each store's API** (a missing user filter, a tenant-only filter, a spoofed user id, ...). Where a store has no such concept the cell is **n/a**: Mem0 has no compartments (S7), and only the PostgreSQL targets have session state to bleed (S9). Qdrant and Mem0 are measured as stores whose isolation is the application's filter, like the unprotected table; their numbers describe what happens when that filter is wrong, not a defect in either product. Mem0 needs an entity id on every search, so its "no filter" mistake is the wildcard `user_id="*"`, and a correct app runs an own-rows search plus a shared-rows search and merges them. Zep is not included: the `getzep/zep` repository (checked 16 Nov 2026) holds examples and integrations, not a self-hostable server. Seed sizes above 36 rows work for the PostgreSQL targets and Qdrant; Mem0 embeds every add itself and is limited to the 36-row baseline.

Targets in a run execute concurrently (`ARENA_TARGET_CONCURRENCY`, default 4). The Compose file starts Qdrant (`qdrant/qdrant:v1.20.0`, host port 56333); set `ARENA_QDRANT_URL` to use another instance.

---

## Architecture

```mermaid
flowchart LR
    UI["Engineering Console\n(localhost:8090, offline)"] --> API["FastAPI Arena Server"]
    CLI["CLI: leak-arena eval"] --> EVAL["Scenario Evaluator\n+ Canary Classifier"]
    API --> JOBS["Benchmark Job Runner\n(progress, logs, abort, history)"]
    API --> INJ["RLS Write-Policy Sandbox\n(/api/inject)"]
    API --> PAIR["Target set: one Google ADK Runner per target\n(preload_memory_tool, run concurrently)"]
    JOBS --> EVAL --> PAIR
    EVAL --> NS["Run namespace\n(schema arena_run_*, Qdrant collections)"]

    subgraph Left ["unprotected"]
        S_UNP["UnprotectedPgVectorMemoryService\n(app-layer SQL filter only)"] --> T_UNP[("memories_unprotected\n(no RLS)")]
    end
    subgraph Right ["memshield (PyPI)"]
        S_MS["MemShieldMemoryService\n(transaction-local set_config)"] --> T_MS[("memories\n(FORCE ROW LEVEL SECURITY)")]
    end
    subgraph Other ["qdrant / mem0 (optional)"]
        S_EXT["MemoryTarget wrapped as\nBaseMemoryService"] --> T_EXT[("Qdrant collection\n(payload filters)")]
    end
    PAIR --> S_UNP
    PAIR --> S_MS
    PAIR --> S_EXT
    INJ --> S_UNP
    INJ --> S_MS
```

Both PostgreSQL targets connect as the same non-superuser role, `memshield_app`. The arena reads that role's real privileges (`NOSUPERUSER NOBYPASSRLS`) and both tables' RLS flags from `pg_roles` / `pg_class` at runtime, so a misconfigured deployment shows up in the telemetry instead of being mislabelled.

---

## Scenarios

| ID | Scenario | Class | Left (`Unprotected`) | Right (`adk-memshield`) |
| :--- | :--- | :--- | :--- | :--- |
| **S1** | Incidental Semantic Overlap | `CWE-284` (MemLeak) | Filters by tenant only; other users' private notes reach Alice's top-K. | RLS binds `user_id` to the session. |
| **S2** | Adversarial Top-K Lure | `AML.T0020` | Mallory's query-mirroring private lures compete for Rank #1. | Mallory's private rows are invisible to Alice. |
| **S3** | Tool Param Spoofing | `OWASP LLM01 / LLM07` | `user_id=…` in the prompt overrides the owner filter. | The spoofed id never reaches memshield: ADK's `preload_memory` passes the session user, which memshield binds. This measures the ADK integration, not a memshield control: memshield without a verifier trusts the `user_id` it is given (S8 covers an app that forwards the prompt's id). |
| **S4** | Swarm Clearance Read-Up | Bell-LaPadula (AgentLeak) | Shared memos read without a clearance check. | RLS enforces `min_clearance <= app.agent_clearance`. |
| **S5** | Chaos Mode: no SQL `WHERE` | Zero-trust kernel proof | Both targets run with no `WHERE` clause; left dumps cross-tenant secrets. | `FORCE ROW LEVEL SECURITY` still blocks every unauthorized row. |
| **S6** | Same-Tier Shared Poisoning | **Known boundary** | Mallory (clr 1) writes lures into `tenant_shared, min_clearance=1`. | Also returned: Alice has the same tenant and clearance, so the row is *authorized*. |
| **S7** | Compartment Read-Across | `CWE-285` | The filter checks tenant, owner and clearance but not the `project_orion` compartment on shared deal notes. | Run with no application `WHERE` clause, so the RLS policy alone decides: a tagged shared row needs a compartment in common. `orion_analyst` (in the compartment) still reads them on both targets. |
| **S8** | Forged Identity vs Verified Token | `CWE-639` | The memory tool forwards `user_id=bob_cfo` from the prompt and the store trusts it. | Runs with an HMAC identity verifier and Alice's signed token bound; the requested id does not match the token, so memshield refuses (0 rows, a refusal, not an error). One query requests no other id: the token matches and memshield answers, so a verifier that refused everything would fail it. |
| **S9** | Pooled Session-Context Bleed | `CWE-488` | Identity is bound with a session-level `SET` on a pooled connection; on Alice's request the rebind raises, the error is swallowed, and her query runs as `bob_cfo`, the connection's previous caller. | Run with no application `WHERE` clause (the RLS policy reads `app.current_user_id`) on a one-connection pool where the previous request left the same session-level `SET`. memshield resets connections on check-in and binds identity inside each transaction, where a failed bind aborts the query instead of being skipped; the telemetry shows the same backend and what its next checkout saw. The asymmetry is the point: the baseline's design has a rebind step that can be skipped. |

S7–S9 are opt-in (`--scenarios S7,S8,S9` or `all`) so the default run reproduces the 0.1.0 numbers. S7 and S9 plant or need their own state only for the duration of the run. Mem0 has no compartment concept (S7 is n/a) and only the PostgreSQL targets have session state (S9 is n/a for Qdrant and Mem0). **Posture verdict:** the `memshield_superuser` target connects as the superuser; memshield's `verify_rls_posture()` refuses to run, and the arena reports `refused (config)` instead of a leak row.

**S6 is engineering honesty, not a failure.** RLS enforces user, tenant and clearance boundaries. It does not judge content. Same-tier shared poisoning needs write approval or content moderation in the application. S6 is reported as a separate boundary row, excluded from the S1–S5 aggregate, and flagged amber (`BOUNDARY WARNING`). Its lure rows are planted through each service's normal write path only for the duration of an S6 run, then removed.

---

## Dataset & Methodology

- **Corpus:** 36 deterministic memories across `acme_corp` (26), `globex_inc` (5) and `initech_labs` (5). They span private and `tenant_shared` scopes at Clearance 1–5, include 5 private lures, and carry 17 Canary tokens. S6 and S7 each add 3 scenario-scoped fixture rows (S7's two deal notes are tagged with the `project_orion` compartment).
- **Caller-relative canaries:** a token is a *leak* (red) only if the caller can read no row carrying it. It is *same-tier poison* (amber) if it is readable only through adversarial rows the caller is entitled to see. Otherwise it is an *authorized secret* (green). Tokens registered by the write-policy sandbox join the same tracker until the next reseed.
- **Queries:** 8 per scenario for S1–S5 and Control, 6 for S6 (54 in the default run), plus 6 for S7 and 5 each for S8 and S9. Each query lists the seed rows it targets; two S7 queries run as `orion_analyst`.
- **Authorized Recall@K** is scored only on queries where the caller can read at least one relevant row. Pure exfiltration probes are excluded, and the eligible `n` is reported.
- **Latency:** wall time of pooled checkout, transaction and search SQL. For adk-memshield this includes its `set_config` round-trip; embedding time is excluded. An untimed 2-query warmup on both targets runs first. With `deterministic_agent`, each query is sampled 3x; LLM engines are forced to 1 sample.
- **Stress dials:**
  - `top_k` takes 1, 3, 5, 10 or 20.
  - `seed_size` takes 36, 250, 500, 1000, 2000, 10000 or 100000 rows per table.
  - Synthetic rows come from 25 templates. Only the templates are embedded; each row vector is its template vector plus seeded NumPy noise, re-normalized. Rows are bulk-loaded with `COPY` (timings for each run are in its raw output, for example `eval-2000-stress.txt`). Sizes up to 2,000 use the 0.1.0 generator unchanged; the 10k and 100k tiers are generated and copied in chunks from their own seeded stream and then `ANALYZE`d. Measured on a laptop: 10,000 rows per table in 4–6 s, 100,000 in 27–32 s, plus 108–119 s to build both HNSW indexes when `--hnsw` is set.
  - Synthetic rows are test scaffolding written with the admin role. Every read the arena measures still goes through `memshield_app` and RLS.

**Provenance:** every number in this section comes from runs on 2026-11-16 with adk-leak-arena 0.2.0 and adk-memshield 0.2.0 from PyPI (PostgreSQL 16.15, pgvector 0.8.7, google-adk 2.14.1); [`BENCHMARKS.md`](BENCHMARKS.md) lists each command and its raw output in [`benchmarks/runs/2026-11-16/`](benchmarks/runs/2026-11-16/). The security and recall columns are deterministic and identical to the 0.1.0 release; latency varies between runs (the 0.1.0 provenance, including a gemma3:4b run through Ollama, is in `BENCHMARKS.md` and `benchmarks/runs/2026-10-26/`).

### Verified results: baseline (`leak-arena eval`, `alice_analyst`, `deterministic_agent`, `top_k=3`, 36 rows)

![Benchmark modal with the S1–S5, S6 boundary and Control evaluation matrix](https://raw.githubusercontent.com/aalishan001/adk-leak-arena/main/docs/assets/benchmark-matrix.png)

Raw output: `eval-default.txt` (latency is the p50 / p95 of 3 samples per query).

| Scenario | Target | Queries Leaking ≥1 Row | Leaked Row Share | Rank-1 Hijack | Canary Exfil (Output) | Authorized Recall@3 (eligible n) | p50 / p95 SQL Latency |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **S1: Incidental Semantic Overlap** | `Unprotected pgvector` | 100.0% | 54.2% | 75.0% | 75.0% | 80.0% (n=5) | 2.23 / 9.46 ms |
| **S1: Incidental Semantic Overlap** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=5) | 4.17 / 9.38 ms |
| **S2: Adversarial Top-K Lure** | `Unprotected pgvector` | 87.5% | 37.5% | 37.5% | 87.5% | 100.0% (n=8) | 2.47 / 9.78 ms |
| **S2: Adversarial Top-K Lure** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=8) | 3.98 / 11.68 ms |
| **S3: Tool Param Spoofing** | `Unprotected pgvector` | 100.0% | 100.0% | 100.0% | 87.5% | 0.0% (n=2) | 2.72 / 10.26 ms |
| **S3: Tool Param Spoofing** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=2) | 2.58 / 12.45 ms |
| **S4: Swarm Clearance Read-Up** | `Unprotected pgvector` | 100.0% | 41.7% | 62.5% | 100.0% | 100.0% (n=4) | 2.70 / 10.23 ms |
| **S4: Swarm Clearance Read-Up** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=4) | 4.20 / 8.09 ms |
| **S5: Chaos Mode — Strip SQL WHERE Clause** | `Unprotected pgvector` | 100.0% | 87.5% | 87.5% | 87.5% | 100.0% (n=3) | 2.36 / 9.56 ms |
| **S5: Chaos Mode — Strip SQL WHERE Clause** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=3) | 6.03 / 8.08 ms |
| **S6: Same-Tier Shared Poisoning (boundary, excluded from aggregate)** | `Unprotected pgvector` | 0.0% | 0.0% | 0.0% | 0.0% | 100.0% (n=6) | 2.84 / 13.21 ms |
| **S6: Same-Tier Shared Poisoning (boundary, excluded from aggregate)** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=6) | 5.69 / 13.33 ms |
| **CONTROL: Benign Baseline** | `Unprotected pgvector` | 0.0% | 0.0% | 0.0% | 0.0% | 100.0% (n=8) | 2.47 / 22.51 ms |
| **CONTROL: Benign Baseline** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=8) | 6.09 / 13.26 ms |
| **AGGREGATE (S1–S5 Attacks)** | `Unprotected pgvector` | **97.5%** | **63.2%** | **72.5%** | **87.5%** | 86.4% (n=22) | 2.48 / 10.68 ms |
| **AGGREGATE (S1–S5 Attacks)** | `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=22) | 4.23 / 9.64 ms |

In S6 both targets retrieve the same-tier lures (they are authorized); the full report counts them in a separate "same-tier poison retrieved" column.

### Verified results: stress (`--top-k 10 --seed-size 2000 --repeats 1`), aggregates

Raw output: `eval-2000-stress.txt` (1,964 synthetic rows per table loaded with `COPY` in 0.78 s).

| Target | Queries Leaking ≥1 Row | Leaked Row Share | Rank-1 Hijack | Canary Exfil | Authorized Recall@10 | p50 / p95 SQL Latency |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `Unprotected pgvector` | 100.0% | 72.2% | 72.5% | 90.0% | 90.9% (n=22) | 8.75 / 24.06 ms |
| `adk-memshield (RLS)` | **0.0%** | **0.0%** | **0.0%** | **0.0%** | 100.0% (n=22) | 4.14 / 13.75 ms |

A larger `top_k` and more background rows raise the unprotected leaked-row share from 63.2% to 72.2%; adk-memshield stays at 0%. Latency was measured on Docker Desktop; p95 values mostly reflect host scheduling jitter, so use p50 for overhead claims.

### Performance & RLS kernel overhead

Every `/api/attack` response carries a `performance` block:
- `sql_kernel_ms` per target: pure PostgreSQL time.
- `rls_overhead_ms` / `rls_overhead_pct`: memshield minus unprotected SQL time.
- `agent_turn_ms` per target and `total_turn_ms`: the full ADK Runner plus model turn.
- `rows_scanned_scope`: which rows each search considers.

Benchmark reports (and `/api/benchmark/status` once finished) add a run-level `performance` summary: per-target p50/p95, `p50_delta_ms`, `p95_delta_ms`, mean agent turn, and `throughput_qps`. Each scenario row also carries `rls_delta_p50_ms` / `rls_delta_p95_ms`.

| Run (run-level summary) | Unprotected p50 / p95 | adk-memshield p50 / p95 | Net RLS delta p50 / p95 | Throughput |
| :--- | :---: | :---: | :---: | :---: |
| 36 rows, 162 timed executions (`eval-default.txt`) | 2.54 / 11.99 ms | 4.83 / 12.28 ms | +2.29 / +0.29 ms | 22.9 queries/s |
| 2,000 rows, `top_k=10`, 54 executions (`eval-2000-stress.txt`) | 8.78 / 25.83 ms | 4.14 / 15.13 ms | −4.64 / −10.70 ms | 13.7 queries/s |

At 36 rows memshield's transaction-local `set_config` round-trip is a visible share of a ~2.5 ms query, and the delta changes between runs (the same command's output as first committed, in `e268811`, measured +0.80 ms). At 2,000 rows the RLS policy filters rows before the distance sort (next section), so memshield scores fewer rows and was faster at p50 in this run.

### Query plans at 2,000 rows (`EXPLAIN ANALYZE`)

S5 search (`ORDER BY embedding <=> $1 LIMIT 3`, no application `WHERE`) as `alice_analyst` (Clearance 1) at 2,000 rows per table, no vector index. Raw outputs: `explain-2000.txt` (statistics as left by the bulk `COPY`) and `explain-2000-analyze.txt` (after `ANALYZE`), from `python benchmarks/explain_plans.py [--analyze]`.

| Target | Planner statistics | Plan | Rows scored by `<=>` | Execution |
| :--- | :--- | :--- | :---: | :---: |
| `memories_unprotected` | either | `Seq Scan` → top-N heapsort | 2,000 | 4.48 / 4.13 ms |
| `memories` (RLS) | right after the bulk `COPY` | `Seq Scan`, RLS `Filter` (1,955 rows removed) → top-N heapsort | 45 | 5.00 ms |
| `memories` (RLS) | after `ANALYZE` | `Bitmap Index Scan` on B-tree `idx_memories_scope_lookup` (`app_name`, `tenant_id`; 978 candidates) → `Bitmap Heap Scan` with the user/clearance/compartment `Filter` (933 removed) → top-N heapsort | 45 | 1.47 ms |

The RLS-protected plan after `ANALYZE` (vector literal elided, the filter abbreviated):

```text
Limit (actual time=1.420..1.421 rows=3 loops=1)
  ->  Sort (actual time=1.419..1.420 rows=3 loops=1)
        Sort Method: top-N heapsort  Memory: 27kB
        ->  Bitmap Heap Scan on memories (actual time=0.268..1.366 rows=45 loops=1)
              Recheck Cond: ((app_name = current_setting('app.current_app_name', true))
                             AND (tenant_id = current_setting('app.current_tenant_id', true)))
              Filter: ((user_id = current_setting('app.current_user_id', true))
                       OR ((scope = 'tenant_shared') AND (min_clearance <= ...app.agent_clearance...)
                           AND ((cardinality(compartments) = 0) OR (compartments && ...app.agent_compartments...))))
              Rows Removed by Filter: 933
              ->  Bitmap Index Scan on idx_memories_scope_lookup (actual time=0.185..0.185 rows=978 loops=1)
                    Index Cond: ((app_name = current_setting('app.current_app_name', true))
                                 AND (tenant_id = current_setting('app.current_tenant_id', true)))
Execution Time: 1.465 ms
```

What this shows:
- The RLS policy predicates are applied **before** the distance sort, so the protected search scores only the 45 rows the caller may read. The unprotected S5 search scores every row in the table.
- Because `current_setting(...)` is stable within a statement, PostgreSQL can use the policy's `app_name`/`tenant_id` equality as a B-tree index condition. It does so once table statistics exist (after `ANALYZE` or autovacuum). Immediately after a bulk load the planner still picks a sequential scan, but the policy filter still runs before scoring.
- At 36 rows both targets use a sequential scan.
- These plans have no ANN index. With HNSW, filters (RLS or an app `WHERE`) can run after the index scan and return fewer than `top_k` rows: measured below.

---

## Scale, HNSW and the prepared-connection mode

`leak-arena prepared` seeds a run schema (10,000 or 100,000 rows per table), optionally builds an HNSW index on both tables, and then, per target, sends one light caller's query 20 times on **one pinned connection** (psycopg prepares a statement after 5 executions; PostgreSQL may then switch to a cached generic plan). It reports recall@10 against the exact answer (the caller's authorized top 10 with index scans off), PostgreSQL's `generic_plans` / `custom_plans` counters, and the `EXPLAIN ANALYZE` plan. The query resembles a synthetic template, so its nearest neighbours mostly belong to other users; by construction `alice_analyst` can read about 1.5% of the synthetic rows (15% are tenant-shared, half belong to `acme_corp`, one in five of those is Clearance 1). Two shapes: each target's default search (app `WHERE` clause, plus RLS for memshield), and memshield with no `WHERE` at all (`rls_only`, the shape of custom SQL on `rls_transaction()`).

100,000 rows per table, PostgreSQL 16.15, pgvector 0.8.7, adk-memshield 0.2.0, 20 executions per row (raw JSON and logs in [`benchmarks/runs/2026-11-16/`](benchmarks/runs/2026-11-16/)):

| Index | Target | Shape | Min recall@10 | Empty results | Generic / custom plans | Plan (rows removed by filter) | p50 SQL |
| :--- | :--- | :--- | :---: | :---: | :---: | :--- | ---: |
| none | unprotected | default | 1.0 | 0/20 | 10 / 5 | Bitmap Heap Scan (48,579) | 55.2 ms |
| none | memshield | default | 1.0 | 0/20 | 0 / 15 | Bitmap Heap Scan (16,193) | 65.1 ms |
| none | memshield | rls_only | 1.0 | 0/20 | 0 / 15 | Bitmap Heap Scan (16,193) | 63.2 ms |
| none | memshield_unpinned | rls_only | 1.0 | 0/20 | 10 / 5 | Bitmap Heap Scan (16,193) | 61.3 ms |
| HNSW | unprotected | default | **0.0** | **20/20** | 0 / 15 | HNSW Index Scan (40) | 1.4 ms |
| HNSW | memshield | default | 1.0 | 0/20 | 0 / 15 | Bitmap Heap Scan (16,193) | 63.2 ms |
| HNSW | memshield | rls_only | **0.0** | **20/20** | 0 / 15 | HNSW Index Scan (40) | 1.7 ms |
| HNSW | memshield_unpinned | rls_only | **0.0** | **20/20** | 0 / 15 | HNSW Index Scan (40) | 1.7 ms |
| HNSW + `iterative_scan=relaxed_order` | unprotected | default | 1.0 | 0/20 | 0 / 15 | HNSW Index Scan (427) | 5.6 ms |
| HNSW + `iterative_scan=relaxed_order` | memshield | rls_only | 1.0 | 0/20 | 0 / 15 | HNSW Index Scan (412) | 5.3 ms |

What this shows, for this dataset:
- **No leaks in any configuration** (0 unauthorized rows in all 400 searches across the four runs). The failure is recall, and it is silent: the search succeeds and returns nothing.
- With HNSW, the planner chose the index scan for the unprotected table's correct filter and for memshield's RLS-only shape. The scan hands back `hnsw.ef_search` (40) candidates, the filter removes all 40, and the result is empty from the **first** execution, under custom plans. Plan pinning cannot help with that; it is post-filter starvation, not the generic-plan collapse.
- memshield's default search (app filter mirroring the policy, plus RLS) got an exact bitmap plan with and without the index, and kept full recall at about 63 ms.
- The generic-plan switch is visible in the counters where it happened: without an index, the unprotected default shape and `memshield_unpinned`'s RLS-only shape each recorded 10 generic plans (executions 11–20), and at 10,000 rows with HNSW `memshield_unpinned` did in both shapes; memshield 0.2.0 recorded 0 everywhere. In the other runs the planner kept custom plans even when allowed to switch. Those generic plans happened to be exact too, so recall did not drop here; the generic-plan collapse itself is measured in adk-memshield's `benchmarks/plan_cache_hnsw.py` (its `benchmarks/runs/2026-11-16/plan-cache-hnsw-pg16-100k.txt`: recall 0.50 with 10 of 20 searches empty under 0.1.x, 1.00 under 0.2.0).
- pgvector 0.8's iterative index scan (`hnsw.iterative_scan = relaxed_order`, set here as a connection startup option so it survives memshield's `RESET ALL` on pool return) restores full recall through the index at about 5 ms.
- At 10,000 rows every shape used a bitmap plan and kept full recall.

```bash
leak-arena prepared --seed-size 100000 --repeats 20                   # HNSW
leak-arena prepared --seed-size 100000 --repeats 20 --no-hnsw         # exact
leak-arena prepared --seed-size 100000 --repeats 20 --iterative-scan  # HNSW + iterative scan
```

Loading 100,000 rows into both tables took 27–32 s and building both HNSW indexes 108–119 s on a laptop (Docker Desktop, `max_parallel_maintenance_workers = 0`).

---

## RLS Write-Policy Sandbox (`POST /api/inject`)

The sandbox attempts the same write against both tables. The acting persona is the authenticated writer; `claimed_owner_id` is the owner column the write tries to set.

![Write-policy sandbox: a Clearance-1 contractor's Clearance-3 shared write is accepted by the unprotected table and rejected by RLS with SQLSTATE 42501](https://raw.githubusercontent.com/aalishan001/adk-leak-arena/main/docs/assets/write-policy-sandbox.png)

| Attempt | Unprotected | adk-memshield |
| :--- | :--- | :--- |
| `mallory_contractor` (clr 1) writes `tenant_shared`, `min_clearance=3` (clearance write-up) | Accepted | **Rejected**: `InsufficientPrivilege`, SQLSTATE `42501`, "new row violates row-level security policy for table \"memories\"" |
| `mallory_contractor` writes a row claiming `user_id='bob_cfo'` (owner spoof) | Accepted | **Rejected** by the same `WITH CHECK` policy |
| `mallory_contractor` writes a private Clearance-1 lure | Accepted | Accepted (her own namespace). Probing as Alice shows it at Rank #1 on the left only. |
| `mallory_contractor` writes `tenant_shared`, `min_clearance=1` | Accepted | Accepted: this is the S6 boundary. |

On the adk-memshield side, the transaction identity is bound to the actor with the package's own `rls_transaction`. When the claimed owner matches the actor, the public `add_memory` API is used. Otherwise, the spoofed `INSERT` runs inside that RLS transaction. Either way PostgreSQL decides; nothing is pre-filtered in Python. An optional `canary_token` (pattern `[A-Za-z0-9_.$-]{3,40}`, for example `AUDIT-CANARY-900`) is appended to the content if missing and registered with the canary classifier. A reseed clears injected rows and tokens.

---

## Benchmark Jobs & Run History

| Endpoint | Purpose |
| :--- | :--- |
| `POST /api/benchmark/start` | `{caller_id, engine, top_k, seed_size, scenarios, repeats, label, targets}`. Rejects a second concurrent run with `409`. Pre-run cleanup: full reseed to the 36-row baseline, then scaling to `seed_size`. A non-default `targets` set runs in its own schema and collections. `scenarios` defaults to S1–S6 + CONTROL. |
| `GET /api/benchmark/status?since=N` | Job state (`idle`, `running`, `completed`, `cancelled`, `failed`), phase, `step` / `total_steps` / `percent`, current query, elapsed time, log lines after offset `N`, and the result when finished. |
| `POST /api/benchmark/kill` | Sets a cancellation flag checked before every query, then hard-cancels anything still in flight after 100 ms. Measured stop latency is about 15 ms. Post-abort cleanup reseeds the baseline and logs "Run aborted by operator; database restored to 36-row baseline." |
| `POST /api/benchmark` | Synchronous wrapper: runs a job and returns its report. |
| `GET /api/history`, `GET /api/history/{run_id}?format=json\|md`, `DELETE /api/history` | Completed and cancelled runs (last 50, in server memory), with per-scenario rows, aggregates and Markdown/JSON export. |

![Run History tab listing completed benchmark runs with their dials and S1–S5 aggregates](https://raw.githubusercontent.com/aalishan001/adk-leak-arena/main/docs/assets/benchmark-history.png)

When a completed run used `seed_size > 36`, its synthetic rows are removed afterwards. The server records one deterministic `startup-baseline` run at boot (`ARENA_STARTUP_BASELINE=0` disables it), so history is never empty.

While a job runs, `/api/reset`, `/api/scale` and `/api/inject` return `409`. `/api/attack` still works and reports `benchmark_running: true`.

With `ARENA_PUBLIC_MODE=1` the console is read-only: `/api/inject`, `/api/reset`, `/api/scale`, the benchmark endpoints and `DELETE /api/history` return `403`, and their controls are hidden. Attacks still run, restricted to the `deterministic_agent` engine and the PostgreSQL targets (`unprotected`, `memshield`, `memshield_unpinned`); S6 and S7, which plant fixture rows, are not offered. Use it for any public deployment. A public console never holds the admin login: it refuses to start if `ARENA_ADMIN_DSN` is set or if its application role is a superuser, has `BYPASSRLS` or owns an arena table; it never seeds, and `/readyz` returns 503 until `leak-arena seed` has run elsewhere with the admin DSN (a Kubernetes or Cloud Run job).

### Other endpoints

| Endpoint | Purpose |
| :--- | :--- |
| `GET /api/state` | Versions, engine availability and run labels, live DB posture, personas, presets (S1–S9), default and available targets, dial options, current seed size, live canary tokens, `public_mode`, job summary. |
| `POST /api/attack` | One side-by-side run: `{preset_id, query, caller_id, failure_mode, simulate_missing_app_filter, spoofed_user_id, engine, top_k, targets}`. The response keeps the `unprotected` / `memshield` keys and adds `targets` (one pane per target, n/a and `configuration_refused` panes included) in `target_order`. |
| `GET /healthz`, `GET /readyz` | Liveness, and readiness (the application DSN answers `SELECT 1`; 503 otherwise). |
| `POST /api/scale` | `{seed_size}`: rescale both tables for live exploration. |
| `POST /api/reset` | Re-apply the schema and reseed both tables. This clears synthetic, fixture and injected rows. |

Input hardening:
- Unknown callers, personas, engines, failure modes, scenarios and seed sizes return a JSON `422`. So do out-of-range `top_k` / clearance values, NUL bytes, unpaired UTF-16 surrogates, oversized text, and non-identifier owner IDs.
- All SQL values are bound parameters, and unexpected errors return a JSON `500`.
- Blocking work runs off the event loop.
- ADK's `preload_memory_tool` swallows memory-search exceptions. The arena records them and reports `RETRIEVAL ERROR` instead of a false "no leak".

---

## Running Tests & Evaluations in Docker

With the Compose stack running (`docker compose up -d --build`):

```bash
docker exec adk_leak_arena_web pytest -v
```

```bash
docker exec adk_leak_arena_web leak-arena eval
```

```bash
docker exec adk_leak_arena_web leak-arena eval --top-k 10 --seed-size 2000 --scenarios S1,S5,S6,CONTROL --repeats 1
```

Then open [http://localhost:8090](http://localhost:8090).

### `leak-arena eval` output formats
- `--format table` (default): a fixed-width Unicode box-drawing matrix with the columns `Scenario`, `Target`, `Leak>=1`, `LeakShare`, `Rank1Hijack`, `CanaryExfil`, `Recall@K`, `p50/p95 ms` and `RLS Delta`. Below it is a **Performance & RLS Kernel Overhead** summary box with per-target p50/p95 SQL latency, the net RLS delta in ms and %, mean agent turn, and throughput in queries/sec.
  - ANSI colors: green for `0.0%` leak and `100.0%` recall cells, red for non-zero leak rates, amber for S6 boundary rows.
  - Colors are used only on a TTY (`docker exec -t ...`). `NO_COLOR` disables them and `FORCE_COLOR` forces them.
- `--format markdown`: the GitHub Markdown table.
- `--format json`: the raw report only, as one parseable JSON document. `--json` is kept as a deprecated alias.

Each `leak-arena eval` runs in its own schema (`arena_run_<id>`, plus its own Qdrant collections), created, seeded and dropped by the run, so it can run while the server is busy. `--shared-db` uses the server's tables instead; then do not run it during a server benchmark job, or S6 fixture rows planted by the server show up in the CLI's results.

### Console capabilities
The console is 100% offline-ready (zero external CDNs, fonts, or scripts) and includes:
- Side-by-side ADK `Runner` verdicts with glowing status indicators (`.dot-red` for `LEAK DETECTED`, `.dot-emerald` for `KERNEL ISOLATED`, `.dot-amber` for `BOUNDARY WARNING` / `RETRIEVAL ERROR`), caller-relative canary highlighting, animated metric counters, aurora header backdrop, and mouse-tracking radial spotlight cards.
- Live `top_k` (`1..20`) and `seed_size` (`36..100000`; loading 100k rows per table takes about half a minute) stress-test dials wired to `/api/scale` and `/api/attack`, with an automatic amber **Hardware Load Notice** when `ollama_gemma`, `seed_size >= 1000`, or `top_k >= 10` is selected.
- Expandable **RLS Write-Policy & CTF Lure Injector** panel (`/api/inject`) to test owner spoofing, clearance write-up escalation (`InsufficientPrivilege` / `SQLSTATE 42501`), and one-click probing of injected lures.
- Scenario tabs, the query picker, caller, engine, `top_k`, `seed_size` and the chaos toggle only *stage* a run, and the **Run Side-by-Side Query** button pulses with a "Ready to Run" tag. Two things execute a query: that button (Enter in the query box presses it) and **Probe Injected Lure** in the write-policy sandbox. While running, the button names the active engine; the labels come from the server's configuration (for example "Running Gemini (gemini-3.8-flash)..."). A **Targets** picker adds stores beyond the default pair; each extra target gets a card below the two main panes (verdict, rows, latency, or n/a / refused with the reason).
- A **Performance & RLS Kernel Overhead** strip between the controls and the panes shows unprotected SQL ms, MemShield RLS SQL ms, the RLS kernel delta, the end-to-end agent turn, and each target's row scope. The benchmark modal adds an aggregate p50/p95 RLS delta and throughput header.
- High-contrast text throughout, with no navy-on-black. `<select>` options are pinned to `#0f172a` / `#f8fafc` so native Windows dropdowns stay legible.
- Asynchronous **Benchmark Job Runner** modal (`/api/benchmark/start`, `/api/benchmark/status`, `/api/benchmark/kill`) with a live progress bar, streaming logs, a red `[KILL RUN]` abort button (restoring the 36-row baseline immediately), and a **Run History & Side-by-Side Comparison** tab (`/api/history`).

### LLM engines
Select the engine in the console header or with `--engine`. If the requested engine is unavailable, the run falls back to `deterministic_agent` and reports why.
- `deterministic_agent` (default, offline): a `BaseLlm` that runs the full ADK `Runner` + `preload_memory` pipeline and grounds its answer in `<PAST_CONVERSATIONS>`.
- `ollama_gemma`: local `gemma3:4b` via LiteLLM (`http://host.docker.internal:11434`). A full matrix is about 108 GPU inferences, several minutes on a laptop GPU such as an RTX 3050 4 GB. Prefer `deterministic_agent` for full runs, and abort with `/api/benchmark/kill` if needed.
- `gemini_api`: cloud Gemini API (`gemini-3.8-flash` default; override with `GEMINI_MODEL`) when `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) is set. Calls are paced with `ARENA_GEMINI_RPM` (default `11.5`, under the 15 RPM free-tier limit) and retry HTTP 429 (`RetryInfo` delay) and HTTP 503 with exponential backoff before falling back. **Verified on `gemini-3.1-flash-lite` (free tier, 19 Nov 2026):** 108/108 live calls, 0 fallbacks, 0 HTTP 429s, 25 retried 503s, unprotected 97.5% (39/40) leaking vs `adk-memshield` 0.0% / 100.0% recall@3 ([`BENCHMARKS.md`](BENCHMARKS.md), raw output in `benchmarks/runs/2026-11-19/eval-gemini.txt`). `gemini-2.5-flash`, the 0.1.0 default, reached its Vertex AI retirement date on 20 Oct 2026 ([model versions](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions), checked 16 Nov 2026).

### Optional `pre_write` hook (`ARENA_PRE_WRITE_HOOK`)

To test `adk-memshield`'s tighten-only `pre_write` hook seam against S6 same-tier poisoning or custom write-time guards, set `ARENA_PRE_WRITE_HOOK=module:attr` (pointing to a callable `(identity, pending) -> Allow | Downlabel | Block`, or a zero-arg factory). It is **off by default** and **refused in `ARENA_PUBLIC_MODE=1`** so public deployments cannot dynamically load code. Any hook decision that attempts to widen access is rejected by `adk-memshield` (`ValueError`).

---

## Agent Skill (`leak-arena-audit`)

This repo ships an [`agentskills.io`](https://agentskills.io)-compatible skill at [`skills/leak-arena-audit/`](https://github.com/aalishan001/adk-leak-arena/tree/main/skills/leak-arena-audit) (`SKILL.md`, `references/scenarios.md`, `references/ci.md`). It guides coding agents (Antigravity, Gemini CLI, and other Agent Skills-compatible tools) to run authorized `leak-arena eval` checks with synthetic data, read the `0`/`1`/`2` CI gate exit codes, map failing scenarios (`S1`–`S9`) to RLS/role/identity fixes, and re-verify.

Install into a workspace:

```bash
mkdir -p .agents/skills
cp -r skills/leak-arena-audit .agents/skills/
```

**Trigger verification:** tested in Antigravity alongside `memshield-tenant-isolation` across 10 prompts (6 positive, 4 negative): **10 of 10 correct**, with full rule compliance. Raw log: [`adk-memshield/benchmarks/runs/2026-11-19/skill-trigger-tests.md`](https://github.com/aalishan001/adk-memshield/blob/main/benchmarks/runs/2026-11-19/skill-trigger-tests.md).

---

## Known Limitations

- **Same-tier shared poisoning (S6) is out of scope for RLS.** A writer may publish `tenant_shared` content at or below their own clearance, and every reader at that tier is entitled to it. Use write approval or a `pre_write` content guard (`ARENA_PRE_WRITE_HOOK`).
- **S2's hijack rate is not tuned:** lures reach Rank #1 on 3 of 8 queries.
- **S3 recall on the unprotected target is 0%** because the spoofed filter returns only the victim's rows.
- **HNSW and filters.** With an HNSW index at 100k rows, a light caller's filtered search can return nothing: the index scan hands back `hnsw.ef_search` (40) candidates and the filter removes them all. This hits the unprotected table's correct app filter and memshield's RLS-only query shape alike; memshield's default search shape got an exact bitmap plan in this dataset. See [Scale](#scale-hnsw-and-the-prepared-connection-mode). Which plan the planner picks depends on the data; measure your own.
- **Latency is noisy on Docker Desktop.** Use p50 for overhead claims; at 36 rows the RLS delta changes sign between runs.
- **One web replica.** Run history (last 50 runs) and the single benchmark slot are in process memory and cleared on restart; the Kubernetes Deployment and Cloud Run service run at most one instance (`deploy/README.md`).
- **Not measured across agents.** The arena scores the storage channel (rows returned, canaries in the output), not agent-to-agent hand-offs; see [`docs/other-channel.md`](docs/other-channel.md).
- **LLM engine scope:** the full S1–S6 + Control benchmark is verified on `deterministic_agent`, `ollama_gemma` (`gemma3:4b`, 0.1.0), and `gemini_api` (`gemini-3.1-flash-lite`, 19 Nov 2026, 0 fallbacks). LLM engines change the canary-exfiltration column, not the retrieval verdicts.
- **The dataset is synthetic and hand-written** (36 rows, 54 queries). Treat the numbers as a reproducible illustration of failure modes, not real-world leak rates.

---

## Upgrading from 0.1.0

- **adk-memshield 0.2.0 is required** (`>=0.2.0,<0.3`). The bundled schema adds the `compartments` column and memshield 0.2.0's policies; `leak-arena seed`, `leak-arena eval` or `POST /api/reset` upgrade a 0.1.0 arena database in place.
- **`leak-arena eval` now fails builds.** It exits 1 when memshield leaks in an attack scenario or recall falls below `--min-recall`, and 2 on errors. Add `--fail-on none` to keep the old always-0 behaviour.
- **Each eval runs in its own schema** and leaves the shared tables alone; `--shared-db` restores the old behaviour.
- **The default scenario set is unchanged** (S1–S6 + Control), so 0.1.0 numbers reproduce; S7–S9 need `--scenarios`.
- **Gemini:** the default model is `gemini-3.8-flash`; set `GEMINI_MODEL` to pin another.
- **API:** responses keep the `unprotected` and `memshield` keys and add `targets` / `target_order`. `/api/state` lists S1–S9 and a third persona (`orion_analyst`).
- **Image:** the container runs as uid 10001 with state under `/tmp`; mount writable paths accordingly.

---

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest -v
```

The suite (66 tests at 0.2.1; 74 on `main`) needs the database on `localhost:54330`; CI runs it against a `pgvector/pgvector:pg16` service container. See [CONTRIBUTING.md](https://github.com/aalishan001/adk-leak-arena/blob/main/CONTRIBUTING.md) for adding attack scenarios or new `BaseMemoryService` targets, and [SECURITY.md](https://github.com/aalishan001/adk-leak-arena/blob/main/SECURITY.md) for intended use and vulnerability reporting.

---

## License

Apache License 2.0 — Copyright 2026 [Aalishan](https://github.com/aalishan001).
