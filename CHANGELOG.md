# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Hosted Cloud Run + AlloyDB Express deployment (`deploy/cloudrun/service.yaml`, `deploy/README.md`):** read-only public console (`ARENA_PUBLIC_MODE=1`, `minScale=0`, `maxScale=1`) with an AlloyDB Auth Proxy `1.18.0` sidecar connected as the non-bypass `memshield_app` role (dedicated least-privilege SA holding `arena-db-dsn` only). Verified live on AlloyDB Express PostgreSQL 18.4 (`asia-southeast1`): `/readyz` 200, S5 LEAK vs ISOLATED in 3.65 s cold / 203 ms warm, full `leak-arena eval` 97.5% (39/40) vs 0.0% (0/40) leaking and 100.0% recall (`benchmarks/runs/2026-11-19/{alloydb-posture,eval-alloydb-express,cloudrun-verify}.txt`).
- **Gemini engine pacing, retries, and recorded run:** `ARENA_GEMINI_RPM` (default `11.5`) paces requests across concurrent targets under free-tier limits; retries HTTP 429 (`RetryInfo` delay) and transient HTTP 503 with exponential backoff (`ARENA_GEMINI_MAX_RETRIES=6`). Verified on `gemini-3.1-flash-lite` (free tier, 108/108 live calls, 0 fallbacks, 0 HTTP 429s, 25 retried 503s; `benchmarks/runs/2026-11-19/eval-gemini.{txt,stderr.txt}`).
- **Optional `ARENA_PRE_WRITE_HOOK=module:attr` loader:** passes an external tighten-only `adk-memshield` `pre_write` hook into the `memshield` target (off by default, refused in `ARENA_PUBLIC_MODE=1`). Covered by 2 new tests in `tests/test_v020.py` (71 tests passing). Verified live on S6 with the private Gemini Memory Guard (`gemini-3.1-flash-lite`): same-tier poison retrieved on `adk-memshield` dropped from **100.0% to 0.0%** while keeping **100.0% authorized recall** (`benchmarks/runs/2026-11-19/eval-s6-guard-{off,on}.txt`).
- **Agent Skill (`skills/leak-arena-audit/`):** `agentskills.io`-format skill (`SKILL.md`, `references/scenarios.md`, `references/ci.md`) verified in Antigravity (10 of 10 trigger tests correct alongside `memshield-tenant-isolation`).

## [0.2.1] - 2026-11-17

### Security
- **A public console no longer holds the admin login.** With `ARENA_PUBLIC_MODE=1` the web process refuses to start if `ARENA_ADMIN_DSN` is set, or if its application role is a superuser, has `BYPASSRLS`, or owns an arena table (or belongs to its owner role). It never seeds: `/readyz` and the API return 503 until `leak-arena seed` has run with the admin DSN in a separate job. Public mode no longer falls back to a built-in admin DSN, and the startup baseline run is skipped.
- `deploy/`: the base web Deployment receives only `ARENA_DB_DSN`; `ARENA_ADMIN_DSN` goes to the seed Job alone. The dev overlay (public mode off) adds it back for the web Deployment. The Cloud Run instructions seed through a Cloud Run job that alone holds the admin DSN.

### Fixed
- A web process that cannot reach its database at startup now starts anyway (`/healthz` up, `/readyz` 503) and seeds on the next request, instead of failing to start.
- The "already seeded" flag is kept per database, not per process.

### Changed
- Manifests and docs point at the 0.2.1 image.
- PyPI keywords: `qdrant`, `mem0`.

## [0.2.0] - 2026-11-16

