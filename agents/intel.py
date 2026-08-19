"""Rule-based intelligence brain for the PSA Intelligence page.

The **PSA Intelligence** page lets a user ask anything about the live terminal
data ("where is SEAU9342928", "how many containers are at sea", "why is this
container in bin 1-12-2A", "what changed in the last hour"). Today a
deterministic ``IntelRuleBrain`` answers those questions by reading the same
tool registry the agentic AI API brain will use — so every answer is live,
auditable (each read lands in the execution trace) and free.

The seam is identical to the rest of the stack: ``default_intel_brain()``
returns ``AgenticAPIBrain`` the moment ``AGENTIC_API_ENDPOINT`` is configured,
and the page never changes — it just starts answering with the LLM. This file
is Phase 0 of ``AI_INTEGRATION_ROADMAP.md``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from config import sim_now
from agents.brain import AgentBrain, AgenticAPIBrain, BrainResult
from agents.tools import call_tool

# ISO 6346 container number: 4 letters + 7 digits (e.g. SEAU9342928).
CONTAINER_RE = re.compile(r"\b([A-Z]{4}\d{7})\b", re.IGNORECASE)

# Journey stages the planner derives (see agents/mcc_planner.py).
STAGES = ["En Route (Sea)", "Unloaded", "Depot", "En Route (Road)", "Arrived"]

FLOW_LABELS = {"mcc": "MCC", "lcl": "LCL", "fcl": "FCL", "topup": "Top Up", "transload": "Transload"}


class IntelRuleBrain:
    """Deterministic question-answering brain over the tool registry.

    Answers are plain text built from live reads through ``call_tool`` (same
    funnel and trace as the LLM brain). Unknown questions return a helpful
    capabilities note rather than a dead end.
    """

    name = "rule-based-intel-v1"

    def run(
        self,
        goal: str,
        context: dict[str, Any],
        store_path: Path,
        history: list[dict] | None = None,
    ) -> BrainResult:
        question = (goal or "").strip()
        try:
            answer, events = self._answer(question, store_path, history)
        except Exception as exc:  # never crash the page; explain instead
            answer = f"I hit an error answering that: {exc}"
            events = []
        return BrainResult(ok=True, summary=answer, events=events)

    # --- intent routing -------------------------------------------------------

    def _answer(self, q: str, path: Path, history: list[dict] | None = None) -> tuple[str, list[dict]]:
        ql = q.lower()
        cid = self._container_id(q)

        # 0. A request to CHANGE a plan -> propose the granular change (never
        #    execute; the page shows Approve / Reject).
        proposal = self._propose_change(q, path)
        if proposal is not None:
            return proposal

        # 1. A container number is mentioned -> track it (or explain its plan).
        if cid:
            plans = self._read("get_mcc_plans", path)
            p = next((x for x in plans if x["container_id"] == cid), None)
            if p is None:
                return f"I couldn't find container **{cid}** in the current MCC plan batch.", []
            if any(w in ql for w in ("why", "reason", "bin", "lane", "plan")):
                return self._plan_explanation(p, path), []
            return self._track(p, path), []

        # 1b. Conversational follow-ups: "and what about its vessel?", "where is
        #     it now?" — no container number of their own, so resolve the
        #     reference against the last container mentioned in the thread.
        followup = self._resolve_followup(q, history, path)
        if followup is not None:
            return followup

        # 2. Warehouse / PSCH facility questions.
        if any(w in ql for w in ("bin utilis", "occupan", "pallet", "receiving lane",
                                 "releasing lane", "stacker", "warehouse", "storage",
                                 "cold room", "ambient", "aisle")):
            return self._warehouse(q, path), []

        # 3. Flow-specific counts (MCC / LCL / FCL / Top Up / Transload) —
        #    checked before the generic pipeline counts so "how many top up
        #    jobs" routes to the flow answer, not the MCC pipeline.
        for flow, label in FLOW_LABELS.items():
            if label.lower() in ql or flow in ql:
                return self._flow(flow, label, q, path), []

        # 4. KPI / pipeline counts.
        if any(w in ql for w in ("how many", "count", "number of", "at sea", "arrived",
                                 "en route", "unloaded", "depot", "pipeline", "total")):
            return self._pipeline(q, path), []

        # 5. Vessel questions.
        vessels = self._read("list_vessels", path)
        vessel = self._match_vessel(q, vessels)
        if vessel is not None:
            return self._vessel(vessel, path), []

        # 6. Exceptions — what needs attention right now.
        if any(w in ql for w in ("exception", "attention", "at risk", "overdue",
                                 "cutoff", "delayed", "missed", "hold", "problem",
                                 "wrong", "alert")):
            return self._exceptions(path), []

        # 7. Outbound / consolidation / destination questions.
        if any(w in ql for w in ("outbound", "consolidat", "bound for", "destination",
                                 "vessel-bound", "back to port", "mcc outbound")):
            return self._outbound(q, path), []

        # 8. What happened recently.
        if any(w in ql for w in ("what changed", "recent", "trace", "history",
                                 "what happened", "last hour", "last 24")):
            return self._recent(q, path), []

        # 9. Capabilities / help.
        if any(w in ql for w in ("help", "what can you", "who are you", "capabilities",
                                 "what do you know", "what questions")):
            return self._help(), []

        return self._fallback(q), []

    # --- plan-change proposals (Phase 4: the agent takes action, with approval) --

    BIN_RE = re.compile(r"\b(\d+-\d{2}-\d+[A-C])\b", re.IGNORECASE)

    def _propose_change(self, q: str, path: Path) -> tuple[str, list[dict]] | None:
        """Detect a request to change a plan and return a pending proposal.

        The change is never executed here — the answer says "proposed, awaiting
        approval" and carries the tool + args in ``events`` so the page can
        render Approve / Reject buttons.
        """
        ql = q.lower()
        cid = self._container_id(q)
        if not cid:
            return None

        # (a) release an outbound lane -> release_lane (approval-level)
        if any(w in ql for w in ("release", "dispatch", "send out")) and any(
            w in ql for w in ("lane", "outbound", "loading")
        ):
            outbounds = self._read("get_outbound_containers", path)
            o = next((x for x in outbounds if x["container_id"] == cid), None)
            if o is None:
                return (
                    f"**{cid}** is not an outbound consolidation container — I can't "
                    "release its lane.",
                    [],
                )
            args = {"container_id": cid, "reason": q[:200]}
            return (
                f"**Proposed:** release the staging lane of **{cid}** now.\n\n"
                "This is a real-world action — waiting for your approval.",
                [{"kind": "pending_approval", "tool": "release_lane", "args": args}],
            )

        # (b) move / reassign to a bin -> reassign_bin
        if any(w in ql for w in ("move", "reassign", "change bin", "put ", "to bin",
                                 "relocate", "transfer")) and any(
            w in ql for w in ("bin", "aisle", "rack", "slot", "store")
        ):
            plans = self._read("get_mcc_plans", path)
            p = next((x for x in plans if x["container_id"] == cid), None)
            if p is None:
                return (
                    f"I couldn't find container **{cid}** in the current MCC plan batch.",
                    [],
                )
            m = self.BIN_RE.search(q)
            if m is None:
                return (
                    f"**{cid}** is currently planned to bin {p.get('bin_location')}. "
                    "Tell me the target bin (e.g. '5-8-1B') and I'll propose the move.",
                    [],
                )
            target = m.group(1).upper()
            if target == (p.get("bin_location") or "").removeprefix("Bin "):
                return f"**{cid}** is already planned to bin {p.get('bin_location')}.", []
            args = {"container_id": cid, "bin_location": target, "reason": q[:200]}
            return (
                f"**Proposed:** move **{cid}** from {p.get('bin_location')} to "
                f"**Bin {target}**.\n\n"
                "Waiting for your approval — the Storage page will show the new bin "
                "once approved.",
                [{"kind": "pending_approval", "tool": "reassign_bin", "args": args}],
            )

        # (c) change the receiving area / door -> reschedule_receiving_area
        if any(w in ql for w in ("receiving", "door", "unload")) and any(
            w in ql for w in ("change", "reschedule", "move", "switch", "swap")
        ):
            plans = self._read("get_mcc_plans", path)
            p = next((x for x in plans if x["container_id"] == cid), None)
            if p is None:
                return (
                    f"I couldn't find container **{cid}** in the current MCC plan batch.",
                    [],
                )
            area = p.get("receiving_area") or "—"
            m = re.search(r"\b(RA-\d+)\b", q, re.IGNORECASE)
            if m is None:
                return (
                    f"**{cid}** is currently scheduled to receive at {area}. "
                    "Tell me the target area (e.g. 'RA-4') and I'll propose the change.",
                    [],
                )
            target = m.group(1).upper()
            args = {"container_id": cid, "receiving_area": target, "reason": q[:200]}
            return (
                f"**Proposed:** reschedule **{cid}** to receive at **{target}** "
                f"(currently {area}).\n\n"
                "Waiting for your approval.",
                [{"kind": "pending_approval", "tool": "reschedule_receiving_area", "args": args}],
            )

        return None

    def _exceptions(self, path: Path) -> str:
        from agents.exception import find_exceptions, format_exceptions  # lazy

        return format_exceptions(find_exceptions(path))

    # --- conversational follow-ups (memory) --------------------------------------

    # Words that mark a question as a reference to something already discussed
    # (word-bounded so "utilisation" never trips the "it" check).
    _REF_RE = re.compile(r"\b(its|it|that|this|same|those|these|one)\b", re.IGNORECASE)
    _REF_PHRASES = (
        "what about", "how about", "and then", "tell me more", "more about",
        "follow up", "the vessel", "which vessel", "which ship", "the ship",
        "that container", "this container", "carried by", "its voyage", "its eta",
    )

    def _resolve_followup(
        self, q: str, history: list[dict] | None, path: Path
    ) -> tuple[str, list[dict]] | None:
        """Resolve a follow-up question against the last-mentioned container.

        Returns ``None`` when the question is not a reference (so normal intent
        routing proceeds). The thread is chronological, so walk it newest-first.
        """
        history = history or []
        if not history:
            return None
        ql = q.lower()
        last_cid: str | None = None
        for m in reversed(history):
            text = (m.get("text") or "") if isinstance(m, dict) else ""
            m2 = CONTAINER_RE.search(text)
            if m2:
                last_cid = m2.group(1).upper()
                break
        if last_cid is None:
            return None
        plans = self._read("get_mcc_plans", path)
        p = next((x for x in plans if x["container_id"] == last_cid), None)
        if p is None:
            return None

        # "its vessel / which ship is it on / carried by" -> the carrying vessel.
        if any(w in ql for w in ("vessel", "ship", "carried by", "voyage")):
            vessels = self._read("list_vessels", path)
            vid = (p.get("carrying_vessel_id") or "").lower()
            v = next(
                (x for x in vessels if (x.get("voyage_id") or "").lower() == vid),
                None,
            )
            if v is None:
                v = next(
                    (
                        x
                        for x in vessels
                        if (x.get("vessel_name") or "").lower()
                        == (p.get("carrying_vessel_name") or "").lower()
                    ),
                    None,
                )
            if v is not None:
                return self._vessel(v, path), []
            return (
                f"**{last_cid}** is carried by **{p.get('carrying_vessel_name')}** "
                f"({p.get('carrying_vessel_id')}) — I don't have a live track for that vessel.",
                [],
            )

        # A pronoun/reference follow-up about the same container.
        if self._REF_RE.search(q) or any(ph in ql for ph in self._REF_PHRASES):
            if any(w in ql for w in ("why", "reason", "bin", "lane", "plan")):
                return self._plan_explanation(p, path), []
            return self._track(p, path), []

        return None

    # --- helpers ---------------------------------------------------------------

    def _read(self, name: str, path: Path, args: dict[str, Any] | None = None) -> Any:
        """Read through the tool registry so the read lands in the trace."""
        return call_tool(name, args or {}, path)

    @staticmethod
    def _container_id(q: str) -> str | None:
        m = CONTAINER_RE.search(q)
        return m.group(1).upper() if m else None

    @staticmethod
    def _match_vessel(q: str, vessels: list[dict]) -> dict | None:
        ql = q.lower()
        best, best_len = None, 0
        for v in vessels:
            name = (v.get("vessel_name") or "").lower()
            if name and name in ql and len(name) > best_len:
                best, best_len = v, len(name)
        return best

    @staticmethod
    def _fmt(iso) -> str:
        """Format a datetime or ISO string as 'YYYY-MM-DD HH:MM'."""
        if iso is None:
            return "—"
        if hasattr(iso, "strftime"):
            return iso.strftime("%Y-%m-%d %H:%M")
        return str(iso).replace("T", " ")[:16] or "—"

    # --- answer builders ----------------------------------------------------------

    def _track(self, p: dict, path: Path) -> str:
        from agents.mcc_planner import journey_status  # lazy: avoid import cycle

        now = sim_now()
        status = journey_status(p, now)
        eta = self._fmt(p.get("psch_receipt_eta"))
        lines = [
            f"**{p['container_id']}** — journey status: **{status}**",
            f"- Carried by **{p['carrying_vessel_name']}** ({p['carrying_vessel_id']})",
            f"- Vessel destination: {p.get('vessel_destination') or '—'}",
            f"- PSCH receipt ETA: {eta}",
            f"- Receiving area: {p.get('receiving_area') or '—'}",
            f"- Putaway bin: {p.get('bin_location') or '—'} (stacker {p.get('putaway_robot') or '—'})",
            f"- Consolidation group: {p.get('consolidation_group') or 'not grouped yet'}",
            f"- Flow: {FLOW_LABELS.get(p.get('flow') or 'mcc', p.get('flow') or 'mcc')}",
        ]
        if status == "Arrived":
            lines.append("- The container is at PSCH now (its pallets are in / going into storage).")
        return "\n".join(lines)

    def _plan_explanation(self, p: dict, path: Path) -> str:
        from agents.mcc_planner import journey_status  # lazy: avoid import cycle

        now = sim_now()
        lines = [
            f"**{p['container_id']}** — how the plan was made (status: {journey_status(p, now)}):",
        ]
        reasoning = (p.get("reasoning") or "").strip()
        if reasoning:
            lines.append(reasoning)
        else:
            lines.append("No written reasoning was recorded for this container.")
        lines.append("")
        lines.append(
            f"The journey was derived from the carrying vessel's ETA: sea arrival "
            f"{self._fmt(p.get('sea_arrival'))} -> unload {self._fmt(p.get('unload_end'))} "
            f"-> depot {self._fmt(p.get('depot_arrive'))} -> road {self._fmt(p.get('road_depart'))} "
            f"-> PSCH {self._fmt(p.get('psch_receipt_eta'))}."
        )
        return "\n".join(lines)

    def _warehouse(self, q: str, path: Path) -> str:
        s = self._read("get_psch_space", path)
        stats = s["stats"]
        rooms = s["rooms"]
        lines = ["**PSCH warehouse (live)**"]
        for r in rooms:
            lines.append(f"- {r['label']} ({r['temp']}): {r['used']}/{r['cap']} bins, {r['pct']}% occupied")
        lines.append(
            f"- Bin utilisation overall: **{stats['bin_util']}%** "
            f"({stats['bins_used']}/{stats['bins_total']} bins, of which {stats['stock_bins']} are prior-wave dwell stock)"
        )
        lines.append(f"- Pallets planned (putaway): {stats['pallets_planned']}")
        lines.append(f"- Pallets in storage (arrived): {stats['pallets_in_storage']}")
        lanes = s["lanes"]
        lines.append(
            f"- Receiving lanes in use: {stats['lanes_rcv_used']}/{stats['lanes_rcv_total']} "
            f"· releasing lanes in use: {stats['lanes_rel_used']}/{stats['releasing_lanes']}"
        )
        lines.append(f"- Releasing groups staged: {stats['releasing_groups']}")
        lines.append(
            "Ask 'how is aisle 5 doing', 'which lane is OOLU9028993 staged on', "
            "or 'what is the cold room occupancy' for detail."
        )
        return "\n".join(lines)

    def _pipeline(self, q: str, path: Path) -> str:
        from agents.mcc_planner import journey_status  # lazy: avoid import cycle

        now = sim_now()
        plans = self._read("get_mcc_plans", path)
        counts: dict[str, int] = {}
        for p in plans:
            st = journey_status(p, now)
            counts[st] = counts.get(st, 0) + 1
        outbounds = self._read("get_outbound_containers", path)
        by_ob: dict[str, int] = {}
        for o in outbounds:
            by_ob[o["status"]] = by_ob.get(o["status"], 0) + 1
        lines = [f"**Containers in the MCC pipeline: {len(plans)}**"]
        for st in STAGES:
            lines.append(f"- {st}: {counts.get(st, 0)}")
        if outbounds:
            lines.append("")
            lines.append(f"**Outbound consolidation: {len(outbounds)}**")
            for st in sorted(by_ob):
                lines.append(f"- {st}: {by_ob[st]}")
        lines.append(
            "Tip: ask 'how many at sea', 'how many arrived at PSCH', or 'how many are delayed'."
        )
        return "\n".join(lines)

    def _vessel(self, v: dict, path: Path) -> str:
        status = v.get("status") or "unknown"
        lines = [
            f"**{v.get('vessel_name')}** ({v.get('voyage_id')}) — **{status}**",
            f"- Berth: {v.get('berth_id') or 'not assigned'}",
            f"- ETA at berth: {self._fmt(v.get('eta'))}",
            f"- ETD: {self._fmt(v.get('etd'))}",
            f"- Destination: {v.get('destination') or '—'}",
        ]
        if v.get("distance_nm") is not None:
            lines.append(f"- {v.get('distance_nm')} nm out at {v.get('speed_knots')} kn")
        if v.get("moves_planned"):
            lines.append(f"- {v.get('moves_planned')} planned moves")
        containers = self._read("list_containers", path)
        onboard = [
            c["container_id"]
            for c in containers
            if c.get("voyage_id") == v.get("voyage_id")
        ]
        lines.append(f"- Containers on this voyage: {len(onboard)}")
        if onboard:
            lines.append("  " + ", ".join(onboard[:12]) + (" …" if len(onboard) > 12 else ""))
        return "\n".join(lines)

    def _flow(self, flow: str, label: str, q: str, path: Path) -> str:
        plans = self._read("get_mcc_plans", path)
        subset = [p for p in plans if (p.get("flow") or "mcc") == flow]
        lines = [f"**{label} flow — {len(subset)} container(s)**"]
        if flow == "mcc":
            lines.append(
                "MCC = **Multi-Country Consolidation**: cargo from multiple shippers/countries "
                "is deconsolidated at PSCH, grouped by destination, and re-packed into outbound "
                "containers that must catch a specific vessel back to port."
            )
            lines.append(
                "Ask 'which MCC containers are bound for Antwerp' or 'show the MCC consolidation schedule'."
            )
        else:
            dests: dict[str, int] = {}
            for p in subset:
                d = p.get("destination") or p.get("vessel_destination") or "—"
                dests[d] = dests.get(d, 0) + 1
            for d, n in sorted(dests.items()):
                lines.append(f"- {d}: {n}")
        return "\n".join(lines)

    def _outbound(self, q: str, path: Path) -> str:
        outbounds = self._read("get_outbound_containers", path)
        ql = q.lower()
        dest = None
        for token in ("bound for", "to "):
            if token in ql:
                rest = ql.split(token, 1)[1].strip().strip("?")
                if rest:
                    dest = rest
                    break
        subset = outbounds
        if dest:
            subset = [o for o in outbounds if dest in (o.get("destination") or "").lower()]
        if not subset:
            return "No outbound consolidation containers match that question."
        lines = [f"**Outbound consolidation containers: {len(subset)}**"]
        for o in subset[:20]:
            lines.append(
                f"- {o['container_id']} -> {o['destination']} · bound {o['bound_vessel_name']} "
                f"· {o['status']} · ETA loading {self._fmt(o.get('eta_loading_area'))}"
            )
        if len(subset) > 20:
            lines.append(f"… and {len(subset) - 20} more.")
        return "\n".join(lines)

    def _recent(self, q: str, path: Path) -> str:
        events = self._read("get_trace_events", path, {"limit": 25})
        if not events:
            return "The execution trace is empty."
        lines = ["**Recent execution trace (newest first)**"]
        for e in events:
            ts = self._fmt(e.get("ts"))
            detail = e.get("detail") or {}
            summary = ""
            if e.get("event") == "tool_call":
                summary = f"tool {detail.get('tool')} ({detail.get('permission')})"
            elif e.get("event") == "agent_run_end":
                summary = f"agent run {'ok' if detail.get('ok') else 'failed'} — {str(detail.get('summary'))[:80]}"
            elif e.get("event") == "approval_required":
                summary = f"approval required for {detail.get('tool')}"
            elif e.get("event") == "approved":
                summary = f"approved {detail.get('tool')}"
            lines.append(f"- {ts} [{e.get('actor')}] {e.get('event')}: {summary}")
        return "\n".join(lines)

    def _help(self) -> str:
        return (
            "I'm **PSA Intelligence** — I answer questions about the live terminal "
            "from the same data the Control Tower shows.\n\n"
            "**MCC = Multi-Country Consolidation**: cargo from many shippers/countries "
            "is deconsolidated at PSCH, grouped by destination, and re-packed into "
            "outbound containers that must catch a specific vessel back to port.\n\n"
            "**Ask me about:**\n"
            "- Tracking a container: *'where is SEAU9342928?'* or *'when does EMCU1397267 reach PSCH?'*\n"
            "- Plans & reasoning: *'why is OOLU9028993 in bin 1-12-2A?'*\n"
            "- The pipeline: *'how many containers are at sea / arrived?'*\n"
            "- The warehouse: *'what is the bin utilisation?'*, *'cold room occupancy?'*, *'which lanes are in use?'*\n"
            "- Vessels: *'where is MAERSK EGYPT?'*\n"
            "- Flows: *'how many Top Up jobs?'*, *'which MCC containers are bound for Antwerp?'*\n"
            "- Exceptions: *'what needs attention?'*, *'any containers overdue?'*\n"
            "- History: *'what changed in the last hour?'*\n"
            "- Changing plans (with your approval): *'move SEAU9342928 to bin 5-8-1B'*, "
            "*'reschedule OOLU9028993 to RA-4'*, *'release the lane of OOLU9077045'*\n\n"
            "Every answer is computed live from the simulation. Plan changes are "
            "proposed and wait for your Approve / Reject before they touch the plan."
        )

    def _fallback(self, q: str) -> str:
        return (
            f"I don't have a direct answer for *'{q[:120]}'* yet, but I can help with "
            "containers, vessels, warehouse state, the pipeline and the trace. "
            "Type **'help'** for the full list, or ask e.g. *'where is SEAU9342928?'* "
            "or *'how many containers are at sea?'*"
        )


def default_intel_brain() -> AgentBrain:
    """Pick the brain for the PSA Intelligence page.

    Same seam as the rest of the stack: when ``AGENTIC_API_ENDPOINT`` is
    configured the page answers through the agentic AI API; otherwise the
    deterministic rule brain answers (zero cost, reproducible). The page and
    endpoint never change.
    """
    if AgenticAPIBrain().configured:
        return AgenticAPIBrain()
    return IntelRuleBrain()


__all__ = ["IntelRuleBrain", "default_intel_brain"]
