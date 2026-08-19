"""Exception agent — the second visible agent of the system.

The **Q&A agent** (PSA Intelligence page) answers questions; the **exception
agent** watches the live world and surfaces what needs attention: receipt ETAs
missed, customs holds, outbound loading windows missed or approaching, and
vessels still at berth past their ETD. Like every agent it reads through the
tool registry (so each read lands in the execution trace), returns a ranked
\"what is wrong, why, what I recommend\" answer, and is deterministic — the
LLM brain can take over the same role later without changing the page.

Purpose in the demo: three agents on one runtime — the planner agent (rules,
writes proposals), the exception agent (this file), and the Q&A agent.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from agents.brain import AgentBrain, BrainResult
from agents.tools import call_tool

# Thresholds (hours) that define "needs attention".
RECEIPT_LATE_WARNING_H = 2.0    # receipt ETA passed by more than this -> warning
RECEIPT_LATE_CRITICAL_H = 6.0  # passed by more than this -> critical
CUTOFF_WINDOW_H = 2.0          # outbound within this of its loading window -> watch
LOADING_LATE_CRITICAL_H = 1.0  # loading window passed by more than this -> critical
VESSEL_ETD_SLIP_H = 2.0        # docked past ETD by more than this -> warning


def _least_loaded_area(plans: list[dict], current: str) -> str:
    """Least-busy receiving area among the current plan batch (ties -> lowest)."""
    counts: Counter[str] = Counter()
    for p in plans:
        a = (p.get("receiving_area") or "").split(" · ")[0]
        if a:
            counts[a] += 1
    if not counts:
        return "RA-1"
    least = min(counts, key=lambda a: (counts[a], a))
    if least == current and len(counts) > 1:
        least = min((a for a in counts if a != current), key=lambda a: (counts[a], a))
    return least


def _vessel_release_candidate(
    v: dict, outbounds: list[dict], now
) -> dict | None:
    """Earliest not-yet-loaded outbound staged for this vessel (release target)."""
    from agents.mcc_planner import outbound_status  # lazy

    best = None
    for o in outbounds:
        if not (
            o.get("bound_vessel_name") == v.get("vessel_name")
            or o.get("bound_vessel_id") == v.get("voyage_id")
        ):
            continue
        if outbound_status(o, now) == "loaded":
            continue
        eta = o.get("eta_loading_area")
        if eta is None:
            continue
        if best is None or eta < best[1]:
            best = (o, eta)
    return best[0] if best else None


def _vessel_release_action(v: dict, outbounds: list[dict], now) -> dict | None:
    """Suggested fix for an ETD slip: release the earliest staged lane on the vessel."""
    c = _vessel_release_candidate(v, outbounds, now)
    if c is None:
        return None
    return {
        "tool": "release_lane",
        "args": {
            "container_id": c["container_id"],
            "reason": f"vessel {v.get('vessel_name') or v.get('voyage_id')} ETD slipped — release staging lane",
        },
        "label": "Release earliest staged lane on this vessel",
    }


def scan_exceptions(
    plans: list[dict],
    outbounds: list[dict],
    containers: list[dict],
    vessels: list[dict],
    now=None,
) -> list[dict[str, Any]]:
    """Pure scan over already-loaded state — no tool calls, no tracing.

    Used by the Control Tower's 8s poll (which already holds this data in
    ``build_state``) so the trace is not flooded with reads. Returns a list of
    ``{severity, kind, container_id, issue, detail, recommendation}`` dicts,
    critical first.
    """
    from agents.mcc_planner import journey_status, outbound_status  # lazy

    now = now if now is not None else _sim_now()

    found: list[dict[str, Any]] = []

    # 1. Receipt ETA missed — the promised PSCH receipt time passed while the
    #    container is still en route (a deterministic road-delay subset of the
    #    wave slips past its planned ETA; see mcc_planner._delay_hours).
    for p in plans:
        st = journey_status(p, now)
        eta = p.get("psch_receipt_eta")
        delay_h = float(p.get("delay_hours") or 0.0)
        if st != "Arrived" and eta is not None and now > eta:
            late_h = (now - eta).total_seconds() / 3600
            actual = eta + timedelta(hours=delay_h)
            detail = (
                f"Planned PSCH receipt {eta:%d %b %H:%M}Z"
                + (f" (delay +{delay_h:g}h → {actual:%d %b %H:%M}Z)" if delay_h else "")
                + f", journey '{st}'; on {p.get('carrying_vessel_name') or '—'}."
            )
            found.append(
                {
                    "severity": "critical" if late_h > RECEIPT_LATE_CRITICAL_H else "warning",
                    "kind": "receipt_eta_missed",
                    "container_id": p["container_id"],
                    "issue": f"Receipt ETA missed by {late_h:.1f}h",
                    "detail": detail,
                    "recommendation": (
                        "Re-sequence the receiving plan (reschedule_receiving_area) "
                        "or expedite the road leg for this container."
                    ),
                    "suggested_action": {
                        "tool": "reschedule_receiving_area",
                        "args": {
                            "container_id": p["container_id"],
                            "receiving_area": _least_loaded_area(
                                plans, (p.get("receiving_area") or "RA-1").split(" · ")[0]
                            ),
                            "reason": "receipt ETA missed — re-sequence receiving to the least-loaded door",
                        },
                        "label": "Reschedule to the least-loaded receiving door",
                    },
                }
            )

    # 2. Customs holds — cargo flagged 'held' by customs cannot be staged or
    #    consolidated; the plan must not advance it.
    for c in containers:
        if c.get("customs_status") == "held":
            found.append(
                {
                    "severity": "warning",
                    "kind": "customs_hold",
                    "container_id": c["container_id"],
                    "issue": "Customs hold in place",
                    "detail": (
                        f"Container {c['container_id']} is flagged held by customs; "
                        "it must not be staged or consolidated until cleared."
                    ),
                    "recommendation": "Keep it out of the receiving/staging plan until the hold is lifted.",
                    "suggested_action": None,  # the correct action is inaction — no plan change until cleared
                }
            )

    # 3. Outbound loading windows — missed (critical) or approaching (watch).
    for o in outbounds:
        st = outbound_status(o, now)
        eta_loading = o.get("eta_loading_area")
        if st == "loaded" or eta_loading is None:
            continue
        h = (eta_loading - now).total_seconds() / 3600
        if h < 0:
            found.append(
                {
                    "severity": "critical" if -h > LOADING_LATE_CRITICAL_H else "warning",
                    "kind": "loading_window_missed",
                    "container_id": o["container_id"],
                    "issue": f"Loading window missed by {-h:.1f}h",
                    "detail": (
                        f"Outbound {o['container_id']} -> {o.get('destination')} on "
                        f"{o.get('bound_vessel_name') or '—'} still {st}, past its "
                        f"planned {eta_loading:%d %b %H:%M}Z loading time."
                    ),
                    "recommendation": "Expedite stuffing / release the staging lane immediately.",
                    "suggested_action": {
                        "tool": "release_lane",
                        "args": {
                            "container_id": o["container_id"],
                            "reason": "loading window missed — release the staging lane immediately",
                        },
                        "label": "Release staging lane now",
                    },
                }
            )
        elif h <= CUTOFF_WINDOW_H:
            found.append(
                {
                    "severity": "warning",
                    "kind": "loading_cutoff_approaching",
                    "container_id": o["container_id"],
                    "issue": f"Loading cutoff in {h:.1f}h",
                    "detail": (
                        f"Outbound {o['container_id']} -> {o.get('destination')} on "
                        f"{o.get('bound_vessel_name') or '—'} is {st}; must reach the "
                        f"loading area by {eta_loading:%d %b %H:%M}Z."
                    ),
                    "recommendation": "Prioritise pallet picking and lane release for this container.",
                    "suggested_action": {
                        "tool": "release_lane",
                        "args": {
                            "container_id": o["container_id"],
                            "reason": "loading cutoff approaching — release the staging lane now",
                        },
                        "label": "Release staging lane now",
                    },
                }
            )

    # 4. Vessel ETD slip — a docked vessel still at berth past its ETD.
    for v in vessels:
        etd = v.get("etd")
        if v.get("status") == "docked" and etd is not None and now > etd:
            slip_h = (now - etd).total_seconds() / 3600
            if slip_h > VESSEL_ETD_SLIP_H:
                found.append(
                    {
                        "severity": "warning",
                        "kind": "vessel_etd_slip",
                        "container_id": v.get("voyage_id", ""),
                        "issue": f"ETD slipped by {slip_h:.1f}h",
                        "detail": (
                            f"{v.get('vessel_name') or v.get('voyage_id')} is still at "
                            f"berth {v.get('berth_id') or '—'} past its "
                            f"{etd:%d %b %H:%M}Z ETD."
                        ),
                        "recommendation": "Expedite remaining loading; flag for berth/yard re-planning.",
                        "suggested_action": _vessel_release_action(v, outbounds, now),
                    }
                )

    sev_order = {"critical": 0, "warning": 1, "info": 2}
    found.sort(key=lambda e: (sev_order.get(e["severity"], 9), e["issue"]))
    return found


def find_exceptions(
    path: Path | str,
    now=None,
) -> list[dict[str, Any]]:
    """Scan live state for operational exceptions, ranked by severity.

    Every read goes through ``call_tool`` so the scan is fully traced (the same
    funnel the LLM brain will use). For the 8s dashboard poll use
    ``scan_exceptions`` directly instead (no tracing).
    """
    now = now if now is not None else _sim_now()
    plans = call_tool("get_mcc_plans", {}, path)
    outbounds = call_tool("get_outbound_containers", {}, path)
    containers = call_tool("list_containers", {}, path)
    vessels = call_tool("list_vessels", {}, path)
    return scan_exceptions(plans, outbounds, containers, vessels, now)


class ExceptionBrain:
    """Deterministic exception agent: ranks what is wrong and recommends fixes.

    Same seam as the other brains (``AgentBrain`` protocol) — the runtime wraps
    it, reads are traced, and the Control Tower renders its output live.
    """

    name = "exception-agent-v1"

    def run(
        self,
        goal: str,
        context: dict[str, Any],
        store_path: Path,
        history: list[dict] | None = None,
    ) -> BrainResult:
        exceptions = find_exceptions(store_path)
        summary = format_exceptions(exceptions)
        return BrainResult(ok=True, summary=summary, events=exceptions)


def format_exceptions(exceptions: list[dict[str, Any]]) -> str:
    """Render the ranked exception list as the agent's written answer."""
    if not exceptions:
        return "No exceptions right now — the terminal is running clean."
    lines = [f"**Attention needed — {len(exceptions)} item(s) need attention**"]
    for e in exceptions:
        lines.append(
            f"- **[{e['severity'].upper()}] {e['issue']}** — {e['container_id']}\n"
            f"    {e['detail']}\n"
            f"    **Recommend:** {e['recommendation']}"
        )
    return "\n".join(lines)


def _sim_now():
    from config import sim_now  # lazy

    return sim_now()


def _road_hours(p: dict, now) -> float:
    eta = p.get("psch_receipt_eta")
    if eta is None:
        return 0.0
    return max(0.0, (eta - now).total_seconds() / 3600)


__all__ = ["ExceptionBrain", "find_exceptions", "format_exceptions", "scan_exceptions"]
