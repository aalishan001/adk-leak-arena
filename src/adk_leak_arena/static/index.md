# MemShield suite, step 1: Live Red-Team Console (adk-leak-arena)

An interactive prototype on Cloud Run and AlloyDB Express (PostgreSQL 18.4). It runs the same agent-memory attack against two stores side by side: an unprotected pgvector table, where the application's WHERE clause is the only boundary, and adk-memshield, which enforces tenant, user and clearance isolation in PostgreSQL with FORCE ROW LEVEL SECURITY. A language model never touches the database: it answers only from the memories the agent puts into the prompt, so whatever the memory search returns can surface in the answer.

## How to use the page
1. Pick a scenario and a caller persona, then run the side-by-side query.
2. Scenario S5 strips the SQL WHERE clause on both sides: the unprotected store returns other tenants' rows, adk-memshield returns only what the caller may read.
3. The RLS write-policy injector tests owner spoofing, clearance write-up and lure writes against PostgreSQL WITH CHECK policies.
4. The benchmark runner repeats the scenario matrix at a chosen top_k and seed size, and keeps a run history you can compare.

## API (JSON)
- GET /api/state: database, model and target status, and the scenario catalogue
- POST /api/attack: run one scenario on both targets. Body: {"preset_id": "S5", "caller_id": "alice_analyst", "query": null}
- POST /api/inject: a write-policy test. Body: {"acting_persona", "claimed_owner_id", "scope": "private" or "tenant_shared", "min_clearance": 1 to 5, "content", "canary_token"}
- POST /api/scale (body {"seed_size"}), POST /api/reset: reseed, or back to the baseline dataset
- POST /api/benchmark/start, GET /api/benchmark/status, POST /api/benchmark/kill: the benchmark job
- GET /api/history, GET /api/history/{run_id}?format=md or ?format=json: saved runs
- GET /healthz, GET /readyz

The public demo runs in public mode on synthetic data; scenarios that plant fixture rows are refused there.

## Pages
- Step 1, this console: /
- Step 2, Continuous Monitor and DPDP reports: https://memshield-monitor-268336845218.asia-southeast1.run.app/
- Step 3, Architecture: /guide (markdown: /guide.md)

## Source
- https://github.com/aalishan001/adk-leak-arena
- https://github.com/aalishan001/adk-memshield
- Built by Aalishan: https://aalishan001.com
