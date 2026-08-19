"""Declarative tool registry — the agentic AI seam.

Every capability an agent can use — reading shared state, computing a plan, or
persisting a *proposal* — is a registered **tool** with a name, a description,
a JSON-schema for its parameters, and a permission level. This registry is the
single source of truth that both brains drive through one interface:

* the **rule-based planner** calls ``call_tool`` for every state read and every
  plan write (each call is logged to the execution trace), and
* a future **agentic AI API** receives ``tool_schemas()`` verbatim (OpenAI-style
  function definitions) and calls back into the same ``call_tool`` — so the two
  brains are interchangeable behind one seam, and the trace stays uniform
  whichever brain ran.

Permission levels gate what an agent may do without a human in the loop:

* ``read``     — safe, always allowed, fully traced
* ``mutate``   — writes a *proposal* (an MCC plan, an outbound container, a
                 work order) into the plan store for human review
* ``approval`` — would execute a real-world action (release a lane, dispatch a
                 truck, book a port slot); outside the agent's authority unless
                 the runtime's autonomy level allows it
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from config import DB_PATH
from data import store
from models.schemas import MccPlan, OutboundContainer

# Permission levels.
READ = "read"
MUTATE = "mutate"
APPROVAL = "approval"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON-schema style property definitions
    permission: str  # READ | MUTATE | APPROVAL
    handler: Callable[[dict[str, Any], Path], Any]
    expose_to_llm: bool = True  # False -> rule-planner-only, hidden from the LLM


TOOLS: dict[str, Tool] = {}


def register_tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    permission: str,
    handler: Callable[[dict[str, Any], Path], Any],
    expose_to_llm: bool = True,
) -> None:
    TOOLS[name] = Tool(name, description, parameters, permission, handler, expose_to_llm)


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-style function schemas, sent verbatim to an agentic AI API.

    Whole-batch plan writers (``save_mcc_plans`` / ``save_outbound_containers``)
    are excluded: the LLM may only propose *granular* adjustments through the
    change tools (reassign_bin, reschedule_receiving_area, release_lane), so it
    can never rewrite the whole plan set behind the user's back.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": t.parameters,
                    "additionalProperties": False,
                },
            },
        }
        for t in TOOLS.values()
        if t.expose_to_llm
    ]


def permission_of(name: str) -> str:
    return TOOLS[name].permission


def call_tool(name: str, args: dict[str, Any], store_path: Path | str = DB_PATH) -> Any:
    """Invoke a registered tool and record the call to the execution trace.

    This is the single funnel for *all* agent tool use (rule-based and LLM), so
    the trace is the auditable record of exactly what the agent read and wrote.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise KeyError(f"Unknown agent tool: {name!r}")
    result = tool.handler(args or {}, Path(store_path))
    store.record_event(
        "agent",
        "tool_call",
        {
            "tool": name,
            "permission": tool.permission,
            "result": json.dumps(result, default=str)[:300],
        },
        store_path,
    )
    return result


# --- Read tools (always allowed) -------------------------------------------------


def _read(fn: Callable[[Path], Any]) -> Callable[[dict[str, Any], Path], Any]:
    return lambda args, path: fn(path)


register_tool(
    "list_vessels",
    "Every vessel on a voyage with its berth assignment and live tracking "
    "(status, berth_id, ETA, ETD, destination, distance_nm, speed_knots).",
    {},
    READ,
    _read(store.get_vessels),
)
register_tool(
    "list_containers",
    "Every port-side container on a voyage, with its status, cargo flag, "
    "customs status, special handling and Bay-Row-Tier stow cell.",
    {},
    READ,
    _read(store.get_containers),
)
register_tool(
    "list_shipments",
    "Every palletised cargo unit at PSCH (shipment), with its destination, "
    "cargo type, volume, source container and outbound container.",
    {},
    READ,
    _read(store.get_shipments),
)
register_tool(
    "list_vessel_stowage",
    "Every occupied cell of every vessel's bay plan (Bay-Row-Tier), with the "
    "container, destination, weight and MCC flag.",
    {},
    READ,
    _read(store.get_vessel_stowage),
)
register_tool(
    "get_mcc_plans",
    "The current MCC plan batch: the end-to-end journey, receiving and "
    "putaway plan the agent derived for each inbound MCC container.",
    {},
    READ,
    _read(store.get_mcc_plans),
)
register_tool(
    "get_outbound_containers",
    "The current consolidated export containers planned at PSCH, with their "
    "bound vessel, stuffing window, loading cell and ETAs.",
    {},
    READ,
    _read(store.get_outbound_containers),
)
register_tool(
    "get_bookings",
    "The PSCH service bookings (LCL deconsolidation / MCC consolidation / "
    "transloading) linked to containers, with required-by and storage zone.",
    {},
    READ,
    _read(store.get_bookings),
)
register_tool(
    "get_yard_status",
    "Yard-level utilisation and gate-lane availability at the port.",
    {},
    READ,
    _read(store.get_yard_status),
)
register_tool(
    "get_drayage",
    "Drayage capacity: total and available trucks at PSCH.",
    {},
    READ,
    _read(store.get_drayage),
)
register_tool(
    "get_slas",
    "Customer service-level profiles (priority tier and SLA hours) used to "
    "prioritise staging and putaway.",
    {},
    READ,
    _read(store.get_slas),
)


