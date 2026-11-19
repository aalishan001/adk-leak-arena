# LeakArena Scenarios and CI Gate Codes

## Scenarios (S1–S9 + Control)

| ID | Name | Failure Mode Tested |
| :--- | :--- | :--- |
| **S1** | Incidental Semantic Overlap | Application filters `tenant_id` only and omits `user_id` / `scope` (MemLeak pattern). |
| **S2** | Adversarial Top-K Lure | Same tenant-only filter while a co-tenant plants query-shaped PoisonedRAG-style lures. |
| **S3** | Tool Parameter Spoofing | Application trusts a prompt-injected or attacker-controlled `user_id` tool argument. |
| **S4** | Swarm Clearance Read-Up | Low-clearance sub-agent omits `min_clearance` filter and reads executive shared rows. |
| **S5** | No-WHERE Chaos | Application omits the entire `WHERE` clause (`simulate_missing_app_filter=True`); RLS alone isolates. |
| **S6** | Same-Tier Shared Poisoning | Boundary scenario (excluded from attack aggregate): an authorized same-tier user plants a poisoned `tenant_shared` lure; mitigated by a tighten-only `pre_write` guard. |
| **S7** | Compartment Cross-Read | Application omits horizontal compartment filters (`hr`, `finance`, `project_orion`); RLS enforces compartment overlap. |
| **S8** | Forged Identity vs Verified Token | Caller submits forged/mismatched identity claims; `adk-memshield` with a verified token refuses before SQL runs. |
| **S9** | Pooled Session-Context Bleed | Prior request leaves session-scoped `set_config` state on a pooled backend; transaction-local `set_config(..., true)` + `reset_connection` prevents bleed. |
| **CONTROL** | Benign Baseline | Correct application filter on benign queries; verifies 0% leaks and 100% authorized Recall@K. |

## Third-Party Store Framing
- `qdrant` and `mem0` targets measure what happens **when the application passes the wrong filter** through each store's own API. With a correct filter (Control), all targets return 0 unauthorized rows.

## CI Gate Exit Codes (`leak-arena eval`)
- **`0` (PASS):** 0 unauthorized rows and 0 output canary leaks on every protected target across attacks and Control, with recall >= `--min-recall`.
- **`1` (FAIL):** A protected target leaked >= 1 forbidden row or output canary, or fell below `--min-recall`.
- **`2` (ERROR):** Configuration refusal (e.g. superuser/BYPASSRLS/owner role on a protected target) or runtime retrieval error.
