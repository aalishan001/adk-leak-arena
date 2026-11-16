# The other channel: guarding inter-agent and model traffic

adk-memshield is a **storage boundary**. It decides which memory rows a search may return, inside PostgreSQL, for the identity bound to the transaction. Everything the arena measures happens at that boundary.

It does not see what happens to a row *after* it is returned: the text goes into a model prompt, may be passed to another agent, and may come back as a tool argument. That traffic is a second channel with its own failure modes:

| Channel | Example failure | Where it is enforced |
| --- | --- | --- |
| Storage (rows in the memory store) | A search returns another user's private note (S1-S5, S7, S9) | adk-memshield: RLS policy + transaction-local identity |
| Identity on the tool call | The model passes `user_id=bob_cfo` to the memory tool (S3, S8) | adk-memshield binds identity from the session or a verified token; a callback can also reject the argument |
| Model and agent traffic | An executive agent reads a Clearance-3 memo legitimately, then hands its text to a Clearance-1 sub-agent or a user-facing agent | **Not memshield.** An ADK callback or plugin on the Runner |

The third row is the gap this page is about. RLS cannot help there: the Clearance-3 agent was entitled to the row, and the downgrade happens in application memory.

## Which ADK hook fits

Checked against google-adk 2.14.1 (the version in the arena image). ADK offers agent callbacks (set on one `LlmAgent`) and **plugins** (`BasePlugin`, passed to `Runner(plugins=[...])`). Plugins run for every agent in the Runner and run before the agent's own callbacks; a plugin that returns a value short-circuits the remaining plugins and agent callbacks. For a security control that must hold for every agent, a plugin is the right container.

| Hook | Sees | Use it to |
| --- | --- | --- |
| `before_model_callback(callback_context, llm_request)` | The full `LlmRequest` about to go to the model: system instruction, session history from other agents, and the `<PAST_CONVERSATIONS>` block `preload_memory` injected | **Main guard.** Drop or redact content labelled above the receiving agent's clearance before the model sees it. Returning an `LlmResponse` skips the model call. |
| `before_tool_callback(tool, tool_args, tool_context)` | Arguments of every tool call, including an `AgentTool` call to another agent | Reject identity overrides (`user_id`, `target_user_id`) that differ from the session; stop a high-clearance agent from passing labelled text to a lower-clearance agent. Returning a dict skips the tool. |
| `after_tool_callback(tool, tool_args, tool_context, result)` | A tool's result, including what a sub-agent sent back | Label or redact a result before it re-enters the conversation. Returning a dict replaces the result. |
| `before_agent_callback` | The start of an agent's turn | Coarse allow/deny of a whole agent. It does not see individual messages, so it is the weakest fit here. |

`before_model_callback` is the one place every route converges: whatever path a piece of text took (memory, history, sub-agent transfer, tool result), it reaches the model through an `LlmRequest`.

## A sketch

The plugin below keeps each memory's labels next to its text and removes anything the receiving agent may not see. It relies on two application conventions: memshield's metadata (`scope`, `min_clearance`, `compartments`) is carried into the text the agent sees, and each agent's clearance is known to the application. Treat it as a starting point, not a drop-in control.

```python
from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.adk.plugins.base_plugin import BasePlugin

AGENT_CLEARANCE = {"exec_agent": 3, "support_agent": 1}
IDENTITY_ARGS = ("user_id", "target_user_id", "tenant_id")


class ClearanceGuardPlugin(BasePlugin):
    """Keeps labelled memory text from reaching agents below its clearance."""

    def __init__(self) -> None:
        super().__init__(name="clearance_guard")

    async def before_model_callback(self, *, callback_context, llm_request: LlmRequest):
        limit = AGENT_CLEARANCE.get(callback_context.agent_name, 1)
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if part.text:
                    part.text = redact_above(part.text, limit)  # your label parser
        return None  # continue to the model with the redacted request

    async def before_tool_callback(self, *, tool, tool_args: dict[str, Any], tool_context):
        # The model must never choose whose memory it reads (S3 / S8).
        if any(key in tool_args for key in IDENTITY_ARGS):
            return {"error": "identity arguments are not accepted; the session identity is used"}
        return None
```

Register it once on the Runner: `Runner(agent=root_agent, app_name=..., session_service=..., memory_service=..., plugins=[ClearanceGuardPlugin()])`.

What this does **not** do: it cannot tell whether a model paraphrased a secret it was entitled to see, and it trusts the labels it parses. Keep memshield as the storage boundary (it enforces labels the application cannot forget to apply), and treat the callback as the second layer for the traffic memshield never sees.

## What the arena measures today

The arena scores the storage channel: which rows each target returned and whether the agent's output repeated a canary. It does not yet score agent-to-agent hand-offs. That would need a multi-agent scenario (a Clearance-3 agent delegating to a Clearance-1 agent) and is not part of 0.2.0.