def _get_psch_space(args: dict[str, Any], path: Path) -> dict[str, Any]:
    """Compact PSCH facility snapshot: rooms, receiving/releasing lanes and stats."""
    from config import sim_now  # live clock, same as the dashboard
    from data.facility import build_psch_space

    plans = store.get_mcc_plans(path)
    shipments = store.get_shipments(path)
    outbounds = store.get_outbound_containers(path)
    space = build_psch_space(plans, shipments, outbounds, sim_now=sim_now())
    return {
        "rooms": [
            {"id": r["id"], "label": r["label"], "temp": r["temp"],
             "used": r["used"], "cap": r["cap"], "pct": r["pct"]}
            for r in space["rooms"]
        ],
        "lanes": space["lanes"],
        "releasing_groups": space["releasing_groups"],
        "stats": space["stats"],
        "hazmat_aisle": space["hazmat_aisle"],
    }


def _get_terminal_snapshot(args: dict[str, Any], path: Path) -> dict[str, Any]:
    """Authoritative live KPIs — the exact numbers the dashboard shows.

    The LLM brain answers count/status questions best when it can read a
    deterministic snapshot instead of deriving numbers from raw tables (which
    is how a 7B model ends up inventing counts). This tool returns the same
    figures the control tower renders, computed from the live sim clock.
    """
    from datetime import timedelta

    from config import sim_now
    from analysis.kpis import compute_kpis
    from agents.mcc_planner import journey_status, outbound_status  # lazy

    now = sim_now()
    containers = store.get_containers(path)
    plans = store.get_mcc_plans(path)
    outbounds = store.get_outbound_containers(path)
    yard = store.get_yard_status(path)
    drayage = store.get_drayage(path)
    k = compute_kpis(containers, plans, outbounds, yard, drayage, sim_now=now)

    journey_counts = {s: 0 for s in ["En Route (Sea)", "Unloaded", "Depot", "En Route (Road)", "Arrived"]}
    overdue: list[str] = []
    for p in plans:
        st = journey_status(p, now)
        journey_counts[st] = journey_counts.get(st, 0) + 1
        if st != "Arrived" and p.get("psch_receipt_eta") and now > p["psch_receipt_eta"]:
            overdue.append(p["container_id"])

    outbound_counts: dict[str, int] = {}
    for o in outbounds:
        st = outbound_status(o, now)
        outbound_counts[st] = outbound_counts.get(st, 0) + 1

    # Vessels by live tracking status.
    vessels_by_status: dict[str, int] = {}
    for v in store.get_vessels(path):
        st = v.get("status") or "unknown"
        vessels_by_status[st] = vessels_by_status.get(st, 0) + 1

    # Plans by hub flow (MCC / LCL / FCL / Top Up / Transload).
    flows_by_count: dict[str, int] = {}
    for p in plans:
        f = p.get("flow") or "mcc"
        flows_by_count[f] = flows_by_count.get(f, 0) + 1

    # Outbound by destination (top 8, sorted by volume).
    dests: dict[str, int] = {}
    for o in outbounds:
        d = o.get("destination") or "—"
        dests[d] = dests.get(d, 0) + 1
    outbound_by_destination = dict(
        sorted(dests.items(), key=lambda kv: -kv[1])[:8]
    )

    # Outbound near or past its loading window (still not loaded).
    loading_deadline = now + timedelta(hours=2)
    outbound_near_or_past_loading = sum(
        1
        for o in outbounds
        if outbound_status(o, now) != "loaded"
        and o.get("eta_loading_area")
        and o["eta_loading_area"] <= loading_deadline
    )

    return {
        "sim_now": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "total_containers_planned": len(plans),
        "containers_by_journey_stage": journey_counts,
        "containers_at_psch_now": journey_counts.get("Arrived", 0),  # the authoritative answer
        "containers_overdue_receipt": overdue,  # planned receipt ETA passed, not arrived
        "vessels_by_status": vessels_by_status,
        "containers_by_flow": flows_by_count,
        "outbound_consolidation_by_status": outbound_counts,
        "outbound_by_destination": outbound_by_destination,
        "outbound_near_or_past_loading": outbound_near_or_past_loading,
        "arrival_rate_per_hour_next_6h": k.get("arrival_rate"),
        "avg_sea_to_psch_pipeline_h": k.get("avg_pipeline_h"),
        "bin_utilisation_pct": k.get("bin_util"),
        "yard_avg_utilisation_pct": k.get("avg_yard_util"),
        "drayage_trucks_available": k.get("drayage_util"),
    }


