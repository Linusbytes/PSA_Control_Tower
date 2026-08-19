"""Agent runtime — the orchestration loop every agent runs through.

One runtime, many brains and tools. This is the **linking layer** of the
software: a goal (or an operational event) comes in, the brain calls tools
(uniformly traced), mutating and execution-level actions are gated by the
autonomy level, and the whole run is recorded in the execution trace.

The same runtime drives the MCC planner today and will drive the LCL
deconsolidation, local-LCL-delivery, FCL and transloading agents tomorrow —
each is just a brain + a goal over the same shared tool registry and data
layer, so features stay linked through one cargo record.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import AGENTIC_AUTONOMY, DB_PATH
from data import store

from agents.brain import AgentBrain, BrainResult, default_brain
from agents.tools import APPROVAL, MUTATE, READ, call_tool, permission_of

# Autonomy levels (see config.py for the semantics of each).
ADVISORY = "advisory"
SEMI_AUTONOMOUS = "semi_autonomous"
AUTONOMOUS = "autonomous"
AUTONOMY_LEVELS = (ADVISORY, SEMI_AUTONOMOUS, AUTONOMOUS)


class AgentRuntime:
    """Orchestrates one agent run: brain + tools + gates + trace."""

    def __init__(
        self,
        brain: AgentBrain | None = None,
        store_path: Path | str = DB_PATH,
        autonomy: str = AGENTIC_AUTONOMY,
    ) -> None:
        self.brain = brain or default_brain()
        self.store_path = Path(store_path)
        if autonomy not in AUTONOMY_LEVELS:
            raise ValueError(f"autonomy must be one of {AUTONOMY_LEVELS}, got {autonomy!r}")
        self.autonomy = autonomy

    # --- permission gate -----------------------------------------------------

    def requires_approval(self, tool_name: str) -> bool:
        """Whether a tool call needs a human before it may execute, given the
        current autonomy level."""
        perm = permission_of(tool_name)
        if perm == APPROVAL:
            return self.autonomy != AUTONOMOUS
        if perm == MUTATE:
            return self.autonomy == ADVISORY
        return False  # READ tools are always allowed

    def gate(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Execute a tool through the runtime's permission gate.

        ``approval``-level tools (real-world execution) are queued for a human
        unless autonomy is ``autonomous``; ``mutate`` tools (plan proposals)
        are auto-applied in ``semi_autonomous``/``autonomous`` and queued in
        ``advisory``. Every outcome is recorded in the trace.
        """
        if self.requires_approval(tool_name):
            store.record_event(
                "runtime",
                "approval_required",
                {
                    "tool": tool_name,
                    "autonomy": self.autonomy,
                    "args": json.dumps(args, default=str)[:300],
                },
                self.store_path,
            )
            return {"status": "pending_approval", "tool": tool_name, "args": args}
        return call_tool(tool_name, args, self.store_path)

    def approve(self, tool_name: str, args: dict[str, Any]) -> Any:
        """A human approves a pending action; record and execute it."""
        store.record_event(
            "runtime",
            "approved",
            {"tool": tool_name, "autonomy": self.autonomy, "args": json.dumps(args, default=str)[:300]},
            self.store_path,
        )
        return call_tool(tool_name, args, self.store_path)

    # --- run loop -------------------------------------------------------------

    def run(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        history: list[dict] | None = None,
    ) -> BrainResult:
        """Run the brain against a goal, recording the run in the trace."""
        context = context or {}
        store.record_event(
            "runtime",
            "agent_run_start",
            {
                "goal": goal,
                "brain": self.brain.name,
                "autonomy": self.autonomy,
                "history": len(history or []),
            },
            self.store_path,
        )
        result = self.brain.run(goal, context, self.store_path, history=history)
        # Every pending proposal the brain returned lands in the trace as
        # approval_required — one recording site for rule-based and LLM brains,
        # so the AI-change lifecycle is always visible: proposal -> decision
        # (approved/rejected) -> executed tool_call.
        for ev in result.events or []:
            if (
                isinstance(ev, dict)
                and ev.get("kind") == "pending_approval"
                and ev.get("tool")
            ):
                store.record_event(
                    "agent",
                    "approval_required",
                    {
                        "tool": ev["tool"],
                        "args": json.dumps(ev.get("args") or {}, default=str)[:300],
                    },
                    self.store_path,
                )
        store.record_event(
            "runtime",
            "agent_run_end",
            {
                "goal": goal,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
            },
            self.store_path,
        )
        return result
