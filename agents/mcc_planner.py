"""MCC (multi-country consolidation) planning agent.

The agent owns the coordination decision of the whole cargo story:

1. For every inbound MCC container it derives the full port->PSCH timeline
   from the carrying vessel's ETA: sea arrival -> quay unload -> port depot ->
   road dispatch -> PSCH doorstep receipt ETA.
2. Before the cargo even arrives, it plans the PSCH receiving area (opened
   according to the inbound arrival rate) and the robot putaway bin for the
   palletised cargo inside.
3. It groups the inbound containers by final destination into outbound
   consolidation containers, times the pallet pick against the stuffing window
   (itself driven by when the bound vessel will arrive to load at the port),
   releases loading lanes per outbound container number, and allocates the
   physical releasing lanes (numbered 1..26) where each group's pallets are
   staged at the PSCH dispatch area for collection by the container.
4. It derives each outbound container's status (staged -> released ->
   in_transit -> loaded) plus the ETA to arrive at the quay loading area.

The agent is deterministic (rule-based) so the demo is fully reproducible with
zero API cost; every state read is recorded to the execution trace so the
reasoning is auditable.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from typing import Callable

from config import (
    DB_PATH,
    MOVE_TO_BIN_MIN,
    PALLETS_PER_LANE,
    PSCH_HAZMAT_AISLE,
    PUTAWAY_ROBOTS,
    RECEIVING_AREAS,
    RELEASING_LANES_TOTAL,
    ROAD_TRANSIT_MIN,
    SIM_NOW,
    STAGING_MIN,
    UNLOAD_MIN,
    YARD_TRANSFER_MIN,
)
from data import store
from data.facility import aisle_of_bin, bin_id_of, iter_bins, level_of_bin
from models.schemas import (
    JourneyStatus,
    MccPlan,
    OutboundContainer,
    OutboundStatus,
)

# Consolidation constants (hours / minutes).
LOADING_CUTOFF_BEFORE_ETD = timedelta(hours=4)   # container at quay 4h before sailing
STUFFING_END_LEAD = timedelta(hours=3)           # stuffing done 3h before cutoff
STUFFING_HOURS = timedelta(hours=6)              # pallet pick / stuffing window length
PALLET_PICK_BUFFER = timedelta(minutes=45)       # pallets picked before stuffing starts
ROAD_BACK_MIN = 45                               # PSCH -> quay loading area transit

# Per-container variance around the nominal depot dwell (minutes), so different
# containers on the same vessel sit at slightly different journey stages.
DEPOT_DWELL_CHOICES = [60, 90, 120]

# Scenario horizon used for the inbound arrival-rate calculation.
RATE_WINDOW_HOURS = 6.0


def journey_status(plan: dict | None, sim_now: datetime = SIM_NOW) -> str:
    """Derive the container's journey stage from the agent's plan times.

    En Route (Sea) -> Unloaded -> Depot -> En Route (Road) -> Arrived.
    """
    if plan is None:
        return JourneyStatus.EN_ROUTE_SEA.value
    if sim_now < plan["sea_arrival"]:
        return JourneyStatus.EN_ROUTE_SEA.value
    if sim_now < plan["depot_arrive"]:
        return JourneyStatus.UNLOADED.value
    if sim_now < plan["road_depart"]:
        return JourneyStatus.AT_DEPOT.value
    if sim_now < plan["psch_receipt_eta"]:
        return JourneyStatus.EN_ROUTE_ROAD.value
    return JourneyStatus.ARRIVED.value


def outbound_status(o: dict, sim_now: datetime = SIM_NOW) -> str:
    """Derive the outbound container's lifecycle from its plan times."""
    if sim_now >= o["eta_loading_area"]:
        return OutboundStatus.LOADED.value
    if sim_now >= o["road_depart"]:
        return OutboundStatus.IN_TRANSIT.value
    if sim_now >= o["lane_release_time"]:
        return OutboundStatus.RELEASED.value
    return OutboundStatus.STAGED.value


def _tool(store_path, name: str, fn: Callable):
    """Read state through the tool layer and record the call to the trace."""
    result = fn(store_path)
    store.record_event(
        "agent",
        "tool_call",
        {"tool": name, "result": json.dumps(result, default=str)[:300]},
        store_path,
    )
    return result