register_tool(
    "get_terminal_snapshot",
    "Authoritative live terminal KPIs, exactly as the control tower renders "
    "them: total containers planned, containers by journey stage (En Route "
    "(Sea) / Unloaded / Depot / En Route (Road) / Arrived), containers at "
    "PSCH now, containers overdue for receipt, vessels by status (docked / "
    "inbound / available), containers by flow (MCC / LCL / FCL / Top Up / "
    "Transload), outbound consolidation status AND by destination, outbound "
    "near or past loading, arrival rate, bin utilisation and yard "
    "utilisation. PREFER THIS TOOL for any count/status/vessel/flow/"
    "destination question instead of deriving numbers from raw tables.",
    {},
    READ,
    _get_terminal_snapshot,
)

register_tool(
    "get_psch_space",
    "PSCH facility snapshot: room occupancy (AMBIENT / COLD ROOM), receiving "
    "lanes in use, releasing lanes and consolidation groups, and warehouse "
    "stats (bin utilisation, pallets planned / in storage).",
    {},
    READ,
    _get_psch_space,
)


def _get_trace_events(args: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    limit = max(1, min(int(args.get("limit", 50)), 200))
    events = store.get_trace(limit=limit, path=path)
    out = []
    for e in events:
        ts = e.get("ts")
        out.append(
            {
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "actor": e.get("actor"),
                "event": e.get("event"),
                "detail": e.get("detail"),
            }
        )
    return out


register_tool(
    "get_trace_events",
    "Recent execution-trace events (agent runs, tool calls, approvals, "
    "inspections) newest first. Use to explain what happened recently.",
    {"limit": {"type": "integer", "description": "Max events to return (default 50)."}},
    READ,
    _get_trace_events,
)


# --- Mutate tools (write proposals for human review) ------------------------------


def _save_mcc_plans(args: dict[str, Any], path: Path) -> dict[str, int]:
    plans = [MccPlan(**p) for p in args["plans"]]
    store.save_mcc_plans(plans, path)
    return {"saved": len(plans)}


def _save_outbound_containers(args: dict[str, Any], path: Path) -> dict[str, int]:
    outbound = [OutboundContainer(**o) for o in args["outbound"]]
    store.save_outbound_containers(outbound, path)
    return {"saved": len(outbound)}


register_tool(
    "save_mcc_plans",
    "Replace the current MCC plan batch with a freshly computed one. Each plan "
    "is a proposal the dashboard shows for human review.",
    {"plans": {"type": "array", "description": "List of MccPlan records."}},
    MUTATE,
    _save_mcc_plans,
    expose_to_llm=False,  # rule-planner-only; the LLM may only adjust granularly
)
register_tool(
    "save_outbound_containers",
    "Replace the current outbound consolidation container plan batch. Each "
    "record is a proposal for human review.",
    {"outbound": {"type": "array", "description": "List of OutboundContainer records."}},
    MUTATE,
    _save_outbound_containers,
    expose_to_llm=False,  # rule-planner-only; the LLM may only adjust granularly
)


# --- Granular change tools (the agent "takes action" with human approval) --------
# These are the ONLY plan writers the LLM can see. Each changes one container's
# plan in place (never a whole-batch rewrite), and each is gated: in advisory
# mode the runtime returns pending_approval and a human approves in the UI.


def _normalise_bin(bin_location: str) -> str:
    """'1-12-2A' -> 'Bin 1-12-2A' (the display form plans carry)."""
    from data.facility import bin_id_of, is_bin  # lazy: avoid import cycle

    bid = bin_id_of(bin_location)
    if not is_bin(bid):
        raise ValueError(
            f"{bin_location!r} is not a rack bin; use AISLE-LEVEL-BAY like '5-8-1B'"
        )
    return f"Bin {bid}"


def _append_reasoning(existing: str, note: str) -> str:
    return (existing or "").strip() + f"\n[agent] {note}"


def _reassign_bin(args: dict[str, Any], path: Path) -> dict[str, Any]:
    """Move one container's putaway bin in its plan (granular, approved)."""
    cid = str(args.get("container_id", "")).strip()
    if not cid:
        raise ValueError("container_id is required")
    bin_location = _normalise_bin(str(args.get("bin_location", "")).strip())
    reason = str(args.get("reason") or "").strip() or "bin reassigned by operator preference"
    plans = store.get_mcc_plans(path)
    p = next((x for x in plans if x["container_id"] == cid), None)
    if p is None:
        raise KeyError(f"No MCC plan for container {cid!r}")
    updated = store.update_mcc_plan(
        path,
        cid,
        bin_location=bin_location,
        reasoning=_append_reasoning(p.get("reasoning"), f"reassigned to {bin_location}: {reason}"),
    )
    return {"container_id": cid, "bin_location": updated["bin_location"], "reasoning": updated["reasoning"]}


register_tool(
    "reassign_bin",
    "Move one container's putaway bin in its MCC plan to a different rack bin "
    "(AISLE-LEVEL-BAY, e.g. '5-8-1B'). Granular: only that container's plan "
    "changes. Always propose this rather than rewriting the plan batch.",
    {
        "container_id": {"type": "string", "description": "The container to move."},
        "bin_location": {"type": "string", "description": "Target rack bin, e.g. '5-8-1B'."},
        "reason": {"type": "string", "description": "Why the move is proposed."},
    },
    MUTATE,
    _reassign_bin,
)


def _reschedule_receiving_area(args: dict[str, Any], path: Path) -> dict[str, Any]:
    """Move one container to another receiving area/door in its plan."""
    cid = str(args.get("container_id", "")).strip()
    if not cid:
        raise ValueError("container_id is required")
    area = str(args.get("receiving_area", "")).strip()
    if not area:
        raise ValueError("receiving_area is required")
    from config import RECEIVING_AREAS  # lazy: avoid import cycle

    # Accept 'RA-4' or a full 'RA-4 · Door 4' label; normalise to the label form.
    if area in RECEIVING_AREAS:
        area = f"{area} · Door {area.removeprefix('RA-')}"
    reason = str(args.get("reason") or "").strip() or "receiving area changed by operator preference"
    plans = store.get_mcc_plans(path)
    p = next((x for x in plans if x["container_id"] == cid), None)
    if p is None:
        raise KeyError(f"No MCC plan for container {cid!r}")
    updated = store.update_mcc_plan(
        path,
        cid,
        receiving_area=area,
        reasoning=_append_reasoning(p.get("reasoning"), f"receiving rescheduled to {area}: {reason}"),
    )
    return {"container_id": cid, "receiving_area": updated["receiving_area"], "reasoning": updated["reasoning"]}


register_tool(
    "reschedule_receiving_area",
    "Move one container to a different receiving area / door in its MCC plan. "
    "Granular: only that container's plan changes.",
    {
        "container_id": {"type": "string", "description": "The container to reschedule."},
        "receiving_area": {"type": "string", "description": "Target area, e.g. 'RA-4'."},
        "reason": {"type": "string", "description": "Why the change is proposed."},
    },
    MUTATE,
    _reschedule_receiving_area,
)


def _release_lane(args: dict[str, Any], path: Path) -> dict[str, Any]:
    """Release an outbound container's staging lane now (real-world action)."""
    from config import sim_now  # lazy

    cid = str(args.get("container_id", "")).strip()
    if not cid:
        raise ValueError("container_id is required")
    outbounds = store.get_outbound_containers(path)
    o = next((x for x in outbounds if x["container_id"] == cid), None)
    if o is None:
        raise KeyError(f"No outbound plan for container {cid!r}")
    reason = str(args.get("reason") or "").strip() or "lane released by operator"
    now = sim_now()
    updated = store.update_outbound_container(
        path,
        cid,
        lane_release_time=now,
        reasoning=_append_reasoning(o.get("reasoning"), f"lane released now ({now:%d %b %H:%M}Z): {reason}"),
    )
    return {"container_id": cid, "lane_release_time": updated["lane_release_time"].isoformat() if hasattr(updated["lane_release_time"], "isoformat") else str(updated["lane_release_time"])}


register_tool(
    "release_lane",
    "Release an outbound container's staging lane immediately (a real-world "
    "action): its lane_release_time is set to now, so its status advances from "
    "staged to released on the next poll. Requires human approval in advisory "
    "mode.",
    {
        "container_id": {"type": "string", "description": "The outbound container to release."},
        "reason": {"type": "string", "description": "Why the lane is released now."},
    },
    APPROVAL,
    _release_lane,
)
