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

import math
import random
from datetime import datetime, timedelta

from config import (
    DB_PATH,
    MCC_GROUP_SIZE,
    MOVE_TO_BIN_MIN,
    PALLETS_PER_LANE,
    PSCH_HAZMAT_AISLE,
    PUTAWAY_ROBOTS,
    RECEIVING_AREAS,
    RELEASING_LANES_TOTAL,
    ROAD_TRANSIT_MIN,
    SEED,
    SIM_NOW,
    STAGING_MIN,
    UNLOAD_MIN,
    YARD_TRANSFER_MIN,
)
from agents.tools import call_tool
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

    ``psch_receipt_eta`` is the *promised* receipt time; a deterministic subset
    of containers carries ``delay_hours`` (road congestion), so their actual
    arrival slips to ``psch_receipt_eta + delay_hours``. Every page derives
    from this same function, so the delay shows consistently everywhere (and
    the exception agent flags the slip).
    """
    if plan is None:
        return JourneyStatus.EN_ROUTE_SEA.value
    if sim_now < plan["sea_arrival"]:
        return JourneyStatus.EN_ROUTE_SEA.value
    if sim_now < plan["depot_arrive"]:
        return JourneyStatus.UNLOADED.value
    if sim_now < plan["road_depart"]:
        return JourneyStatus.AT_DEPOT.value
    delay = timedelta(hours=float(plan.get("delay_hours") or 0.0))
    if sim_now < plan["psch_receipt_eta"] + delay:
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


def _receiving_areas_for_rate(rate: float) -> int:
    """Open receiving areas (lanes) based on inbound volume rate (containers/hour)."""
    if rate < 1.0:
        return 2
    if rate < 2.0:
        return 4
    if rate < 3.5:
        return 6
    return min(10, len(RECEIVING_AREAS))


# Deterministic road-delay layer: a small subset of containers runs late so
# the terrarium has real exceptions for the exception agent to catch (and the
# demo can show the agent reacting to the world). Chosen by (SEED, container)
# so every run of the same seed produces the same delays.
DELAY_CHOICES = (1.0, 2.0, 3.0, 5.0, 8.0)
DELAY_RATE = 0.12  # ~12% of the wave runs late


def _delay_hours(container_id: str) -> float:
    h = f"{SEED}:road-delay:{container_id}"
    r = random.Random(h)
    return DELAY_CHOICES[r.randrange(len(DELAY_CHOICES))] if r.random() < DELAY_RATE else 0.0


def _delay_note(delay_h: float, receipt: datetime) -> str:
    if not delay_h:
        return ""
    slipped = receipt + timedelta(hours=delay_h)
    return (
        f"\n⚠ ROAD DELAY +{delay_h:g}h — receipt slips from {receipt:%d %b %H:%M}Z "
        f"to {slipped:%d %b %H:%M}Z (en-route congestion)"
    )


def _bin_pool() -> dict[tuple[str, int], list[str]]:
    """Bins grouped by storage zone and rack level (AISLE-LEVEL-BAY naming).

    Bins are named the conventional DC way -- AISLE-LEVEL-BAY ("1-12-2A" =
    Aisle 1, Level 12, Bay 2A). The slotting rule chooses the level (height)
    from the predicted dwell; these pools hand out the next free bin at that
    level within the right zone: cold room for reefer cargo, the segregated
    hazmat aisle for dangerous goods, ambient aisles for the rest.

    Each level's pool is shuffled with a fixed per-(zone, level) seed so
    consecutive putaways SPREAD across every aisle of the zone instead of
    filling aisle 1, then aisle 2, ... -- a busy rack has cargo in all its
    aisles at once. The result is still fully deterministic per scenario.
    """
    from data.facility import ROOMS

    ambient = iter_bins("ambient")
    zone_bins = {
        "ambient": [b for b in ambient if aisle_of_bin(b) != PSCH_HAZMAT_AISLE],
        "hazmat": [b for b in ambient if aisle_of_bin(b) == PSCH_HAZMAT_AISLE],
        "cold_room": iter_bins("cold_room"),
    }
    levels = ROOMS["ambient"]["levels"]
    pools: dict[tuple[str, int], list[str]] = {}
    for zone, bins in zone_bins.items():
        for level in range(1, levels + 1):
            pool = [b for b in bins if level_of_bin(b) == level]
            random.Random(f"{SEED}:bin-pool:{zone}:{level}").shuffle(pool)
            pools[(zone, level)] = pool
    return pools


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


def _lcl_unit_id(rng: random.Random, seen: set[str]) -> str:
    while True:
        cid = f"LCLU90{rng.randint(10000, 99999)}"
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


def _allocate_releasing_lanes(outbounds: list[OutboundContainer]) -> None:
    """Reserve releasing lanes time-aware so the dispatch area cycles.

    Every consolidation box needs ``ceil(pallets / PALLETS_PER_LANE)``
    contiguous lanes for its stuffing window. Windows that do not overlap
    reuse the same lanes, exactly like a real dispatch area whose bays are
    freed when a box is sealed and trucked to the quay. Each lane belongs to
    at most one box at a time.
    """
    if not outbounds:
        return
    earliest = min(o.stuffing_start for o in outbounds)
    lane_free_until: list[datetime] = [earliest] * (RELEASING_LANES_TOTAL + 1)
    for o in sorted(outbounds, key=lambda x: (x.stuffing_start, x.stuffing_end)):
        span = max(1, math.ceil(len(o.source_shipment_ids) / PALLETS_PER_LANE))
        span = min(span, RELEASING_LANES_TOTAL)
        start = next(
            (
                s
                for s in range(1, RELEASING_LANES_TOTAL - span + 2)
                if all(lane_free_until[i] <= o.stuffing_start for i in range(s, s + span))
            ),
            None,
        )
        if start is None:  # everything busy: reuse the span freed earliest
            start = min(
                range(1, RELEASING_LANES_TOTAL - span + 2),
                key=lambda s: max(lane_free_until[i] for i in range(s, s + span)),
            )
        end = start + span - 1
        for i in range(start, end + 1):
            lane_free_until[i] = o.stuffing_end
        o.staging_lane_start = start
        o.staging_lane_end = end


def plan(store_path=DB_PATH, sim_now: datetime = SIM_NOW) -> None:
    """Read the world, derive the full MCC plan, and persist it."""
    vessels = call_tool("list_vessels", {}, store_path)
    containers = call_tool("list_containers", {}, store_path)
    shipments = call_tool("list_shipments", {}, store_path)

    vessel_by_id = {v["voyage_id"]: v for v in vessels}
    shipments_by_container: dict[str, list[dict]] = {}
    for s in shipments:
        if s.get("source_container_id"):
            shipments_by_container.setdefault(s["source_container_id"], []).append(s)

    # Every PSCH-bound container (all five service flows) enters the same
    # port->PSCH journey pipeline; only the downstream handling differs.
    by_flow: dict[str, list[dict]] = {}
    for c in containers:
        if c["cargo_flag"] == "deconsolidation_required":
            by_flow.setdefault(c.get("flow") or "mcc", []).append(c)
    bin_flows = ("mcc", "lcl")  # deconsolidated into rack bins
    whole_flows = ("fcl", "topup", "transload")  # handled whole at staging

    # ---- 1. Journey timeline per container ------------------------------------
    timelines: list[dict] = []
    for idx, c in enumerate([x for f in ("mcc", "lcl", "fcl", "topup", "transload") for x in by_flow.get(f, [])]):
        vessel = vessel_by_id.get(c["voyage_id"])
        if vessel is None or vessel.get("eta") is None:
            continue
        sea_arrival: datetime = vessel["eta"]
        dwell = DEPOT_DWELL_CHOICES[idx % len(DEPOT_DWELL_CHOICES)]
        unload_end = sea_arrival + timedelta(minutes=UNLOAD_MIN)
        depot_arrive = unload_end + timedelta(minutes=YARD_TRANSFER_MIN)
        # Road dispatch is jittered per container (customs holds, truck
        # availability, driver shifts) so receipts at the PSCH doorstep flow
        # CONTINUOUSLY instead of bunching into tight vessel-ETA clusters with
        # multi-hour dry spells — without this the live dashboard sits frozen
        # between clusters. The jitter is deterministic per container id.
        dispatch = dwell + random.Random(f"arrival-jitter:{c['container_id']}").uniform(-270, 270)
        road_depart = depot_arrive + timedelta(minutes=max(30, dispatch))
        receipt = road_depart + timedelta(minutes=ROAD_TRANSIT_MIN)
        timelines.append(
            {
                "container": c,
                "vessel": vessel,
                "flow": c.get("flow") or "mcc",
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
    stowage = call_tool("list_vessel_stowage", {}, store_path)
    stow_by_vessel: dict[str, list[dict]] = {}
    for s in stowage:
        stow_by_vessel.setdefault(s["vessel_id"], []).append(s)

    rng = random.Random(
        hash(tuple(sorted(t["container"]["container_id"] for t in timelines))) & 0xFFFF
    )
    seen_outbound: set[str] = set()

    # LCL delivery units: group LCL-flow cargo by its land destination; the
    # pallet pick for each member is timed against the unit's build window.
    lcl_pick: dict[str, datetime] = {}
    lcl_group: dict[str, str] = {}
    lcl_by_dest: dict[str, list[dict]] = {}
    for t in timelines:
        if t["flow"] == "lcl" and (t["container"].get("destination") or ""):
            lcl_by_dest.setdefault(t["container"]["destination"], []).append(t)
    for members in lcl_by_dest.values():
        unit_id = _lcl_unit_id(rng, seen_outbound)
        build_start = max(m["receipt"] for m in members)
        for m in members:
            cid = m["container"]["container_id"]
            lcl_pick[cid] = build_start + timedelta(minutes=45)
            lcl_group[cid] = unit_id

    # ---- 2. Consolidation: MCC cargo groups by final destination --------------
    # Only the mcc flow re-sails on vessels; LCL cargo leaves by land, FCL /
    # top-up / transload containers are handled whole (see step 4).
    by_dest: dict[str, list[dict]] = {}
    for t in timelines:
        if t["flow"] != "mcc":
            continue
        dest = t["vessel"].get("destination") or "—"
        by_dest.setdefault(dest, []).append(t)

    pick_by_container: dict[str, datetime] = {
        t["container"]["container_id"]: t["receipt"] for t in timelines
    }
    group_by_container: dict[str, str] = {}
    outbounds: list[OutboundContainer] = []
    # Releasing lanes are allocated AFTER all boxes are planned: the dispatch
    # area is a cycling staging buffer (see _allocate_releasing_lanes below).
    rel_lanes_by_group: dict[str, str] = {}
    used_load_cells: set[tuple] = set()

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

        # A vessel call takes several consolidation boxes: chunk the inbound
        # MCC cargo by receipt order so each box is stuffed from a manageable
        # pallet group (a real CFS builds multiple boxes per vessel call).
        chunks = [
            eligible[i : i + MCC_GROUP_SIZE]
            for i in range(0, len(eligible), MCC_GROUP_SIZE)
        ]
        for chunk_idx, chunk in enumerate(chunks):
            ob_id = _outbound_id(rng, seen_outbound)
            source_container_ids = [m["container"]["container_id"] for m in chunk]
            source_shipment_ids = [
                s["shipment_id"]
                for cid in source_container_ids
                for s in shipments_by_container.get(cid, [])
            ]
            # Boxes are stuffed one after another: stagger each pick slightly
            # so dwell levels differ and the schedule reads real.
            stagger = timedelta(minutes=45 * chunk_idx)
            lane = f"Lane {1 + (len(outbounds) % 4)}"
            lane_release = stuffing_end
            road_depart = lane_release + timedelta(minutes=ROAD_BACK_MIN)
            eta_loading = road_depart + timedelta(minutes=ROAD_BACK_MIN)
            # Loading cell on the bound vessel: re-use a cell freed by discharge
            # (an MCC cell of that vessel), so the outbound box takes a real,
            # bay-parity-correct slot in the vessel's bay plan. No two boxes
            # take the same cell.
            vessel_cells = stow_by_vessel.get(bound["voyage_id"], [])
            target_pool = [
                s
                for s in vessel_cells
                if s["is_mcc"]
                and (bound["voyage_id"], s["bay"], s["stack"], s["tier"]) not in used_load_cells
            ] or [s for s in vessel_cells if s["bay"] % 2 == 0]
            target = rng.choice(target_pool) if target_pool else None
            if target is not None:
                stow_bay, stow_row, stow_tier = target["bay"], target["stack"], target["tier"]
                stow = f"Bay {_bay_label(stow_bay)} · Row {stow_row:02d} · Tier {stow_tier:02d}"
                used_load_cells.add((bound["voyage_id"], stow_bay, stow_row, stow_tier))
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
                f"Consolidation: {len(chunk)} inbound containers ({dest} cargo) "
                f"pallet-picked and stuffed into {ob_id}\n"
                f"Vessel: {bound['vessel_name']} ({bound['voyage_id']}) — {vessel_pic}\n"
                f"Departure: {vessel_etd:%d %b %H:%M}Z from berth {bound['berth_id']}\n"
                f"Loading window: to {cutoff:%H:%M}Z (container at quay 4h before sailing) · "
                f"stuffing {stuffing_start:%H:%M}-{stuffing_end:%H:%M}Z\n"
                f"Stowage cell: {stow}\n"
                f"Lane release: {lane} @ {lane_release:%H:%M}Z · ETA quay loading "
                f"area {eta_loading:%H:%M}Z"
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
                    stuffing_start=stuffing_start + stagger,
                    stuffing_end=stuffing_end,
                    lane_release_time=lane_release,
                    loading_lane=lane,
                    road_depart=road_depart,
                    eta_loading_area=eta_loading,
                    status=OutboundStatus.STAGED,
                    staging_lane_start=None,
                    staging_lane_end=None,
                    reasoning=reasoning,
                )
            )

            # Each member's pallet pick is timed against the stuffing window
            # (staggered per box): pallets are picked when the box is stuffed.
            for m in chunk:
                pick = max(m["receipt"], stuffing_start + PALLET_PICK_BUFFER + stagger)
                cid = m["container"]["container_id"]
                pick_by_container[cid] = pick
                group_by_container[cid] = ob_id

    # Releasing lanes: the dispatch area is a CYCLING STAGING BUFFER. The 40
    # physical lanes are reserved for each box only during its stuffing window;
    # once a box is stuffed and released, its lanes free up for the next group.
    _allocate_releasing_lanes(outbounds)
    for o in outbounds:
        span = _rel_span_label(o.staging_lane_start or 1, o.staging_lane_end or 1)
        rel_lanes_by_group[o.container_id] = span
        o.reasoning += (
            f"\nStaging at PSCH: {len(o.source_shipment_ids)} pallets on releasing "
            f"lane(s) {span} during the stuffing window (one lane per "
            f"{PALLETS_PER_LANE} pallets; the lanes are a cycling buffer, freed "
            f"for the next box once this one is stuffed)"
        )

    # ---- 3. Receiving plan: areas opened by the inbound arrival rate ----------
    window_start, window_end = sim_now, sim_now + timedelta(hours=RATE_WINDOW_HOURS)
    arriving = [t["receipt"] for t in timelines if window_start <= t["receipt"] < window_end]
    rate = len(arriving) / RATE_WINDOW_HOURS
    n_areas = _receiving_areas_for_rate(rate)
    open_areas = RECEIVING_AREAS[:n_areas]

    # ---- 4. Robot putaway (bin flows) / whole-container staging (others) -----
    # The slotting agent optimises the storage HEIGHT of the container placed:
    # cargo released soon (short predicted dwell before its pallets are picked)
    # is put at the floor levels for fast robot retrieval; slower movers go
    # higher up the rack. Whole-container flows (FCL / Top Up / Transload) do
    # not deconsolidate into rack bins — they are staged and released whole.
    bin_pool = _bin_pool()
    bin_cursor: dict[tuple[str, int], int] = {(z, l): 0 for (z, l) in bin_pool}
    bin_used = 0
    whole_idx = 0
    plans: list[MccPlan] = []

    for idx, t in enumerate(sorted(timelines, key=lambda x: x["receipt"])):
        c, vessel = t["container"], t["vessel"]
        flow = t["flow"]
        receipt = t["receipt"]
        area = open_areas[idx % len(open_areas)]
        door = 1 + (idx % 4)
        staging_start = receipt
        staging_end = staging_start + timedelta(minutes=STAGING_MIN)
        move_start = staging_end
        move_end = move_start + timedelta(minutes=MOVE_TO_BIN_MIN)
        cid = c["container_id"]
        delay_h = _delay_hours(cid)

        special = c.get("special_handling") or []
        if "reefer" in special:
            zone = "cold_room"
        elif "hazmat" in special:
            zone = "hazmat"
        else:
            zone = "ambient"
        zone_txt = {
            "cold_room": "cold room (reefer)",
            "hazmat": f"segregated hazmat aisle {PSCH_HAZMAT_AISLE}",
            "ambient": "ambient",
        }[zone]

        if flow in bin_flows:
            pick = pick_by_container.get(cid) or lcl_pick.get(cid, receipt)
            dwell_h = max(0.0, (pick - receipt).total_seconds() / 3600)
            level = _dwell_level(dwell_h)
            pool = bin_pool[(zone, level)]
            bid = pool[bin_cursor[(zone, level)] % len(pool)]
            bin_cursor[(zone, level)] += 1
            bin_location = f"Bin {bid}"
            bin_used += 1
            robot = f"Stacker {1 + (bin_used % PUTAWAY_ROBOTS):02d}"
            # The cargo is staged at its consolidation group's releasing lanes
            # (or, for cargo not yet grouped, at a default lane by rotation).
            release_lane = rel_lanes_by_group.get(
                group_by_container.get(cid) or cid, str(1 + (idx % RELEASING_LANES_TOTAL))
            )
            consol_group = group_by_container.get(cid) or lcl_group.get(cid)
        else:
            # Whole-container flows: staged in a dedicated slot/bay, released
            # whole by land (FCL / topped-up FCL) or transferred (transload).
            whole_idx += 1
            robot = "—"
            if flow == "fcl":
                bin_location = f"Yard Slot F-{1 + (whole_idx % 24):02d}"
                pick = receipt + timedelta(hours=3 + (idx % 6))
                release_lane = f"Gate {chr(65 + (idx % 8))}"
                consol_group = f"FCL-{whole_idx:02d}"
            elif flow == "topup":
                bin_location = f"Top-up Bay {1 + (whole_idx % 10)}"
                pick = receipt + timedelta(hours=4 + (idx % 5))
                release_lane = f"Gate {chr(65 + (idx % 8))}"
                consol_group = f"TOPUP-{whole_idx:02d}"
            else:  # transload
                bin_location = f"Transload Bay {1 + (whole_idx % 8)}"
                pick = receipt + timedelta(hours=2 + (idx % 4))
                release_lane = f"Transfer Dock {1 + (idx % 4)}"
                consol_group = f"TRNS-{whole_idx:02d}"
            move_end = pick

        if flow == "mcc":
            dest_txt = vessel.get("destination") or "—"
            stage_txt = (
                f"Slotting: pallet pick {pick:%d %b %H:%M}Z → "
                f"{max(0.0, (pick - receipt).total_seconds() / 3600):.1f}h dwell → "
                f"level {_dwell_level(max(0.0, (pick - receipt).total_seconds() / 3600))} "
                "(fast movers at floor level, slow movers high); stacker putaway to "
                f"{bin_location} in the {zone_txt}."
            )
        elif flow == "lcl":
            dest_txt = c.get("destination") or "—"
            stage_txt = (
                f"LCL delivery unit {consol_group} to {dest_txt}: cargo deconsolidated "
                f"to {bin_location}, pallets picked {pick:%d %b %H:%M}Z for the delivery "
                "build window (fast mover → floor level), staged on releasing lane "
                f"{release_lane} for the land truck."
            )
        elif flow == "fcl":
            dest_txt = c.get("destination") or "—"
            stage_txt = (
                f"FCL land release to {dest_txt}: container handled whole, staged at "
                f"{bin_location} after receiving, released via {release_lane} at "
                f"{pick:%d %b %H:%M}Z for the land truck (destination ETA +2h). "
                "No deconsolidation — the container is the delivery unit."
            )
        elif flow == "topup":
            dest_txt = c.get("destination") or "—"
            stage_txt = (
                f"Top-up / reconsolidation at {bin_location}: the partially filled "
                f"container is staged {receipt:%H:%M}Z, additional cargo consolidated "
                f"in, sealed, and released via {release_lane} at {pick:%d %b %H:%M}Z "
                f"bound for {dest_txt}. (Workflow pre-wired for the AI agent.)"
            )
        else:  # transload
            dest_txt = "PSCH (transload)"
            stage_txt = (
                f"Transloading at {bin_location}: cargo transferred from this container "
                f"into target unit {consol_group} in the {pick:%d %b %H:%M}Z window, "
                f"then the emptied box is released at {release_lane}."
            )
        reasoning = (
            f"Journey: {vessel['vessel_name']} ({vessel['voyage_id']}) · stow "
            f"{c['stow_position'] or '—'}\n"
            f"Timeline: sea ETA {t['sea_arrival']:%d %b %H:%M}Z → unload "
            f"{t['unload_end']:%H:%M}Z → port depot {t['depot_arrive']:%H:%M}Z → road "
            f"{t['road_depart']:%H:%M}Z → PSCH receipt ETA {receipt:%H:%M}Z\n"
            f"Receiving: {area} (Door {door}) · {n_areas} area(s) opened at "
            f"{rate:.1f} ctn/h\n"
            f"Flow: {flow}\n"
            f"{stage_txt}"
        )
        plans.append(
            MccPlan(
                container_id=cid,
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
                consolidation_group=consol_group,
                flow=flow,
                destination=c.get("destination"),
                delay_hours=delay_h,
                reasoning=reasoning + _delay_note(delay_h, receipt),
            )
        )

    # ---- 5. Persist + trace ----------------------------------------------------
    # Plans are written as *proposals* through the tool registry (mutate tools),
    # so the trace records exactly what the agent proposed for human review.
    call_tool("save_mcc_plans", {"plans": [p.model_dump(mode="json") for p in plans]}, store_path)
    call_tool(
        "save_outbound_containers",
        {"outbound": [o.model_dump(mode="json") for o in outbounds]},
        store_path,
    )

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
