# Contributing to adk-leak-arena

Thanks for helping improve the arena. Bug reports, new attack scenarios and new memory-service targets are all welcome.

## Development setup

You need Python 3.11+ and PostgreSQL 16 with `pgvector`. The test suite expects the database on `localhost:54330`.

```bash
git clone https://github.com/aalishan001/adk-leak-arena.git
cd adk-leak-arena
docker compose up -d db
pip install -e ".[dev]"
pytest -v
```

Or run everything inside the Compose stack:

```bash
docker compose up -d --build
docker exec adk_leak_arena_web pytest -v
docker exec adk_leak_arena_web leak-arena eval
```

The first `/api/reset` (or `leak-arena eval`) applies `sql/init_arena.sql` through the admin DSN. Override connection strings with `ARENA_DB_DSN` and `ARENA_ADMIN_DSN`.

## Adding an attack scenario

1. **Failure mode.** Add a value to `FailureMode` in `src/adk_leak_arena/unprotected_service.py` and implement the buggy application-layer filter in `UnprotectedPgVectorMemoryService`. The left target should reproduce a realistic mistake, not an artificial one.
2. **Preset and queries.** Add an `AttackPreset` to `ATTACK_PRESETS` in `src/adk_leak_arena/dataset.py` with 6–8 `EvalQuery` entries. List the seed keys each query targets so Authorized Recall@K can be scored; pure exfiltration probes list no authorized keys.
3. **Seed data.** If the scenario needs new rows, add them to `SEED_RECORDS` with canary tokens. `_validate_dataset()` checks the corpus at import time.
4. **Boundary or attack.** If RLS is *not expected* to stop the scenario (like S6), add it to `BOUNDARY_PRESETS` so it stays out of the S1–S5 aggregate.
5. **Tests.** Extend `tests/test_arena_e2e.py`: the unprotected target must leak, adk-memshield must not (or, for a boundary, both must behave identically), and recall eligibility must be correct.

## Adding a memory-service target

Targets are Google ADK `BaseMemoryService` implementations. Both panes are built in `run_side_by_side_comparison` in `src/adk_leak_arena/runner_pair.py`, and the seed path is `ensure_schema_and_seed`. A new target must:

- implement `add_memory` and `search_memory` and return `MemoryEntry` rows whose `custom_metadata` carries `tenant_id`, `user_id`, `scope`, `min_clearance` and `seed_key`, so the canary classifier can judge each row;
- expose the SQL it ran (or an equivalent trace) for the telemetry panel;
- connect as a non-superuser role so the comparison stays fair.

## Pull requests

- Keep changes focused and describe the failure mode or behaviour change in the PR.
- Run `pytest -v` and `leak-arena eval` before opening the PR, and paste the evaluation table if numbers change.
- Benchmark claims in the README must come from a run you can reproduce with the commands shown.
- No emojis in code, UI or docs (a test enforces this).

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
