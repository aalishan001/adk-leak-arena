# The NUL byte that looked like a security pass

Rough notes, facts only. Times are UTC (IST = UTC+5:30). Sources:
- **[transcript]** the Claude Code session log for this repo (`05a570f6-…jsonl`, local, not in git);
- **[git]** this repo's history and the `fix/preload-memory-error-handling` branch of `aalishan001/adk-python`;
- **[PR]** google/adk-python#7723, read from the public PR page and the public GitHub REST API on
  2026-10-25 ~23:36Z (not with `gh`);
- **[run]** `benchmarks/runs/2026-10-26/nul-byte-repro.txt`, produced by [`nul_byte_repro.py`](nul_byte_repro.py).

Anything not backed by one of these is marked UNCONFIRMED.

## Timeline

| When (UTC) | What | Source |
| :--- | :--- | :--- |
| 2026-10-22 10:31:04 | Adversarial review of the then-uncommitted arena. A batch of malformed bodies was sent to `POST /api/attack` with curl (SQL injection strings, a NUL in `spoofed_user_id`, a NUL in `query`, a lone surrogate, bad enums, an oversized query). | transcript |
| 2026-10-22 10:31:10 | `{"preset_id":"S3","spoofed_user_id":"bob\u0000cfo"}` came back **HTTP 200**. | transcript |
| 2026-10-22 10:31:20–22 | The same request, re-sent and inspected, showed an unprotected pane with `engine_used=deterministic_agent`, **`total_rows=0`** and empty telemetry `session_context={}`. `docker logs adk_leak_arena_web` showed `raise ex.with_traceback(None)` / `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`. | transcript |
| 2026-10-22 10:31:37 | Read `google/adk/tools/preload_memory_tool.py` (google-adk 2.12.0 was installed): `search_memory` is wrapped in `try/except Exception`. | transcript |
| 2026-10-22 10:35–10:39 | Fixes written: a `RETRIEVAL ERROR` verdict (10:35), arena-side capture of search exceptions (10:37), input validation rejecting NUL and lone surrogates with 422 (10:38), regression test (10:39). | transcript |
| 2026-10-24 07:05:14–07:05:15 (12:35 IST) | First commits of the repo, dated event day (24 Oct), already contain all of the above: verdict in `fea8358`, capture in `d1c013c`, validation in `a8613ae`, tests in `f37ebb6`. | git |
| 2026-10-25 10:35:11 (16:05 IST) | Upstream fix committed on `fix/preload-memory-error-handling` (`b15755ea`). | git |
| 2026-10-25 10:59:30 | google/adk-python#7723 opened. | PR |

**Before or during the 24 Oct event:** before. Found on 22 Oct, two days earlier; the fix shipped in the
event-day commits. Nothing in git records the bug itself, because it never reached a commit.

## What was running

- Scenario **S3 (Tool Param Spoofing)**, failure mode `param_spoof_user_id`, caller `alice_analyst`,
  engine `deterministic_agent`, the S3 default query (no `query` in the body).
- Fuzz input: `spoofed_user_id = "bob\u0000cfo"`. In S3 the unprotected service puts the spoofed owner
  straight into its SQL (`WHERE … AND user_id = %s`), modelling a confused-deputy tool argument.
- The NUL in the `query` field (`"keys\u0000now"`) did **not** fail. Query text only goes to the embedder,
  never into a SQL text parameter, and that request returned normal results.

## The exception and where it was raised

- Message: `psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`.
- Raised client-side, before anything is sent to PostgreSQL. psycopg's text dumper refuses the parameter
  (`psycopg/types/string.py:52`, `_StrDumper.dump`: `if "\x00" in obj: raise DataError(...)`).
  - UNCONFIRMED: whether the pure-Python dumper or the equivalent C dumper from `psycopg[binary]` ran.
  - psycopg re-raises with `raise ex.with_traceback(None)`, so the inner frames are dropped.
- Call site: `UnprotectedPgVectorMemoryService._search_memory_sync`, at `cur.execute(sql, params)`.
  Today's repro shows `unprotected_service.py:385 in _search_memory_sync` then `psycopg/cursor.py:117 in execute`.
- The original 22 Oct traceback was only seen as its last lines in `docker logs`. That container has since
  been recreated, so the full original traceback is UNCONFIRMED.

## How PreloadMemoryTool turned it into empty context

In `PreloadMemoryTool.process_llm_request`:

```python
try:
  response = await tool_context.search_memory(user_query)
except Exception:
  logger.warning('Failed to preload memory (query length: %d)', len(user_query), exc_info=True)
  return
```

- On any exception it logs a warning and returns before inserting the `<PAST_CONVERSATIONS>` block.
- The model call then proceeds with no memory context. The turn completes normally, and nothing is raised
  to the Runner or the caller.
- Verified identical in the google-adk 2.12.0 wheel (installed on 22 Oct) and 2.13.0 (installed now).
- The `exc_info=True` warning is what produced the traceback in `docker logs` while the HTTP response was 200.

## How the harness ended up reporting "0 leaked rows"

The 22 Oct code, before the fix (from the transcript; it was never committed):

1. The arena wrapped `memory_service.search_memory` to capture retrieved rows. The wrapper did not catch
   exceptions: on failure it re-raised into ADK, ADK swallowed it, and the captured row list stayed empty.
