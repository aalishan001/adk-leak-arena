---
name: leak-arena-audit
description: Use when asked to test, audit, or prove that an AI agent's memory does not leak data across tenants, users, compartments, or clearance levels, or when adding a memory-isolation check to CI.
license: Apache-2.0
compatibility: Requires Python 3.11+, Docker or PostgreSQL 16+ with pgvector, and adk-leak-arena >= 0.2.1.
metadata:
  author: aalishan001
  version: "1.0.0"
---

# AI Agent Memory Isolation Audit and CI Gate (`adk-leak-arena`)

Follow this procedure when auditing an AI agent's memory store for cross-tenant, cross-user, clearance, or session-bleed leakage, or when wiring a leak regression gate into CI.

## Instructions

1. **Confirm scope, authorization, and synthetic data.**
   - Only run attacks against systems the user owns or has explicit written authorization to test.
   - Always use synthetic canary data (such as `adk-leak-arena`'s built-in 36-row synthetic corpus), never production customer data.

2. **Run `leak-arena eval` with environment-supplied credentials.**
   - Keep `ARENA_DB_DSN` (application role: `NOSUPERUSER`, `NOBYPASSRLS`) and `ARENA_ADMIN_DSN` (schema owner for isolated run namespaces) in environment variables only; never echo, log, or commit them.
   - Default cross-boundary suite (S1–S6 + Control):
     ```bash
     leak-arena eval --format markdown
     ```
   - Full scenario and multi-backend suite (S1–S9 + Control across pgvector, Qdrant, Mem0):
     ```bash
     leak-arena eval --scenarios all --targets unprotected,memshield,qdrant,mem0 --min-recall 1.0 --format markdown
     ```
   - For a live Gemini verification run (never on a public web deployment), supply `GEMINI_API_KEY` and `GEMINI_MODEL` in the shell only, pace calls with `ARENA_GEMINI_RPM` (default `11.5`), and verify `0` fallback warnings in stderr.

3. **Read the CI gate exit code and posture verdicts.**
   - Exit `0`: **PASS** — 0 unauthorized rows and 0 leaked output canaries on every protected target across attacks and Control, with recall at or above `--min-recall`.
   - Exit `1`: **FAIL (leak or recall drop)** — at least one protected target leaked a forbidden row/canary or dropped below `--min-recall`.
   - Exit `2`: **ERROR (configuration refusal or retrieval error)** — a protected target refused its database posture (for example, superuser, `BYPASSRLS`, table owner, or missing `FORCE ROW LEVEL SECURITY`) or hit a runtime retrieval error.
   - Inspect `refused (config)` verdicts and `identity_refusals` (expected on S8 forged tokens).

4. **Map every failure to a concrete database or identity fix.**
   - Cite the exact failing scenario (see `references/scenarios.md`):
     - **S1 / S2 / S5** (missing/wrong `WHERE` or lure hijack): enforce `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY` on the memory table and connect via a `NOSUPERUSER NOBYPASSRLS` non-owner role.
     - **S3 / S8** (spoofed or forged identity): bind identity per request with `JwtIdentityVerifier` (`issuer=` required) or `HmacIdentityVerifier` via `bind_identity(token)`; never read `user_id` or `tenant_id` from prompt/tool arguments.
     - **S4 / S7** (clearance read-up or compartment cross-read): enforce `min_clearance <= app.agent_clearance` and compartment array overlap in the RLS `SELECT` and `INSERT`/`UPDATE` policies.
     - **S9** (pooled session bleed): bind identity with transaction-local `set_config(..., true)` inside an explicit transaction and configure `reset=adk_memshield.reset_connection` on connection pools.
     - **S6** (same-tier shared poisoning boundary): add a tighten-only `pre_write` guard hook (`MemShieldConfig(pre_write=...)`) to screen shared writes before storage.
     - **`refused (config)`**: switch the application DSN off the superuser/owner/`alloydbsuperuser`/`cloudsqlsuperuser` role and verify `relforcerowsecurity = true`.

5. **Re-run until the gate passes and wire it into CI.**
   - Re-run `leak-arena eval` until exit code is `0` with `0.0%` leaking and `100.0%` authorized recall on protected targets.
   - Save the command and raw output under `benchmarks/runs/<YYYY-MM-DD>/` (verifying `0` occurrences of any API key or password).
   - Add the CI gate workflow using `references/ci.md`.

## References (Load on Demand)
- `references/scenarios.md` — One-line summary of Scenarios S1–S9, Control, and exit codes.
- `references/ci.md` — GitHub Actions CI gate workflow snippet matching `adk-leak-arena`'s own CI.
