"""Control-tower KPIs for the MCC cargo flow.

Everything is computed over the current simulated state: how much MCC cargo is
in the pipeline (at sea / unloaded / at depot / on the road / arrived at PSCH),
how the receiving plan is responding to the inbound volume rate, and how the
outbound consolidation is tracking against vessel sailings.
"""
from __future__ import annotations

from datetime import datetime

from config import RECEIVING_LANES, RELEASING_LANES, SIM_NOW
from agents.mcc_planner import journey_status, outbound_status
from data.facility import ROOMS, bin_room, room_capacity, total_capacity

JOURNEY_STAGES = ["En Route (Sea)", "Unloaded", "Depot", "En Route (Road)", "Arrived"]
OUTBOUND_STAGES = ["staged", "released", "in_transit", "loaded"]


def compute_kpis(
    containers: list[dict],
    plans: list[dict],
    outbounds: list[dict],
    yard: list[dict],
    drayage: dict,
    sim_now: datetime = SIM_NOW,
) -> dict:
    """Return the KPI dict for the control-tower view."""
    journey_counts = {s: 0 for s in JOURNEY_STAGES}
    for p in plans:
        journey_counts[journey_status(p, sim_now)] = journey_counts.get(
            journey_status(p, sim_now), 0
        ) + 1

    outbound_counts = {s: 0 for s in OUTBOUND_STAGES}
    for o in outbounds:
        outbound_counts[outbound_status(o, sim_now)] = outbound_counts.get(
            outbound_status(o, sim_now), 0
        ) + 1

    # Sea -> PSCH doorstep pipeline duration (what the agent derives from ETA).
    pipeline_h = (
        [
            (p["psch_receipt_eta"] - p["sea_arrival"]).total_seconds() / 3600
            for p in plans
            if p.get("sea_arrival") and p.get("psch_receipt_eta")
        ]
        or [0.0]
    )
    avg_pipeline_h = round(sum(pipeline_h) / len(pipeline_h), 1)

    # Remaining ETA for cargo still in flight (not yet at the PSCH doorstep).
    in_flight = [
        (p["psch_receipt_eta"] - sim_now).total_seconds() / 3600
        for p in plans
        if p.get("psch_receipt_eta") and p["psch_receipt_eta"] > sim_now
    ]
    avg_remaining_h = round(sum(in_flight) / len(in_flight), 1) if in_flight else 0.0

    total_bins = total_capacity()
    bins_used = len({p.get("bin_location") for p in plans if p.get("bin_location")})
    bin_util = round(100 * bins_used / total_bins, 1) if total_bins else 0.0

    # Per-room rack occupancy (ambient vs cold room) for the PSCH space view.
    room_occupancy: dict[str, float] = {}
    for room in ROOMS:
        used = len(
            {
                p["bin_location"]
                for p in plans
                if p.get("bin_location")
                and bin_room(p["bin_location"].removeprefix("Bin ")) == room
            }
        )
        cap = room_capacity(room)
        room_occupancy[room] = round(100 * used / cap, 1) if cap else 0.0

    areas_opened = len({p["receiving_area"].split(" · ")[0] for p in plans})
    lanes_releasing_used = len(
        {p["release_lane"] for p in plans} | {o["loading_lane"] for o in outbounds}
    )
    arrived_now = journey_counts["Arrived"]

    utils = [y["utilization_pct"] for y in yard]
    avg_yard_util = round(sum(utils) / len(utils), 1) if utils else 0.0

    return {
        "mcc_containers": len(plans),
        "journey_counts": journey_counts,
        "en_route_total": journey_counts["En Route (Sea)"] + journey_counts["En Route (Road)"],
        "arrived_at_psch": arrived_now,
        "outbound_counts": outbound_counts,
        "outbound_total": len(outbounds),
        "loaded_outbound": outbound_counts["loaded"],
        "avg_pipeline_h": avg_pipeline_h,
        "avg_remaining_h": avg_remaining_h,
        "arrival_rate": round(len(in_flight) / 6.0, 2) if in_flight else 0.0,
        "receiving_areas_opened": areas_opened,
        "bin_util": bin_util,
        "bins_used": bins_used,
        "bins_total": total_bins,
        "room_occupancy": room_occupancy,
        "lanes_releasing_used": lanes_releasing_used,
        "lanes_total": len(RECEIVING_LANES) + len(RELEASING_LANES),
        "avg_yard_util": avg_yard_util,
        "drayage_util": drayage.get("utilization_pct", 0.0),
    }