def _receiving_areas_for_rate(rate: float) -> int:
    """Open receiving areas (lanes) based on inbound volume rate (containers/hour)."""
    if rate < 1.0:
        return 2
    if rate < 2.0:
        return 4
    if rate < 3.5:
        return 6
    return min(10, len(RECEIVING_AREAS))


def _bin_pool() -> dict[tuple[str, int], list[str]]:
    """Bins grouped by storage zone and rack level (AISLE-LEVEL-BAY naming).

    Bins are named the conventional DC way -- AISLE-LEVEL-BAY ("1-12-2A" =
    Aisle 1, Level 12, Bay 2A). The slotting rule chooses the level (height)
    from the predicted dwell; these pools hand out the next free bin at that
    level within the right zone: cold room for reefer cargo, the segregated
    hazmat aisle for dangerous goods, ambient aisles for the rest.
    """
    from data.facility import ROOMS

    ambient = iter_bins("ambient")
    zone_bins = {
        "ambient": [b for b in ambient if aisle_of_bin(b) != PSCH_HAZMAT_AISLE],
        "hazmat": [b for b in ambient if aisle_of_bin(b) == PSCH_HAZMAT_AISLE],
        "cold_room": iter_bins("cold_room"),
    }
    levels = ROOMS["ambient"]["levels"]
    return {
        (zone, level): [b for b in bins if level_of_bin(b) == level]
        for zone, bins in zone_bins.items()
        for level in range(1, levels + 1)
    }


def _dwell_level(dwell_hours: float, levels: int = 12) -> int:
    """Rack level (height) for a predicted dwell -- fast movers at the floor.

    Slotting rule: the longer cargo is predicted to sit before its pallets
    are picked for outbound release, the higher up the rack it is stored, so
    the robots reach the soon-to-release cargo without climbing. ~3h dwell ->
    level 1 (floor), ~24h+ -> the top level.
    """
    return max(1, min(levels, math.ceil(dwell_hours / 3.0)))


def _outbound_id(rng: random.Random, seen: set[str]) -> str:
    while True:
        cid = f"OOLU90{rng.randint(10000, 99999)}"
        if cid not in seen:
            seen.add(cid)
            return cid


def _bay_label(bay: int) -> str:
    """40ft bays span the preceding odd bay: 33(34); 20ft bays are single odds."""
    return f"{bay - 1}({bay})" if bay % 2 == 0 else f"{bay}"


def _rel_span_label(start: int, end: int) -> str:
    """Human label for a releasing-lane span, e.g. 5..5 -> '5', 5..7 -> '5–7'."""
    if start == end:
        return str(start)
    return f"{start}–{end}"