### Added
- **Memory targets beyond ADK.** A `MemoryTarget` interface (seed through the store's own write path, search under a caller identity, optional posture check), wrapped in an ADK `BaseMemoryService` so every target runs through the same `Runner` + `preload_memory` pipeline and canary classifier. Built-in targets: `unprotected`, `memshield`, `memshield_unpinned` (plan pinning off, the 0.1.x behaviour), `memshield_superuser` (posture demo), `qdrant` (Qdrant 1.20 payload filters, `pip install 'adk-leak-arena[qdrant]'`) and `mem0` (Mem0 OSS 2.2 raw adds over Qdrant, `[mem0]`). A mistake a store cannot express through its API is reported as **n/a**, never as 0%. Zep was not added: no maintained self-hostable server was found for this release.
- **N targets per run.** `--targets unprotected,memshield,qdrant,...` (CLI), a `targets` field on `/api/attack` and `/api/benchmark/start`, and extra target cards in the console. Targets run concurrently (`ARENA_TARGET_CONCURRENCY`, default 4). The default set is still `unprotected,memshield`.
- **Per-run isolation.** Each `leak-arena eval` (and each server benchmark with a non-default target set) runs in its own PostgreSQL schema and Qdrant collections, created at start and dropped at the end. This fixes the 0.1.0 limit where a CLI run during a server job picked up the server's S6 fixture rows. `--shared-db` keeps the old behaviour.
- **Scenarios for adk-memshield 0.2.0** (opt-in with `--scenarios S7,S8,S9` or `all`): S7 compartment read-across (the app filter omits the department check), S8 forged identity (the tool passes another user's id while a verified HMAC token is bound; memshield refuses, recorded as a refusal rather than an error; one query requests no other id as a positive control), S9 pooled session-context bleed (a baseline that binds identity with a session-level `SET` on a pooled connection, where a swallowed exception skips the rebind and the next request inherits the previous caller). memshield runs S7 and S9 with no application `WHERE` clause, so its RLS policy alone decides. New caller `orion_analyst` (compartment `project_orion`).
- **Posture verdict.** A target that refuses to run (memshield on a superuser DSN) is shown as `refused (config)`, not as a leak row.
- **CI gate.** `leak-arena eval` exits 1 when a protected target returns an unauthorized row or the agent repeats an unauthorized canary in an attack scenario or Control (the S6 boundary excluded), or its recall falls below `--min-recall`; and 2 on run errors, including a protected target that refused to run (except the `memshield_superuser` demo). `--fail-on none` restores the old always-0 behaviour for leaks.
- **Scale.** `--seed-size 10000` and `100000` (chunked COPY, then `ANALYZE`; Qdrant gets the same rows), `--hnsw` to build an HNSW index on both tables, and `leak-arena prepared`: a light user's query repeated on one pinned connection, recall@k per execution against the exact answer, PostgreSQL's generic/custom plan counters and the `EXPLAIN ANALYZE` plan.
- **Deployment.** Multi-arch (`linux/amd64`, `linux/arm64`) image on GHCR from the release workflow; `deploy/` with kustomize base, dev overlay (pgvector StatefulSet, Qdrant) and production overlay, validated with kubeconform; `/healthz` and `/readyz`; `leak-arena seed` for the seed Job; a documented Cloud Run command. The image runs as uid 10001.
- `ARENA_PUBLIC_MODE=1`: a read-only console (inject, reset, rescale, benchmark and history-delete endpoints return 403 and are hidden; attacks still run on the deterministic engine and the PostgreSQL targets, without the fixture-planting S6/S7).
- `docs/other-channel.md`: guarding inter-agent and model traffic with an ADK Runner plugin (`before_model_callback`, `before_tool_callback`), next to memshield's storage boundary.

### Changed
- Requires `adk-memshield>=0.2.0,<0.3`. The arena schema follows memshield 0.2.0 (compartments column and policies) and upgrades a 0.1.0 arena database in place.
- The default Gemini model is `gemini-3.8-flash`, GA on Vertex AI; `gemini-2.5-flash`, the 0.1.0 default, reached its scheduled retirement date on 20 Oct 2026 (Vertex AI model versions page, https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions, checked 16 Nov 2026). `GEMINI_MODEL` still overrides it. The console's running labels come from the server config instead of a hardcoded model name.
- The unprotected baseline's correct filter (Control, S6) now also checks compartments, matching the memshield policy.
- Searches memshield refuses on identity (S8) are excluded from aggregate and run-level latency; the scenario row keeps them and reports `identity_refusals`.
- Development status is now `3 - Alpha`; new classifiers and keywords.

### Fixed
- Scenario S3's description: the spoofed id never reaches memshield in S3 (ADK passes the session user); memshield without a verifier trusts the `user_id` it is given, which S8 now measures.
- The console caption and README no longer say adk-memshield returns "only the caller's own rows": it returns the rows the caller is entitled to (their own, plus tenant-shared rows at or below their clearance that are untagged or share a compartment with them).

## [0.1.0] - 2026-10-25

### Added
- Dual Google ADK `Runner` + `preload_memory` pipelines comparing an unprotected `pgvector` table with `adk-memshield` (PostgreSQL RLS) on the same seeded, canary-laced dataset.
- Scenarios S1–S5 (incidental overlap, adversarial top-K lure, tool-parameter spoofing, swarm clearance read-up, no-`WHERE` chaos mode), the S6 same-tier poisoning boundary, and a benign Control.
- Metrics: queries leaking, leaked row share, rank-1 hijack, canary exfiltration, same-tier poison retrieved, Authorized Recall@K, and p50/p95 SQL latency with RLS overhead.
- Offline web console with live SQL/RLS telemetry, stress dials (`top_k`, `seed_size`), an RLS write-policy sandbox, background benchmark jobs with abort, and run history.
- `leak-arena` CLI (`serve`, `eval`) with table, Markdown and JSON output.
- Arena schema bundled in the wheel for `pip install` usage.
