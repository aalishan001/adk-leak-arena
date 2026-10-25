"""Minimal repro: a NUL byte makes the memory search raise, and ADK's PreloadMemoryTool turns
that into an agent turn with empty memory context instead of an error.

Part 1 needs the arena database (run inside the web container).
Part 2 is pure google-adk: no database, no network, no API key.

    python NOTES/nul_byte_repro.py
"""

from __future__ import annotations

import asyncio
import logging
import traceback

# ---------------------------------------------------------------- part 1: where it raises
from adk_leak_arena.config import ArenaConfig
from adk_leak_arena.unprotected_service import UnprotectedPgVectorMemoryService

cfg = ArenaConfig()
svc = UnprotectedPgVectorMemoryService(
    dsn=cfg.db_dsn,
    failure_mode="param_spoof_user_id",  # S3: owner taken from the (attacker-supplied) tool arg
    spoofed_user_id="bob\x00cfo",
)
try:
    svc._search_memory_sync(app_name=cfg.app_name, user_id="alice_analyst", query="KMS keys")
except Exception as exc:
    print("part 1:", type(exc).__module__ + "." + type(exc).__name__, "-", exc)
    for frame in traceback.extract_tb(exc.__traceback__)[-3:]:
        print(f"    {frame.filename.split('site-packages/')[-1]}:{frame.lineno} in {frame.name}")

# ---------------------------------------------------------------- part 2: how ADK swallows it
from google.adk.agents import LlmAgent
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.preload_memory_tool import preload_memory_tool
from google.genai import types


class FailingMemory(InMemoryMemoryService):
    async def search_memory(self, *, app_name, user_id, query):
        raise RuntimeError("PostgreSQL text fields cannot contain NUL (0x00) bytes")


class RecordingLlm(BaseLlm):
    model: str = "recording-llm"
    saw_memory_block: bool = False

    async def generate_content_async(self, llm_request, stream=False):
        text = "".join(
            p.text or "" for c in llm_request.contents or [] for p in c.parts or []
        )
        self.saw_memory_block = "<PAST_CONVERSATIONS>" in text
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))


async def part2() -> None:
    llm = RecordingLlm()
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="repro", user_id="alice", session_id="s1")
    runner = Runner(
        app_name="repro",
        agent=LlmAgent(name="a", model=llm, instruction="answer", tools=[preload_memory_tool]),
        session_service=sessions,
        memory_service=FailingMemory(),
    )
    events = [
        e
        async for e in runner.run_async(
            user_id="alice",
            session_id="s1",
            new_message=types.Content(role="user", parts=[types.Part(text="KMS keys")]),
        )
    ]
    print(
        f"part 2: turn completed with {len(events)} event(s), no exception raised; "
        f"memory block in model prompt: {llm.saw_memory_block}"
    )


logging.getLogger("google_adk").setLevel(logging.ERROR)  # hide the warning; behaviour is unchanged
asyncio.run(part2())