def plan(store_path=DB_PATH, sim_now: datetime = SIM_NOW) -> None:
    """Read the world, derive the full MCC plan, and persist it."""
    vessels = _tool(store_path, "list_vessels", store.get_vessels)
    containers = _tool(store_path, "list_containers", store.get_containers)
    shipments = _tool(store_path, "list_shipments", store.get_shipments)

    vessel_by_id = {v["voyage_id"]: v for v in vessels}
    shipments_by_container: dict[str, list[dict]] = {}
    for s in shipments:
        if s.get("source_container_id"):
            shipments_by_container.setdefault(s["source_container_id"], []).append(s)

    mcc = [c for c in containers if c["cargo_flag"] == "deconsolidation_required"]

    # ---- 1. Journey timeline per container ------------------------------------
    timelines: list[dict] = []
    for idx, c in enumerate(mcc):
        vessel = vessel_by_id.get(c["voyage_id"])
        if vessel is None or vessel.get("eta") is None:
            continue
        sea_arrival: datetime = vessel["eta"]
        dwell = DEPOT_DWELL_CHOICES[idx % len(DEPOT_DWELL_CHOICES)]
        unload_end = sea_arrival + timedelta(minutes=UNLOAD_MIN)
        depot_arrive = unload_end + timedelta(minutes=YARD_TRANSFER_MIN)
        road_depart = depot_arrive + timedelta(minutes=dwell)
        receipt = road_depart + timedelta(minutes=ROAD_TRANSIT_MIN)
        timelines.append(
            {
                "container": c,
                "vessel": vessel,
                "sea_arrival": sea_arrival,
                "unload_end": unload_end,
                "depot_arrive": depot_arrive,
                "road_depart": road_depart,
                "receipt": receipt,
            }
        )

    # ---- 2. Consolidation: group inbound containers by final destination ------
    # A consolidation container can sail on a vessel already alongside at the
    # berth (waiting for loading) or on one still arriving en route; either way
    # the loading window is derived from when the vessel departs (its ETD). The
    # pallet pick times decided here drive the slotting step below.
    stowage = _tool(store_path, "list_vessel_stowage", store.get_vessel_stowage)
    stow_by_vessel: dict[str, list[dict]] = {}
    for s in stowage:
        stow_by_vessel.setdefault(s["vessel_id"], []).append(s)

    rng = random.Random(
        hash(tuple(sorted(t["container"]["container_id"] for t in timelines))) & 0xFFFF
    )
    seen_outbound: set[str] = set()

    by_dest: dict[str, list[dict]] = {}
    for t in timelines:
        dest = t["vessel"].get("destination") or "—"
        by_dest.setdefault(dest, []).append(t)

    pick_by_container: dict[str, datetime] = {
        t["container"]["container_id"]: t["receipt"] for t in timelines
    }
    group_by_container: dict[str, str] = {}
    outbounds: list[OutboundContainer] = []
    # Physical releasing-lane allocation at the PSCH dispatch area: each
    # consolidation container is staged on one lane, or a contiguous group of
    # adjacent lanes, sized by how many pallets wait to be loaded into it.
    rel_lanes_by_group: dict[str, str] = {}
    rel_lane_cursor = 1

    for dest in sorted(by_dest):
        members = sorted(by_dest[dest], key=lambda m: m["receipt"])
        bound = min(
            (v for v in vessels if v.get("destination") == dest and v.get("etd")),
            key=lambda v: v["etd"],
            default=None,
        )
        if bound is None or len(members) < 2:
            continue
        vessel_etd = bound["etd"]
        cutoff = vessel_etd - LOADING_CUTOFF_BEFORE_ETD
        stuffing_end = cutoff - STUFFING_END_LEAD
        stuffing_start = stuffing_end - STUFFING_HOURS
        eligible = [m for m in members if m["receipt"] <= stuffing_end]
        if len(eligible) < 2:
            eligible = members  # fall back: consolidate whatever is in flight

        ob_id = _outbound_id(rng, seen_outbound)
        source_container_ids = [m["container"]["container_id"] for m in eligible]
        source_shipment_ids = [
            s["shipment_id"]
            for cid in source_container_ids
            for s in shipments_by_container.get(cid, [])
        ]
        # Releasing lanes: allocate a contiguous span of the 26 physical lanes,
        # sized by the pallets staged for this one container (one lane holds up
        # to PALLETS_PER_LANE pallets; groups may share nothing -- every lane
        # belongs to exactly one container).
        pallets_staged = len(source_shipment_ids)
        lane_span = max(1, math.ceil(pallets_staged / PALLETS_PER_LANE))
        staging_start = rel_lane_cursor
        staging_end = min(RELEASING_LANES_TOTAL, staging_start + lane_span - 1)
        rel_lane_cursor = staging_end + 1
        if rel_lane_cursor > RELEASING_LANES_TOTAL:
            rel_lane_cursor = 1  # next group wraps to the first free lanes
        rel_lanes_by_group[ob_id] = _rel_span_label(staging_start, staging_end)
        lane = f"Lane {1 + (len(outbounds) % 4)}"
        lane_release = stuffing_end
        road_depart = lane_release + timedelta(minutes=ROAD_BACK_MIN)
        eta_loading = road_depart + timedelta(minutes=ROAD_BACK_MIN)
        # Loading cell on the bound vessel: re-use a cell freed by discharge
        # (an MCC cell of that vessel), so the outbound box takes a real,
        # bay-parity-correct slot in the vessel's bay plan.
        vessel_cells = stow_by_vessel.get(bound["voyage_id"], [])
        target_pool = [s for s in vessel_cells if s["is_mcc"]] or [
            s for s in vessel_cells if s["bay"] % 2 == 0
        ]
        target = rng.choice(target_pool) if target_pool else None
        if target is not None:
            stow_bay, stow_row, stow_tier = target["bay"], target["stack"], target["tier"]
            stow = f"Bay {_bay_label(stow_bay)} · Row {stow_row:02d} · Tier {stow_tier:02d}"
        else:
            stow_bay = stow_row = stow_tier = None
            stow = "—"

        if bound["status"] == "docked":
            vessel_pic = "already alongside at the berth, waiting for loading"
        else:
            vessel_pic = (
                f"still en route, {bound.get('distance_nm')} nm out at "
                f"{bound.get('speed_knots')} kn"
            )
        reasoning = (
            f"MCC consolidation: {len(eligible)} inbound containers ({dest} cargo) "
            f"pallet-picked and stuffed into {ob_id}; bound for {bound['vessel_name']} "
            f"({bound['voyage_id']}) {vessel_pic}, departing {vessel_etd:%d %b %H:%M}Z "
            f"from berth {bound['berth_id']}. Loading window runs to "
            f"{cutoff:%H:%M}Z (container at quay 4h before sailing) with stuffing "
            f"{stuffing_start:%H:%M}-{stuffing_end:%H:%M}Z; loading cell {stow}; "
            f"lane {lane} released {lane_release:%H:%M}Z; ETA quay loading area "
            f"{eta_loading:%H:%M}Z. Staging at PSCH: {pallets_staged} pallets "
            f"staged on releasing lane(s) {_rel_span_label(staging_start, staging_end)} "
            f"(one lane per {PALLETS_PER_LANE} pallets, contiguous lanes so the "
            f"container collects the whole group at one spot)."
        )
        outbounds.append(
            OutboundContainer(
                container_id=ob_id,
                destination=dest,
                size_type=rng.choice(["40FT", "40HC"]),  # consolidation boxes are 40ft
                source_container_ids=source_container_ids,
                source_shipment_ids=source_shipment_ids,
                bound_vessel_id=bound["voyage_id"],
                bound_vessel_name=bound["vessel_name"],
                vessel_etd=vessel_etd,
                stow_position=stow,
                stow_bay=stow_bay,
                stow_row=stow_row,
                stow_tier=stow_tier,
                stuffing_start=stuffing_start,
                stuffing_end=stuffing_end,
                lane_release_time=lane_release,
                loading_lane=lane,
                road_depart=road_depart,
                eta_loading_area=eta_loading,
                status=OutboundStatus.STAGED,
                staging_lane_start=staging_start,
                staging_lane_end=staging_end,
                reasoning=reasoning,
            )
        )

        # Each member container's pallet pick is timed against the stuffing
        # window: pallets are picked when the consolidation container arrives
        # to be stuffed (or as soon as the cargo itself has arrived).
        for m in eligible:
            pick = max(m["receipt"], stuffing_start + PALLET_PICK_BUFFER)
            cid = m["container"]["container_id"]
            pick_by_container[cid] = pick
            group_by_container[cid] = ob_id

    # ---- 3. Receiving plan: areas opened by the inbound arrival rate ----------
    window_start, window_end = sim_now, sim_now + timedelta(hours=RATE_WINDOW_HOURS)
    arriving = [t["receipt"] for t in timelines if window_start <= t["receipt"] < window_end]
    rate = len(arriving) / RATE_WINDOW_HOURS
    n_areas = _receiving_areas_for_rate(rate)
    open_areas = RECEIVING_AREAS[:n_areas]

    # ---- 4. Robot putaway: slot each container into a rack level by dwell -----
    # The slotting agent optimises the storage HEIGHT of the container placed:
    # cargo released soon (short predicted dwell before its pallets are picked
    # for the outbound consolidation container) is put at the floor levels for
    # fast robot retrieval; slower movers go higher up the rack.
    bin_pool = _bin_pool()
    bin_cursor: dict[tuple[str, int], int] = {(z, l): 0 for (z, l) in bin_pool}
    bin_used = 0
    plans: list[MccPlan] = []

    for idx, t in enumerate(sorted(timelines, key=lambda x: x["receipt"])):
        c, vessel = t["container"], t["vessel"]
        receipt = t["receipt"]
        area = open_areas[idx % len(open_areas)]
        door = 1 + (idx % 4)
        staging_start = receipt
        staging_end = staging_start + timedelta(minutes=STAGING_MIN)
        move_start = staging_end
        move_end = move_start + timedelta(minutes=MOVE_TO_BIN_MIN)

        special = c.get("special_handling") or []
        if "reefer" in special:
            zone = "cold_room"
        elif "hazmat" in special:
            zone = "hazmat"
        else:
            zone = "ambient"

        pick = pick_by_container[c["container_id"]]
        dwell_h = max(0.0, (pick - receipt).total_seconds() / 3600)
        level = _dwell_level(dwell_h)
        pool = bin_pool[(zone, level)]
        bid = pool[bin_cursor[(zone, level)] % len(pool)]
        bin_cursor[(zone, level)] += 1
        bin_location = f"Bin {bid}"
        bin_used += 1
        robot = f"Robot {1 + (bin_used % PUTAWAY_ROBOTS):02d}"
        # The cargo is staged at its consolidation group's releasing lanes
        # (or, for cargo not yet grouped, at a default lane by rotation).
        release_lane = rel_lanes_by_group.get(
            c["container_id"], str(1 + (idx % RELEASING_LANES_TOTAL))
        )

        zone_txt = {
            "cold_room": "cold room (reefer)",
            "hazmat": f"segregated hazmat aisle {PSCH_HAZMAT_AISLE}",
            "ambient": "ambient",
        }[zone]
        reasoning = (
            f"Carried by {vessel['vessel_name']} ({vessel['voyage_id']}), stow "
            f"{c['stow_position'] or '—'}. Vessel ETA {t['sea_arrival']:%d %b %H:%M}Z → unload "
            f"done {t['unload_end']:%H:%M}Z → port depot {t['depot_arrive']:%H:%M}Z → road "
            f"{t['road_depart']:%H:%M}Z → PSCH receipt ETA {receipt:%H:%M}Z. Inbound arrival "
            f"rate {rate:.1f} ctn/h → {n_areas} receiving area(s) opened; assigned "
            f"{area} (Door {door}). Slotting: pallet pick {pick:%d %b %H:%M}Z → "
            f"{dwell_h:.1f}h dwell → level {level} (fast movers at floor level, "
            f"slow movers high); robot putaway to {bin_location} in the {zone_txt}."
        )
        plans.append(
            MccPlan(
                container_id=c["container_id"],
                carrying_vessel_id=vessel["voyage_id"],
                carrying_vessel_name=vessel["vessel_name"],
                vessel_destination=vessel.get("destination"),
                vessel_distance_nm=vessel.get("distance_nm"),
                vessel_speed_knots=vessel.get("speed_knots"),
                stow_position=c.get("stow_position"),
                sea_arrival=t["sea_arrival"],
                unload_end=t["unload_end"],
                depot_arrive=t["depot_arrive"],
                road_depart=t["road_depart"],
                psch_receipt_eta=receipt,
                receiving_area=f"{area} · Door {door}",
                staging_start=staging_start,
                staging_end=staging_end,
                move_start=move_start,
                move_end=move_end,
                bin_location=bin_location,
                putaway_robot=robot,
                pallet_pick_time=pick,
                release_lane=release_lane,
                consolidation_group=group_by_container.get(c["container_id"]),
                reasoning=reasoning,
            )
        )

    # ---- 5. Persist + trace ----------------------------------------------------
    store.save_mcc_plans(plans, store_path)
    store.save_outbound_containers(outbounds, store_path)

    by_level: dict[int, int] = {}
    for p in plans:
        level = level_of_bin(bin_id_of(p.bin_location))
        by_level[level] = by_level.get(level, 0) + 1
    store.record_event(
        "agent",
        "slotting_applied",
        {
            "rule": "dwell-based height: cargo released soon at floor level, slow movers high",
            "containers_by_level": by_level,
        },
        store_path,
    )

    for o in outbounds:
        store.record_event(
            "agent",
            "consolidation_group_planned",
            {"outbound_container": o.container_id, "destination": o.destination,
             "bound_vessel": o.bound_vessel_id, "sources": len(o.source_container_ids)},
            store_path,
        )
    store.record_event(
        "agent",
        "mcc_plan_computed",
        {"containers_planned": len(plans), "outbound_groups": len(outbounds),
         "receiving_areas_opened": n_areas, "arrival_rate_ctn_per_hr": round(rate, 2)},
        store_path,
    )
