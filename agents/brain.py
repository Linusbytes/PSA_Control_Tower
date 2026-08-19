"""Agent brain abstraction — where the agentic AI API plugs in.

The **brain** is the decision engine behind the runtime. Today it is the
deterministic rule-based MCC planner (zero API, byte-for-byte reproducible).
The seam is ``AgenticAPIBrain``: it speaks the same ``AgentBrain`` protocol but
drives the external agentic AI API, passing it the tool registry's schemas and
executing its tool calls through ``call_tool``. Swapping brains is a config
change (set ``AGENTIC_API_ENDPOINT``) — nothing else in the stack changes.

``default_brain()`` picks the brain from config, so the runtime and the
dashboard never need to know which brain ran.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from config import (
    AGENTIC_API_ENDPOINT,
    AGENTIC_API_KEY,
    AGENTIC_API_MODEL,
    AGENTIC_MAX_TOOL_ITERATIONS,
    DB_PATH,
)
from data import store
from agents.tools import APPROVAL, MUTATE, call_tool, tool_schemas


@dataclass
class BrainResult:
    ok: bool
    summary: str
    error: str | None = None
    events: list[dict] = field(default_factory=list)


class AgentBrain(Protocol):
    """The one interface every decision engine implements.

    ``run`` receives the goal (what the agent is asked to achieve), a context
    snapshot (free-form: scenario state, user request, operational alert), and
    the data-store path. It returns a structured result; the runtime owns the
    trace and the approval gates.
    """

    name: str

    def run(
        self,
        goal: str,
        context: dict[str, Any],
        store_path: Path,
        history: list[dict] | None = None,
    ) -> BrainResult: ...


class RuleBasedBrain:
    """Deterministic MCC planning brain (the current default).

    Delegates to the rule-based planner. Deterministic and free, so demos are
    fully reproducible; every state read and plan write flows through the tool
    registry, so the trace is identical in shape to what the agentic AI API
    brain will produce.
    """

    name = "rule-based-mcc-v1"

    def run(
        self,
        goal: str,
        context: dict[str, Any],
        store_path: Path,
        history: list[dict] | None = None,
    ) -> BrainResult:
        from agents import mcc_planner  # lazy: avoid import cycle

        mcc_planner.plan(store_path)
        n = len(store.get_mcc_plans(store_path))
        o = len(store.get_outbound_containers(store_path))
        return BrainResult(
            ok=True,
            summary=f"Planned {n} inbound MCC containers and {o} outbound consolidation containers.",
        )


class AgenticAPIBrain:
    """Adapter for the external agentic AI API — **the seam**.

    Activated only when ``AGENTIC_API_ENDPOINT`` is configured. Until then it
    returns a clear not-configured error so the rest of the stack runs
    unchanged (``default_brain`` falls back to the rule-based brain).

    When configured, this brain runs a provider-agnostic agentic loop against
    an OpenAI-compatible endpoint:

    1. build a system prompt from the goal + a context snapshot,
    2. send ``tool_schemas()`` alongside the prompt (function calling),
    3. execute every tool call the model makes through ``call_tool``,
    4. honour permission gates — ``approval``-level tools are *not* executed
       here; they are returned as pending for the runtime/human to approve,
    5. loop until the model stops calling tools (bounded by
       ``AGENTIC_MAX_TOOL_ITERATIONS``), then return the final answer.

    Any agentic AI API that supports function calling (OpenAI Responses /
    Chat Completions, Claude Agent SDK, LangGraph, a PSA-internal API) can be
    adapted here — the loop below is the reference shape.
    """

    name = "llama"  # the agent's own name: the llama agent (runs via Ollama)

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.endpoint = endpoint or AGENTIC_API_ENDPOINT
        self.api_key = api_key if api_key is not None else AGENTIC_API_KEY
        self.model = model or AGENTIC_API_MODEL

    @property
    def configured(self) -> bool:
        return bool(self.endpoint)

    def _system_prompt(self, goal: str, context: dict[str, Any]) -> str:
        snapshot = json.dumps(context, default=str)[:4000]
        return (
            "You are the PSA iWX coordination agent for the Tuas Port / PSCH "
            "container freight station. You achieve the stated goal by calling "
            "the available tools."
            "\n\nDOMAIN VOCABULARY:"
            "\n- MCC = Multi-Country Consolidation: cargo from multiple shippers/"
            "countries is deconsolidated at PSCH, grouped by destination, and "
            "re-consolidated into outbound containers that must catch a specific "
            "vessel back to port. MCC is NOT 'merged' or 'mixed' container "
            "consolidation. Use this definition whenever the user asks what MCC "
            "means or what it stands for."
            "\n\nCONVERSATION:\nThe messages after this prompt are a conversation "
            "history followed by the user's latest question. Resolve references "
            "like 'it', 'its vessel', 'that container' or 'the same plan' against "
            "the conversation when the latest question alone is ambiguous."
            "\n\nGROUNDING RULES (strict):"
            "\n1. For any count, status or pipeline question, call "
            "get_terminal_snapshot FIRST and answer from its exact numbers."
            "\n2. Never invent numbers, totals or block-level counts. If a tool "
            "does not provide a figure, say the figure is not available rather "
            "than estimating or summing percentages."
            "\n3. Percentages (e.g. yard utilisation) are NOT container counts. "
            "Do not convert utilisation percentages into container totals."
            "\n4. Answer in plain, concise language and cite the tool you used."
            "\n5. You never execute real-world actions and never change plans "
            "directly: any change you propose (reassign_bin, "
            "reschedule_receiving_area, release_lane) is returned as a pending "
            "proposal for a human to approve. Never replace whole plan batches "
            "and never call the batch save tools."
            "\n\nOUTPUT FORMAT (strict):"
            "\n- No markdown headings (never '###' or '##'), no code fences "
            "(never ```), and no backticks around tool names — write tool names "
            "plainly, e.g. reschedule_receiving_area."
            "\n- Use short plain paragraphs or simple '-' bullet lines; bold "
            "(**...**) only for key figures, dates or container IDs."
            "\n- Never end with a courtesy question or closing like 'Would you "
            "like me to ...?', 'please let me know', or 'if you need ...'. End "
            "with the answer's final fact or a single concrete next step in "
            "one short line."
            f"\n\nGoal: {goal}\n\nContext snapshot:\n{snapshot}"
        )

    def run(
        self,
        goal: str,
        context: dict[str, Any],
        store_path: Path,
        history: list[dict] | None = None,
    ) -> BrainResult:
        if not self.configured:
            return BrainResult(
                ok=False,
                summary="Agentic AI API not configured",
                error=(
                    "Agentic AI API not configured: set AGENTIC_API_ENDPOINT "
                    "(optionally AGENTIC_API_KEY / AGENTIC_API_MODEL). Until then "
                    "the deterministic rule-based brain keeps the stack running."
                ),
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(goal, context)},
        ]
        # Conversation context: the chat history before this question (role/text
        # pairs from the PSA Intelligence thread), so follow-ups like "and what
        # about its vessel?" resolve against what was just discussed instead of
        # being answered in isolation.
        for m in history or []:
            if not isinstance(m, dict):
                continue
            role, text = m.get("role"), m.get("text")
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": str(text)})
        messages.append({"role": "user", "content": goal})
        pending_approvals: list[dict[str, Any]] = []

        for _ in range(AGENTIC_MAX_TOOL_ITERATIONS):
            payload = {
                "model": self.model or "default",
                "messages": messages,
                "tools": tool_schemas(),
            }
            reply = self._post(payload)
            message = reply["choices"][0]["message"]
            messages.append({"role": "assistant", "content": message.get("content") or ""})

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return BrainResult(
                    ok=True,
                    summary=message.get("content") or "Agent finished without a final message.",
                    events=pending_approvals,
                )

            for tc in tool_calls:
                fn = tc["function"]
                name, raw_args = fn["name"], fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
                if permission_of_tool(name) in (APPROVAL, MUTATE):
                    # Plan changes and real-world actions never execute inside
                    # the brain: they become pending proposals the runtime/
                    # human approves. This is the human-in-the-loop guarantee.
                    # (The runtime records the approval_required trace event so
                    # every brain — rule-based and LLM — traces identically.)
                    pending_approvals.append({"kind": "pending_approval", "tool": name, "args": args})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(
                                {"status": "pending_approval", "tool": name, "args": args}
                            ),
                        }
                    )
                    continue
                try:
                    result = call_tool(name, args, store_path)
                except Exception as exc:  # tool failure -> hand back to the model
                    result = {"error": str(exc)}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, default=str)[:3000],
                    }
                )

        return BrainResult(
            ok=False,
            summary="Agentic loop hit the tool-iteration limit",
            error=f"Exceeded AGENTIC_MAX_TOOL_ITERATIONS={AGENTIC_MAX_TOOL_ITERATIONS}",
            events=pending_approvals,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Agentic AI API returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Agentic AI API unreachable: {exc.reason}") from exc


def permission_of_tool(name: str) -> str:
    """Permission level of a tool, imported lazily to avoid a cycle at module load."""
    from agents.tools import permission_of

    return permission_of(name)


def default_brain() -> AgentBrain:
    """Pick the brain from config: the agentic AI API when configured, else the
    deterministic rule-based planner."""
    if AGENTIC_API_ENDPOINT:
        return AgenticAPIBrain()
    return RuleBasedBrain()


# The seam is documented in AGENTIC_AI_ARCHITECTURE.md; MUTATE is imported so the
# schema of a mutate tool is available to future agent brains without a second
# import site.
__all__ = [
    "AgentBrain",
    "AgenticAPIBrain",
    "BrainResult",
    "RuleBasedBrain",
    "default_brain",
    "MUTATE",
]