2. The classifier ran on that empty list: `leaked_row_count=0`, `leak_detected=False`.
3. The pane badge was built from those values. With that code, an empty result renders as
   `KERNEL ISOLATED (0 rows leaked | 0 rows returned)`, on the **unprotected** side too (the badge text was
   not target-aware then).
   - What the transcript records directly is `total_rows=0` and empty telemetry.
   - The exact badge string as rendered is UNCONFIRMED; it is derived from the code at the time.

In short, a crash in the attack path looked the same as a clean, isolated result.

## Minimal repro

[`nul_byte_repro.py`](nul_byte_repro.py):
- Part 1 needs the arena database.
- Part 2 is pure google-adk: a memory service whose `search_memory` raises, a recording `BaseLlm`, the
  stock `preload_memory_tool` and a real `Runner`.

Command: `docker exec -i adk_leak_arena_web python - < NOTES/nul_byte_repro.py`, at commit `850c3f3`, google-adk 2.13.0.

```text
# started 2026-10-25T23:44:40Z | commit 850c3f3482508b0b592a9698b0991b1113a21af9 | command: docker exec -i adk_leak_arena_web python - < NOTES/nul_byte_repro.py
part 1: psycopg.DataError - PostgreSQL text fields cannot contain NUL (0x00) bytes
    <stdin>:27 in <module>
    /app/src/adk_leak_arena/unprotected_service.py:385 in _search_memory_sync
    psycopg/cursor.py:117 in execute
part 2: turn completed with 1 event(s), no exception raised; memory block in model prompt: False
```

## google/adk-python#7723

Read from the public page and REST API at about 2026-10-25T23:36Z.

- **Title:** "fix(tools): add raise_on_error and on_error callback to PreloadMemoryTool".
  Author `aalishan001`, head `aalishan001/adk-python:fix/preload-memory-error-handling` (`b15755ea`),
  base `main`. 1 commit, 2 files, +154 / −2.
- **What it changes** (`src/google/adk/tools/preload_memory_tool.py`). `PreloadMemoryTool.__init__` gains two
  keyword-only parameters:
  - `raise_on_error: bool = False`: re-raise the search exception, so the turn fails closed.
  - `on_error: Optional[Callable[[Exception, ToolContext], None]] = None`: called with the exception and the
    tool context on failure. If the callback itself raises, that is logged and the request proceeds as before.
  - The existing warning log is unchanged.
  - The defaults keep today's fail-open behaviour, including the `preload_memory_tool` singleton.
- **Tests added** (`tests/unittests/tools/test_preload_memory_tool.py`, +121 lines):
  - `test_preload_memory_search_failure_default_fails_open`
  - `test_preload_memory_raise_on_error_propagates_search_failure`
  - `test_preload_memory_on_error_receives_exception_and_context`
  - `test_preload_memory_on_error_not_called_on_success`
  - `test_preload_memory_on_error_called_before_raise`
  - `test_preload_memory_failing_on_error_callback_is_contained`
- **Test results reported in the PR description** (author's claim, NOT re-run for these notes):
  - 28 passing in the preload/load memory tool test files.
  - 3286 pass and 4 fail across `tests/unittests/tools`. The 4 failures are in `test_skill_toolset.py`
    and are described as also failing on unmodified `main` on Windows.
  - End-to-end not run.
- **Related issues:** #3069 and #6884. Their content was not read for these notes (UNCONFIRMED).
- **Status:**
  - Open, not draft, not merged. `mergeable_state: "unstable"`; which check makes it "unstable" is UNCONFIRMED.
  - Combined commit status `pending` with 0 statuses.
  - Labels: none. Reviews: none. Requested reviewers: none.
  - Assignee `xuanyang15`, assigned via adk-bot at 2026-10-25T11:00:07Z.
- **Comments:** none. 0 issue comments, 0 review comments, no bot comments on the PR thread.
- **Checks on `b15755ea`:** 8 check runs.
  - **`cla/google`: success** (2026-10-25T10:59:36Z, "All contributors are covered under a CLA with Google").
  - `header-check`: success. `check-changes`: success. `agent-triage-pull-request`: success.
  - Four `zizmor-*` checks: skipped.

## Does the arena now tell "crashed" apart from "0 leaked"?

**Per query: yes.**
- The arena wraps `search_memory`, records any exception, and sets `retrieval_error` on the verdict.
- The status becomes `error`, the badge reads `RETRIEVAL ERROR (memory search raised; verdict unavailable)`,
  the pane is amber instead of green, and recall is marked ineligible.
- Covered by `test_retrieval_error_is_surfaced_not_reported_as_isolated`.
- NUL bytes and lone surrogates are also rejected at the API with 422 before reaching SQL.
- This detection lives in the arena's wrapper, not in ADK: the arena still uses the stock fail-open
  `preload_memory_tool`.

**In aggregates: not fully.** This is still open.
- `queries_leaking_pct` and the leaked-row share count an errored query in the denominator as "not leaking".
- Errors are counted separately as `retrieval_errors`, which appears in the JSON report and in the web
  console's "Errors" column. It does **not** appear in the `leak-arena eval` box table or in the Markdown
  table.
- So a run where searches crashed could still print 0.0% leakage in those two formats.
- Every run behind the published numbers reported 0 retrieval errors (`benchmarks/runs/2026-10-26/query-ledger.txt`).
