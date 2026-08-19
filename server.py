"""Classic early-2000s control-tower frontend, served by the Python stdlib.

The MCC control tower gives one shared view of the multi-country consolidation
cargo story: the inbound container list (each clickable to open a ship-tracker
sidebar), the Tuas berth plan map (with the berth rectangle of the vessel each
outbound container is bound for), the agent's PSCH receiving/putaway plan, and
the outbound consolidation schedule. Same SQLite data layer as the Streamlit
UI; no extra dependencies.

Run:
    python server.py          # then open http://127.0.0.1:8513
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from config import DB_PATH, N_CONTAINERS, SEED, SIM_NOW, sim_now
from data import store
from data.facility import build_psch_space
from data.seed import seed, seed_if_empty
from data.simulator import LOCAL_DESTINATIONS
from agents import mcc_planner
from agents.exception import scan_exceptions
from agents.intel import default_intel_brain
from agents.runtime import AgentRuntime
from analysis.bayplan import build_bay_plan
from analysis.kpis import compute_kpis
from analysis.psch_view import FACILITY_CSS

PORT = int(os.environ.get("PSA_PORT", "8513"))

# --- Static facility map --------------------------------------------------------
# The berth-plan graphic lives at the project root as Map.png. Each berth is a
# clickable rectangle on the graphic; occupancy comes from the vessels table.
STATIC_DIR = Path(__file__).parent / "static"
MAP_FILE = Path(__file__).resolve().parent / "Map.png"

# Berth rectangles as % of the 1450x1452 map image (left, top, width, height).
# Each rectangle sits exactly on the berth marker drawn on the aerial graphic
# (top pier -> bottom slipway, left to right within each row):
#   B1-B3  top pier finger              B7-B8   center-middle slipway
#   B4-B6  upper-middle slipway         B9-B11  lower-middle quayside
#   B12-B13 lower pier edge             B14-B15 bottom slipway
BERTHS = [
    {"id": "B1", "pier": 1, "x": 37.9, "y": 26.6, "w": 10.5, "h": 2.1},
    {"id": "B2", "pier": 1, "x": 51.5, "y": 26.6, "w": 9.4, "h": 2.1},
    {"id": "B3", "pier": 1, "x": 62.8, "y": 26.6, "w": 10.6, "h": 2.1},
    {"id": "B4", "pier": 2, "x": 37.9, "y": 33.2, "w": 10.5, "h": 2.1},
    {"id": "B5", "pier": 2, "x": 51.6, "y": 33.2, "w": 9.3, "h": 2.1},
    {"id": "B6", "pier": 2, "x": 63.4, "y": 33.2, "w": 10.0, "h": 2.1},
    {"id": "B7", "pier": 3, "x": 36.8, "y": 47.1, "w": 11.1, "h": 2.1},
    {"id": "B8", "pier": 3, "x": 50.6, "y": 47.1, "w": 11.4, "h": 2.1},
    {"id": "B9", "pier": 4, "x": 36.6, "y": 53.4, "w": 11.3, "h": 2.1},
    {"id": "B10", "pier": 4, "x": 50.6, "y": 53.4, "w": 11.3, "h": 2.1},
    {"id": "B11", "pier": 4, "x": 66.3, "y": 53.4, "w": 11.4, "h": 2.1},
    {"id": "B12", "pier": 5, "x": 45.3, "y": 67.1, "w": 11.4, "h": 2.1},
    {"id": "B13", "pier": 5, "x": 59.0, "y": 67.1, "w": 11.4, "h": 2.1},
    {"id": "B14", "pier": 6, "x": 36.1, "y": 73.3, "w": 11.4, "h": 2.1},
    {"id": "B15", "pier": 6, "x": 51.5, "y": 73.3, "w": 11.4, "h": 2.1},
]


def _iso(value):
    return value.isoformat() if value is not None else None


def _type_code(special: list[str]) -> str:
    """Industry container-type labels (ISO 6346 / port terminal convention).

    GP = general purpose, RF = reefer/refrigerated, DG = dangerous goods,
    OOG = out of gauge (oversized). High-cube is carried by the size label
    (40HC).
    """
    if "reefer" in special:
        return "RF"
    if "hazmat" in special:
        return "DG"
    if "oversized" in special:
        return "OOG"
    return "GP"


def _bay_label(bay) -> str:
    """40ft bays live in even numbers and span the preceding odd bay (33(34))."""
    if not bay:
        return "—"
    return f"{bay - 1}({bay})" if bay % 2 == 0 else f"{bay}"


def _serialise_vessel(v: dict) -> dict:
    """Vessel row -> JSON-safe dict for the frontend."""
    return {
        "voyage_id": v["voyage_id"],
        "vessel_name": v["vessel_name"],
        "status": v["status"],
        "berth_id": v["berth_id"],
        "eta": _iso(v["eta"]),
        "etd": _iso(v["etd"]),
        "moves_planned": v["moves_planned"],
        "destination": v.get("destination"),
        "distance_nm": v.get("distance_nm"),
        "speed_knots": v.get("speed_knots"),
    }


def _berth_state(vessels: list[dict]) -> list[dict]:
    """Static berth geometry, joined with whichever vessel occupies each berth."""
    by_berth = {v["berth_id"]: v for v in vessels if v.get("berth_id")}
    return [
        {
            "id": b["id"],
            "pier": b["pier"],
            "x": b["x"],
            "y": b["y"],
            "w": b["w"],
            "h": b["h"],
            "vessel": _serialise_vessel(by_berth[b["id"]]) if b["id"] in by_berth else None,
        }
        for b in BERTHS
    ]


def _wave_complete(now: datetime) -> bool:
    """True when the current wave has run its full lifecycle.

    Every inbound container's pallets have been picked and every outbound
    consolidation container has been loaded back to the port (or an LCL/FCL
    release has rolled out by land). At that point the terrarium regenerates
    a fresh wave so the ecosystem keeps living.
    """
    plans = store.get_mcc_plans(DB_PATH)
    outbounds = store.get_outbound_containers(DB_PATH)
    if not plans or not outbounds:
        return False
    if any(now < p["pallet_pick_time"] for p in plans):
        return False
    return all(now >= o["eta_loading_area"] for o in outbounds)


def _maybe_regenerate_wave(now: datetime) -> None:
    """Seed a fresh wave (and re-run the planner) once the current one is done.

    Called from every state poll, so the world advances on its own: containers
    arrive, get unloaded, put away, picked, released and loaded out; when the
    last outbound container is loaded, a new wave is seeded relative to the
    live clock and the cycle repeats — the whole thing is a terrarium.
    """
    if not _wave_complete(now):
        return
    store.record_event(
        "system",
        "wave_complete_regenerating",
        {"sim_now": _iso(now), "note": "full lifecycle finished — seeding the next wave"},
        DB_PATH,
    )
    seed(db_path=DB_PATH, sim_now=now)
    mcc_planner.plan(DB_PATH, sim_now=now)


# Vessel stowage is big (~40k cells) and only needed when a bay plan is
# actually viewed, so it is served on demand and cached per DB file (the file
# mtime changes when a wave regenerates, which busts the cache).
_STOW_BY_BAY_CACHE: tuple[int, dict] | None = None


def _stow_by_bay() -> dict:
    global _STOW_BY_BAY_CACHE
    mtime = DB_PATH.stat().st_mtime_ns
    if _STOW_BY_BAY_CACHE is None or _STOW_BY_BAY_CACHE[0] != mtime:
        by_bay: dict[tuple, list[dict]] = {}
        for s in store.get_vessel_stowage(DB_PATH):
            by_bay.setdefault((s["vessel_id"], s["bay"]), []).append(s)
        _STOW_BY_BAY_CACHE = (mtime, by_bay)
    return _STOW_BY_BAY_CACHE[1]


def _bayplan_for_container(container_id: str) -> str:
    """Bay-plan HTML for one container, built on demand (never in the polled state)."""
    by_bay = _stow_by_bay()
    # Inbound containers carry their structured stow cell on the CONTAINER
    # record (the plan only keeps the human-readable stow_position string).
    c = next(
        (x for x in store.get_containers(DB_PATH) if x["container_id"] == container_id),
        None,
    )
    if c is not None and c.get("stow_bay"):
        entry = {
            "container_id": container_id,
            "bay_label": _bay_label(c.get("stow_bay")),
            "bay_cells": by_bay.get((c["voyage_id"], c.get("stow_bay")), []),
        }
        return build_bay_plan(entry, clickable=True)
    o = next(
        (
            x
            for x in store.get_outbound_containers(DB_PATH)
            if x["container_id"] == container_id
        ),
        None,
    )
    if o is not None and o.get("stow_row"):
        entry = {
            "container_id": container_id,
            "bay_label": _bay_label(o.get("stow_bay")),
            "bay_cells": by_bay.get((o["bound_vessel_id"], o.get("stow_bay")), []),
        }
        return build_bay_plan(entry, clickable=False, target=(o["stow_row"], o["stow_tier"]))
    return ""


def build_state() -> dict:
    """Assemble the full, JSON-serialisable view state for the frontend."""
    now = sim_now()
    _maybe_regenerate_wave(now)
    containers = store.get_containers(DB_PATH)
    vessels = store.get_vessels(DB_PATH)
    plans = store.get_mcc_plans(DB_PATH)
    outbounds = store.get_outbound_containers(DB_PATH)

    # The agent's plan is (re)computed automatically when the world is fresh
    # (after seeding) and on demand via the "Run MCC Planner" button.
    if not plans and any(
        c["cargo_flag"] == "deconsolidation_required" for c in containers
    ):
        mcc_planner.plan(DB_PATH)
        plans = store.get_mcc_plans(DB_PATH)
        outbounds = store.get_outbound_containers(DB_PATH)

    yard = store.get_yard_status(DB_PATH)
    drayage = store.get_drayage(DB_PATH)
    trace = store.get_trace(path=DB_PATH)
    # Exception watch: pure scan over data already loaded (no tool calls, so
    # the 8s poll never floods the trace). The exception agent on the Control
    # Tower surfaces exactly what needs attention right now.
    exceptions = scan_exceptions(plans, outbounds, containers, vessels, now=now)
    # KPIs must be derived from the LIVE sim clock (same `now` as the storage
    # view) or the whole KPI strip would sit frozen at SIM_NOW while the
    # facility churns underneath it.
    kpis = compute_kpis(containers, plans, outbounds, yard, drayage, sim_now=now)

    # PSCH space-utilisation view: every rack, bin and staging lane with the
    # agent's assignments colour-coded by the container's journey status.
    shipments = store.get_shipments(DB_PATH)
    psch = build_psch_space(plans, shipments, outbounds, sim_now=now)
    shipments_by_cid: dict[str, list[dict]] = {}
    for s in shipments:
        shipments_by_cid.setdefault(s.get("source_container_id") or "", []).append(s)

    vessel_by_id = {v["voyage_id"]: v for v in vessels}
    plan_by_id = {p["container_id"]: p for p in plans}

    inbound = []
    for c in containers:
        if c["cargo_flag"] != "deconsolidation_required":
            continue
        p = plan_by_id.get(c["container_id"])
        if p is None:
            continue
        v = vessel_by_id.get(p["carrying_vessel_id"], {})
        status = mcc_planner.journey_status(p, now)
        hours_until = (p["psch_receipt_eta"] - now).total_seconds() / 3600
        entry = {
            "container_id": c["container_id"],
            "size": c["size_type"],
            "type": _type_code(c["special_handling"]),
            "customs": c["customs_status"],
            "status": status,
            "vessel_name": p["carrying_vessel_name"],
            "vessel_id": p["carrying_vessel_id"],
            "vessel_status": v.get("status", ""),
            "berth_id": v.get("berth_id"),
            "distance_nm": p["vessel_distance_nm"],
            "speed_knots": p["vessel_speed_knots"],
            "destination": p["vessel_destination"],
            "sea_arrival": _iso(p["sea_arrival"]),
            "depot_arrive": _iso(p["depot_arrive"]),
            "road_depart": _iso(p["road_depart"]),
            "psch_receipt_eta": _iso(p["psch_receipt_eta"]),
            "hours_until": round(hours_until, 1),
            "stow": p["stow_position"],
            "stow_bay": c.get("stow_bay"),
            "stow_row": c.get("stow_row"),
            "stow_tier": c.get("stow_tier"),
            "bay_label": _bay_label(c.get("stow_bay")),
            "receiving_area": p["receiving_area"],
            "staging_start": _iso(p["staging_start"]),
            "staging_end": _iso(p["staging_end"]),
            "move_start": _iso(p["move_start"]),
            "move_end": _iso(p["move_end"]),
            "bin_location": p["bin_location"],
            "putaway_robot": p["putaway_robot"],
            "pallet_pick_time": _iso(p["pallet_pick_time"]),
            "release_lane": p["release_lane"],
            "consolidation_group": p["consolidation_group"],
            "flow": p.get("flow") or "mcc",
            "destination": p.get("destination")
            or (p.get("vessel_destination") if (p.get("flow") or "mcc") == "mcc" else None),
            "cargoes": len(shipments_by_cid.get(c["container_id"], [])),
            "reasoning": p["reasoning"],
        }
        # Bay plan is served on demand (/api/bayplan) and cached client-side,
        # so the 8s state poll stays lean even with hundreds of containers.
        inbound.append(entry)
    inbound.sort(key=lambda x: x["psch_receipt_eta"])

    outbound = []
    for o in outbounds:
        v = vessel_by_id.get(o["bound_vessel_id"], {})
        status = mcc_planner.outbound_status(o, now)
        hours = (o["eta_loading_area"] - now).total_seconds() / 3600
        etd = o.get("vessel_etd")
        loading_end = etd - timedelta(hours=2) if etd else None
        cutoff = etd - timedelta(hours=4) if etd else None
        hours_dep = (etd - now).total_seconds() / 3600 if etd else None
        hours_end = (loading_end - now).total_seconds() / 3600 if loading_end else None
        entry = {
            "container_id": o["container_id"],
            "destination": o["destination"],
            "size": o.get("size_type") or "40HC",
            "source_containers": o["source_container_ids"],
            "source_shipments": o["source_shipment_ids"],
            "bound_vessel_id": o["bound_vessel_id"],
            "bound_vessel_name": o["bound_vessel_name"],
            "vessel_status": v.get("status", ""),
            "vessel_eta_berth": _iso(v.get("eta")),
            "vessel_distance_nm": v.get("distance_nm"),
            "vessel_speed_knots": v.get("speed_knots"),
            "vessel_destination": v.get("destination"),
            "vessel_moves": v.get("moves_planned"),
            "berth_id": v.get("berth_id"),
            "vessel_etd": _iso(etd),
            "hours_to_departure": round(hours_dep, 1) if hours_dep is not None else None,
            "loading_cutoff": _iso(cutoff),
            "loading_end": _iso(loading_end),
            "hours_to_loading_end": round(hours_end, 1) if hours_end is not None else None,
            "stow_position": o.get("stow_position"),
            "stow_bay": o.get("stow_bay"),
            "stow_row": o.get("stow_row"),
            "stow_tier": o.get("stow_tier"),
            "bay_label": _bay_label(o.get("stow_bay")),
            "stuffing_start": _iso(o["stuffing_start"]),
            "stuffing_end": _iso(o["stuffing_end"]),
            "lane_release_time": _iso(o["lane_release_time"]),
            "loading_lane": o["loading_lane"],
            "staging_lane_start": o.get("staging_lane_start"),
            "staging_lane_end": o.get("staging_lane_end"),
            "road_depart": _iso(o["road_depart"]),
            "eta_loading_area": _iso(o["eta_loading_area"]),
            "hours_to_loading": round(hours, 1),
            "status": status,
            "reasoning": o["reasoning"],
        }
        # Bay plan is served on demand (/api/bayplan) and cached client-side.
        outbound.append(entry)
    outbound.sort(key=lambda x: x["eta_loading_area"])

    # ---- Derived hub-flow views ----------------------------------------------
    # Distribution: land releases (LCL delivery units + FCL land releases) plus
    # the back-to-port vessel-bound consolidation containers (state["outbound"]).
    distribution = _build_distribution(plans, shipments_by_cid, now)
    top_up = _build_top_up(plans, shipments_by_cid, now)
    qc = _build_qc(plans, containers, shipments_by_cid, now)

    return {
        "sim_now": now.isoformat(),
        "seed": SEED,
        "n_containers": N_CONTAINERS,
        "kpis": kpis,
        "yard": yard,
        "drayage": {
            "total_trucks": drayage.get("total_trucks", 0),
            "available_trucks": drayage.get("available_trucks", 0),
            "utilization_pct": drayage.get("utilization_pct", 0.0),
        },
        "vessels": [_serialise_vessel(v) for v in vessels],
        "berths": _berth_state(vessels),
        "inbound": inbound,
        "outbound": outbound,
        "psch": psch,
        "distribution": distribution,
        "top_up": top_up,
        "qc": qc,
        "asrs": _asrs_state(now),
        "exceptions": exceptions,
        "trace": [
            {"ts": _iso(e["ts"]), "actor": e["actor"], "event": e["event"], "detail": e["detail"]}
            for e in trace
        ],
    }


def _transit_hours(destination: str | None) -> int:
    """Road transit from PSCH: ~2h to local Singapore areas, ~5h regional."""
    if destination in LOCAL_DESTINATIONS:
        return 2
    return 5


def _land_status(now, release_time, road_depart, eta_destination) -> str:
    """staged -> released -> in_transit -> delivered for a land release."""
    if eta_destination is not None and now >= eta_destination:
        return "delivered"
    if road_depart is not None and now >= road_depart:
        return "in_transit"
    if release_time is not None and now >= release_time:
        return "released"
    return "staged"


def _build_distribution(plans, shipments_by_cid, now: datetime) -> list[dict]:
    """LCL delivery units + FCL land releases (state["outbound"] = back to port)."""
    lcl_units: dict[str, dict] = {}
    fcl_releases: list[dict] = []
    for p in plans:
        flow = p.get("flow") or "mcc"
        cid = p["container_id"]
        if flow == "lcl" and p.get("destination") and p.get("consolidation_group"):
            gid = p["consolidation_group"]
            unit = lcl_units.setdefault(
                gid,
                {
                    "kind": "LCL delivery",
                    "container_id": gid,
                    "destination": p["destination"],
                    "source_containers": [],
                    "pallets": 0,
                    "build_start": None,
                    "release_time": None,
                    "road_depart": None,
                    "eta_destination": None,
                    "status": "staged",
                },
            )
            unit["source_containers"].append(cid)
            unit["pallets"] += len(shipments_by_cid.get(cid, []))
            build = p.get("pallet_pick_time") or p["psch_receipt_eta"]
            unit["build_start"] = max(
                unit["build_start"] or p["psch_receipt_eta"], build
            )
        elif flow == "fcl":
            release = p["pallet_pick_time"]
            road = release + timedelta(minutes=30)
            eta = road + timedelta(hours=_transit_hours(p.get("destination")))
            fcl_releases.append(
                {
                    "kind": "FCL land release",
                    "container_id": cid,
                    "destination": p.get("destination") or "—",
                    "source_containers": [cid],
                    "pallets": len(shipments_by_cid.get(cid, [])),
                    "build_start": release - timedelta(minutes=30),
                    "release_time": release,
                    "road_depart": road,
                    "eta_destination": eta,
                    "status": _land_status(now, release, road, eta),
                }
            )

    distribution: list[dict] = []
    for gid, unit in lcl_units.items():
        release = unit["build_start"] + timedelta(minutes=90)
        road = release + timedelta(minutes=30)
        eta = road + timedelta(hours=_transit_hours(unit["destination"]))
        unit["release_time"] = release
        unit["road_depart"] = road
        unit["eta_destination"] = eta
        unit["status"] = _land_status(now, release, road, eta)
        distribution.append(unit)
    distribution.extend(fcl_releases)
    distribution.sort(key=lambda d: d["release_time"] or now)
    for d in distribution:
        d["release_time"] = _iso(d["release_time"])
        d["road_depart"] = _iso(d["road_depart"])
        d["eta_destination"] = _iso(d["eta_destination"])
        d["build_start"] = _iso(d["build_start"])
    return distribution


def _build_top_up(plans, shipments_by_cid, now: datetime) -> list[dict]:
    """Re-consolidation (top-up) jobs: pending -> in_progress -> done."""
    jobs: list[dict] = []
    for i, p in enumerate(
        sorted(
            (x for x in plans if (x.get("flow") or "mcc") == "topup"),
            key=lambda x: x["psch_receipt_eta"],
        )
    ):
        cid = p["container_id"]
        window_start = p["psch_receipt_eta"] + timedelta(hours=1)
        window_end = p["pallet_pick_time"]
        seal = window_end + timedelta(minutes=30)
        road = seal + timedelta(minutes=30)
        eta = road + timedelta(hours=_transit_hours(p.get("destination")))
        if now >= window_end:
            status = "done"
        elif now >= window_start:
            status = "in_progress"
        else:
            status = "pending"
        jobs.append(
            {
                "job_id": p.get("consolidation_group") or f"TOPUP-{i + 1:02d}",
                "container_id": cid,
                "destination": p.get("destination") or "—",
                "status": status,
                "pallets_added": len(shipments_by_cid.get(cid, [])),
                "window_start": _iso(window_start),
                "window_end": _iso(window_end),
                "seal_time": _iso(seal),
                "release_eta": _iso(eta),
                "bay": p["bin_location"],
                "reasoning": p["reasoning"],
            }
        )
    return jobs


_QC_STATIONS = {
    "survey": "QC Bay 1",
    "sampling": "QC Bay 2",
    "repack": "QC Bay 3",
    "rework": "QC Bay 4",
}


def _qc_note(kind: str, p: dict) -> str:
    flow = p.get("flow") or "mcc"
    base = {
        "survey": "Pre-receipt survey of the container and cargo condition; damage / shortfall noted before putaway.",
        "sampling": "Cargo sampling for quality and customs verification; samples logged and sent for analysis.",
        "repack": "Repacking of damaged or loose cartons into fresh palletised units before putaway.",
        "rework": "Rework of mislabelled / mis-sorted cargo per the forwarder's instructions.",
    }[kind]
    return f"{base} (flow: {flow})"


def _build_qc(plans, containers, shipments_by_cid, now: datetime) -> list[dict]:
    """Quality-control tasks (survey / sampling / repack / rework) derived from
    the cargo profile: hazmat gets sampled or surveyed, OOG repacked, reefers
    sampled, held customs surveyed, plus a rotation for rework."""
    container_by_id = {c["container_id"]: c for c in containers}
    tasks: list[dict] = []
    for idx, p in enumerate(sorted(plans, key=lambda x: x["psch_receipt_eta"])):
        cid = p["container_id"]
        special = container_by_id.get(cid, {}).get("special_handling") or []
        customs = container_by_id.get(cid, {}).get("customs_status")
        kind = None
        if "hazmat" in special:
            kind = "sampling" if idx % 3 else "survey"
        elif "oversized" in special:
            kind = "repack"
        elif "reefer" in special:
            kind = "sampling"
        elif customs == "held":
            kind = "survey"
        elif idx % 5 == 0:
            kind = "rework"
        elif idx % 3 == 0:
            kind = "sampling"
        if kind is None:
            continue
        start = p["psch_receipt_eta"] + timedelta(hours=1)
        end = start + timedelta(hours=1)
        if now >= end:
            status = "done"
        elif now >= start:
            status = "in_progress"
        else:
            status = "pending"
        tasks.append(
            {
                "task_id": f"QC-{idx + 1:03d}",
                "kind": kind,
                "container_id": cid,
                "flow": p.get("flow") or "mcc",
                "destination": p.get("destination") or p.get("vessel_destination") or "—",
                "status": status,
                "station": _QC_STATIONS[kind],
                "window_start": _iso(start),
                "window_end": _iso(end),
                "cargoes": len(shipments_by_cid.get(cid, [])),
                "note": _qc_note(kind, p),
            }
        )
    return tasks


def _asrs_state(now: datetime) -> dict:
    """AS/RS fleet snapshot: 8 stackers on a staggered 24/7 charge rotation.

    Charge % declines ~8%/hour with use; a stacker at or under the park
    threshold (45%) sits on the charging station, unavailable for putaway,
    and tops back up (~30%/hour) before resuming. The rotation is driven by
    the live sim clock, so the numbers move on every poll (at the default
    60x speed, ~8% per real minute) — the stackers genuinely charge and
    discharge as you watch.
    """
    park_threshold = 45
    drain = 8.0    # %/hour while working
    refill = 30.0  # %/hour while charging
    work_min = (100 - park_threshold) / drain * 60    # ~412 min working
    charge_min = (100 - park_threshold) / refill * 60  # ~110 min charging
    cycle_min = work_min + charge_min                   # ~8.7h full cycle
    stagger = cycle_min / 8
    elapsed_min = (now - SIM_NOW).total_seconds() / 60
    stackers = []
    charging = 0
    for i in range(8):
        phase = (elapsed_min + i * stagger) % cycle_min
        if phase < work_min:
            status = "working"
            charge = round(100 - drain * phase / 60)
            mins_to_park = round(work_min - phase)
            mins_to_ready = None
            ready = now + timedelta(minutes=work_min - phase)
        else:
            status = "charging"
            charge = round(park_threshold + refill * (phase - work_min) / 60)
            mins_to_ready = round(cycle_min - phase)
            mins_to_park = None
            ready = now + timedelta(minutes=cycle_min - phase)
            charging += 1
        stackers.append(
            {
                "id": f"Stacker {i + 1:02d}",
                "charge_pct": charge,
                "status": status,
                "ready": _iso(ready),
                "mins_to_park": mins_to_park,
                "mins_to_ready": mins_to_ready,
            }
        )
    return {
        "stackers": stackers,
        "charging": charging,
        "charging_bays": 2,
        "park_threshold": park_threshold,
        "note": "staggered 24/7 charge rotation — stackers at or under 45% park at the charging station (2 bays) and top back up before resuming",
    }


def _run_planner() -> None:
    mcc_planner.plan(DB_PATH)


PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PSA Control Tower — Port - PSCH Integration</title>
<style>
  body { background:#C0C0C0; color:#000000; margin:6px;
         font-family:"MS Sans Serif","Tahoma",Verdana,sans-serif; font-size:11px; }
  a { color:#0000EE; text-decoration:underline; }
  a:visited { color:#551A8B; }
  h3 { font-size:14px; margin:8px 0 4px 0; }
  h4 { font-size:12px; margin:6px 0 2px 0; }

  .banner { background:#000080; color:#FFFFFF; width:100%;
            border:3px outset #C0C0C0; padding:6px 10px; }
  /* The scrolling news strip lives outside the banner so Full Screen can
     collapse the title bar while the words keep scrolling right-to-left.
     A CSS keyframe animation drives the scroll (the deprecated <marquee>
     element is disabled under prefers-reduced-motion; this always runs). */
  .tickerstrip { background:#000080; color:#FFFF00; font-weight:bold;
                 border:3px outset #C0C0C0; padding:2px 10px; margin-top:3px;
                 overflow:hidden; white-space:nowrap; }
  body.fullscreen .banner { display:none; }
  body.fullscreen .tickerstrip { margin-top:0; }

  .navbar { width:100%; background:#C0C0C0; border-bottom:2px groove #808080; }
  .navbar td { padding:3px 10px; border-right:2px groove #808080; }
  .navlink a { font-weight:bold; }
  .navlink a.active { color:#000000; }

  /* PSCH nav group: one nav item holding the hub sub-pages. Outbound opens a
     second-level menu with Distribution and Top Up (secondary pages, not
     further dropdowns). */
  .navlink.nav-group { position:relative; }
  .navlink.nav-group > a .arrow { font-size:9px; }
  .nav-sub { display:none; position:absolute; top:100%; left:0; z-index:60;
             background:#C0C0C0; border:2px solid #000080; min-width:160px; }
  .navlink.nav-group:hover .nav-sub,
  .navlink.nav-group:focus-within .nav-sub { display:block; }
  .nav-sub a { display:block; padding:3px 16px; font-weight:bold; white-space:nowrap; }
  .nav-sub a:hover { background:#000080; color:#FFFFFF; }
  .nav-sub a.active { background:#000080; color:#FFFFFF; }
  .nav-sub .nav-group2 { position:relative; }
  .nav-sub .nav-group2 > a { padding-right:6px; }
  .nav-sub .nav-group2 .arrow2 { font-size:9px; margin-left:10px; }
  .nav-sub2 { display:none; position:absolute; left:100%; top:-2px; z-index:61;
              background:#C0C0C0; border:2px solid #000080; min-width:150px; }
  .nav-group2:hover .nav-sub2, .nav-group2:focus-within .nav-sub2 { display:block; }
  .nav-sub2 a { display:block; padding:3px 16px; font-weight:bold; white-space:nowrap; }
  .nav-sub2 a:hover { background:#000080; color:#FFFFFF; }
  .nav-sub2 a.active { background:#000080; color:#FFFFFF; }

  /* Flow / kind filter chips above each hub page's list */
  .flow-chips { margin:4px 0 2px 0; }
  .flow-chips .chip { border:2px outset #FFFFFF; background:#C0C0C0;
                      font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:10px;
                      padding:1px 10px; margin-right:4px; cursor:pointer; }
  .flow-chips .chip:active { border-style:inset; }
  .flow-chips .chip.active { background:#000080; color:#FFFFFF; }

  /* Distribution / Top Up / QC board blocks */
  .board-box { border:1px solid #404040; background:#FFFFFF; margin:4px 0; padding:4px; font-size:10px; }
  .board-box .bb-head { background:#000080; color:#FFFFFF; font-weight:bold; padding:2px 4px;
                        font-size:9px; margin:-4px -4px 4px -4px; }
  .board-box.land .bb-head { background:#1F4E79; }
  .board-box.port .bb-head { background:#6A3FA0; }
  .unit-chip { display:inline-block; border:1px solid #606060; background:#EAF2FB; margin:2px;
               padding:1px 6px; font-size:9px; cursor:pointer; white-space:nowrap; }
  .unit-chip.sel { outline:2px solid #FF0000; background:#FFE9E9; }
  .unit-chip.done { background:#E3F5E3; }
  .unit-chip.transit { background:#FDF3D9; }
  .unit-chip .u-status { color:#404040; font-weight:bold; }
  .flow-pill { font-size:8px; font-weight:bold; padding:0 3px; border:1px solid #808080; }
  .flow-pill.mcc { background:#CFE3F2; }
  .flow-pill.lcl { background:#E6F2D9; }
  .flow-pill.fcl { background:#FDEBD9; }
  .flow-pill.dist { background:#FFF2CC; }
  .flow-pill.topup { background:#F2DCE5; }
  .flow-pill.transload { background:#EAE0F5; }
  .flow-pill.yard { background:#E0E0E0; }
  .qc-kind { font-size:8px; font-weight:bold; padding:0 3px; border:1px solid #808080; }
  .qc-kind.survey { background:#CFE3F2; }
  .qc-kind.sampling { background:#E6F2D9; }
  .qc-kind.repack { background:#FDEBD9; }
  .qc-kind.rework { background:#F2DCE5; }
  .wf-strip { display:flex; flex-wrap:wrap; gap:2px; margin-bottom:6px; }
  .wf-step { border:2px solid #808080; background:#E4E4E4; padding:2px 8px; font-size:9px;
             font-weight:bold; color:#404040; }
  .wf-step.now { background:#000080; color:#FFFFFF; border-color:#000080; }

  table.grid { border-collapse:separate; border:2px outset #FFFFFF; background:#FFFFFF; width:100%; }

  /* Clickable berth rectangles overlaid on the static map */
  .berth { position:absolute; border:2px solid; cursor:pointer; box-sizing:border-box; }
  .berth.free { border-color:#008000; background:rgba(0,128,0,0.14); }
  .berth.occ  { border-color:#000080; background:rgba(0,0,128,0.22); }
  .berth.inb  { border-color:#B07000; border-style:dashed; background:rgba(176,112,0,0.15); }
  .berth.sel  { outline:3px solid #FF0000; }
  .berth.hl   { outline:3px solid #FF00FF; }
  .berth font { position:absolute; left:1px; top:0; font-size:8px; font-weight:bold; color:#000000; background:#FFFFFF; padding:0 2px; }
  table.grid th { background:#000080; color:#FFFFFF; font-weight:bold; text-align:left;
                  padding:3px 6px; border:1px solid #000080; }
  table.grid td { border:1px solid #808080; padding:2px 6px; background:#FFFFFF; }
  table.grid tr.alt td { background:#EFEFEF; }

  .btn { background:#C0C0C0; color:#000000; border:2px outset #FFFFFF;
         font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:11px; padding:1px 12px; }
  .btn:active { border-style:inset; }
  .btn:disabled { color:#808080; }
  /* The toolbar buttons sit as one compact group (a small gap between them)
     instead of being stretched across the full-width row. */
  .toolbar .btn + .btn { margin-left:4px; }
  /* The toolbar goal bar matches the theme's 11px UI font. */
  .toolbar-goal { font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:11px;
                  padding:1px 4px; background:#FFFFFF; border:2px inset #808080; }

  fieldset { border:2px outset #FFFFFF; background:#C0C0C0; padding:6px 8px; min-width:0; }
  legend { font-weight:bold; background:#C0C0C0; border:1px solid #808080; padding:0 4px; }

  .statusbar { width:100%; margin-top:8px; border-top:2px solid #808080;
               border-bottom:2px solid #FFFFFF; background:#C0C0C0; }
  .statusbar td { padding:2px 8px; }
  .metric { font-weight:bold; color:#000080; }
  .footer { margin-top:8px; text-align:center; color:#404040; }

  .mp-item { display:block; width:100%; text-align:left; border:1px solid #808080;
             background:#FFFFFF; padding:2px 4px; margin-bottom:2px; cursor:pointer;
             font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:11px; }
  .mp-item:hover { background:#FFFFCC; }
  .mp-item.sel { background:#FFFF99; }
  .mp-item .cid { font-weight:bold; }

  /* Searchable incoming-container combobox */
  .incoming-search-wrap { position:relative; }
  .searchbox { width:100%; box-sizing:border-box; font-family:"MS Sans Serif","Tahoma",sans-serif;
               font-size:11px; padding:2px 4px; border:2px inset #FFFFFF; background:#FFFFFF; }
  .search-drop { position:absolute; top:100%; left:0; width:100%; box-sizing:border-box;
                 z-index:80; display:none; max-height:420px; overflow:auto; background:#FFFFFF;
                 border:2px solid #000080; }
  .search-drop .mp-item { margin-bottom:0; border-left:none; border-right:none; }
  .search-drop .mp-item:first-child { border-top:1px solid #808080; }
  .search-drop .no-match { padding:4px; font-size:10px; color:#404040; }
  .search-drop .drop-foot { padding:2px 4px; background:#E8E8E8; color:#404040; font-size:9px; }
  .incoming-selinfo { margin-top:2px; font-size:10px; color:#404040; word-wrap:break-word; }
  /* The berth-info slots only take space when they actually show a berth /
     vessel; collapse them when empty so nothing leaves a stray gap. */
  #berthInfo:empty, #mccBerthInfo:empty { display:none; }
  .psch-inc-sub { font-size:9px; color:#667085; margin:0 0 4px 0; }

  /* Reflow: when a container detail is open the left column collapses so the
     map slides left and the ship-tracker inspector widens (no window resize). */
  table.cols { table-layout:fixed; }
  td.incoming-col { width:30%; }
  td.map-col     { width:42%; }
  td.inspector-col { width:28%; }
  td.incoming-col, td.map-col, td.inspector-col { transition:width 0.25s ease; }
  #view-in.detail-open td.incoming-col, #view-store.detail-open td.incoming-col,
  #view-mcc.detail-open td.incoming-col, #view-dist.detail-open td.incoming-col,
  #view-top.detail-open td.incoming-col, #view-qc.detail-open td.incoming-col { width:13%; }
  #view-in.detail-open td.map-col, #view-store.detail-open td.map-col,
  #view-mcc.detail-open td.map-col, #view-dist.detail-open td.map-col,
  #view-top.detail-open td.map-col, #view-qc.detail-open td.map-col { width:44%; }
  #view-in.detail-open td.inspector-col, #view-store.detail-open td.inspector-col,
  #view-mcc.detail-open td.inspector-col, #view-dist.detail-open td.inspector-col,
  #view-top.detail-open td.inspector-col, #view-qc.detail-open td.inspector-col { width:43%; }

  .countdown { font-weight:bold; }
  .countdown.crit { color:#CC0000; }
  .countdown.warn { color:#CC6600; }

  .map-kpi { position:absolute; background:#C0C0C0; border:1px solid #404040;
             padding:1px 4px; font-size:10px; }

  .st-badge { font-weight:bold; padding:0 4px; border:1px solid #808080; }
  .st-sea   { color:#0000CC; background:#DDE6FF; }
  .st-unload{ color:#B07000; background:#FFF0D9; }
  .st-depot { color:#805000; background:#FFE8C2; }
  .st-road  { color:#007050; background:#D9FFF0; }
  .st-arr   { color:#008000; background:#DDFFDD; }
  .st-loaded{ color:#008000; background:#DDFFDD; }
  .st-staged{ color:#0000CC; background:#DDE6FF; }

  .bayplan table.bp-table { border-collapse:collapse; border:1px solid #404040; }
  .bayplan .bp-table caption { font-weight:bold; text-align:left; padding:2px; }
  .bayplan .bp-table td { border:1px solid #808080; }
  .bayplan td.bp-tier { background:#C0C0C0; font-size:7px; font-weight:bold; text-align:center;
                        padding:0 2px; width:14px; }
  .bayplan td.bp-tier-r { border-left:none; }
  .bayplan td.bp-stack { background:#D0D0D0; font-size:7px; text-align:center; padding:1px 0; }
  .bayplan td.bp-cell { min-width:52px; height:26px; padding:0 2px; vertical-align:top;
                        text-align:center; color:#000000; font-family:"MS Sans Serif",Tahoma,sans-serif; }
  .bayplan td.bp-empty { background:#FFFFFF; }
  .bayplan td.bp-hatch { background:#A9A9A9; color:#4A4A4A; font-size:8px; font-weight:bold;
                         vertical-align:middle; text-align:center; }
  .bayplan td.bp-target { outline:2px dashed #FF00FF; outline-offset:-2px; }
  .bayplan .bp-target-tag { font-size:7px; font-weight:bold; color:#FF00FF; }
  .bayplan td.bp-sel { outline:3px solid #FF0000; }
  .bayplan .bp-size { font-size:8px; font-weight:bold; }
  .bayplan .bp-id { font-size:7px; font-weight:bold; }
  .bayplan .bp-w { font-size:7px; color:#222222; }
  .bayplan td.bp-deck { background:#F5DEB3; font-size:8px; text-align:center; color:#5C4033;
                        border-left:none; border-right:none; }
  .bayplan td.bp-void { border:none; background:#EFEFEF; }
  .bayplan td.bp-wall-l { border-left:2px solid #3F3F3F; }
  .bayplan td.bp-wall-r { border-right:2px solid #3F3F3F; }
  .bayplan .bp-legend-wrap { margin-top:3px; }
  .bayplan .bp-legend { margin-right:6px; font-size:8px; }
  .bayplan .bp-legend i { display:inline-block; width:8px; height:8px; border:1px solid #000;
                          margin-right:2px; vertical-align:middle; }

  .journey { width:100%; border-collapse:collapse; }
  .journey td { padding:2px 4px; border-bottom:1px solid #C0C0C0; }
  .journey .stage-done { color:#808080; }
  .journey .stage-now { background:#FFFFCC; font-weight:bold; }
  .journey .stage-wait { color:#C0C0C0; }
  /* Compact overview-metric strip: metrics in one wrapping row that hugs its
     content, instead of a full-width table of stacked rows eating the page. */
  .kpi-strip { display:flex; flex-wrap:wrap; border:2px outset #FFFFFF; background:#FFFFFF;
               margin:0 0 4px 0; }
  .kpi-strip .kpi-cell { border-right:1px solid #808080; padding:2px 8px; white-space:nowrap; }
  .kpi-strip .kpi-cell .k { color:#404040; }

  /* ---- Bloomberg-style change flashes ---------------------------------
     Any value that changed between polls flashes so the viewer sees that
     *something just happened*: green ▲ for an increase, red ▼ for a
     decrease, amber for a status change / new element. The flash fades
     out over ~2.5s; the next poll re-renders and re-evaluates. */
  /* Bloomberg-style value flashes: the field that just changed flashes the
     exact millisecond the new value is rendered, colour-coded by direction
     (green ▲ = higher, red ▼ = lower, amber = neutral/status change). Like
     the terminal's PDF <GO>, everything is customisable from the Flash
     settings panel — palette, speed, brightness, and an on/off switch — via
     CSS variables driven by applyFlashSettings(). */
  :root {
    --flash-dur:2.5s; --flash-glow:8px;
    --flash-up-bg:#C8F7C5; --flash-up-glow:#2E8B57; --flash-up-text:#2E8B57;
    --flash-down-bg:#FDE2E2; --flash-down-glow:#C0504D; --flash-down-text:#C0504D;
    --flash-chg-bg:#FFF3B8; --flash-chg-glow:#DAA520;
  }
  /* The fade ends at --flash-end (each element's own resting background,
     captured by flashEl at flash time) so the colour returns seamlessly —
     never flashing through transparent/grey before snapping back. */
  @keyframes flashUp { 0% { background-color:var(--flash-up-bg); box-shadow:0 0 var(--flash-glow) var(--flash-up-glow); } 100% { background-color:var(--flash-end, transparent); box-shadow:none; } }
  @keyframes flashDown { 0% { background-color:var(--flash-down-bg); box-shadow:0 0 var(--flash-glow) var(--flash-down-glow); } 100% { background-color:var(--flash-end, transparent); box-shadow:none; } }
  @keyframes flashChange { 0% { background-color:var(--flash-chg-bg); box-shadow:0 0 var(--flash-glow) var(--flash-chg-glow); } 100% { background-color:var(--flash-end, transparent); box-shadow:none; } }
  .flash-up { animation: flashUp var(--flash-dur) ease-out 1; }
  .flash-down { animation: flashDown var(--flash-dur) ease-out 1; }
  .flash-change { animation: flashChange var(--flash-dur) ease-out 1; }
  .flashes-off .flash-up, .flashes-off .flash-down, .flashes-off .flash-change { animation:none; }
  .delta { font-weight:bold; font-size:10px; margin-left:3px; }
  .delta-up { color:var(--flash-up-text); }
  .delta-down { color:var(--flash-down-text); }

  /* Flash display settings (the PDF <GO> equivalent). */
  .flash-settings { position:absolute; left:0; top:100%; z-index:50; background:#C0C0C0;
    border:2px outset #FFFFFF; padding:8px; font-size:10px; width:230px; display:none;
    box-shadow:2px 2px 6px rgba(0,0,0,0.4); text-align:left; }
  .flash-settings h4 { margin:0 0 6px 0; font-size:11px; border-bottom:1px solid #808080; padding-bottom:3px; }
  .flash-settings label { display:block; margin:3px 0; }
  .flash-settings select { font-size:10px; margin-left:4px; }
  .kpi-strip .kpi-cell { transition: box-shadow 0.2s; }

  /* ============ PSCH Plan — schematic floorplan ============ */
  .psch-plan { width:100%; min-width:560px; }
  .psch-plan .fp-site { background:#000080; color:#FFFFFF; font-size:10px; font-weight:bold;
                        padding:4px 8px; border:2px outset #C0C0C0;
                        display:flex; flex-wrap:wrap; gap:4px 12px; align-items:center; }
  .psch-plan .fp-site .fp-road { color:#FFE87C; font-weight:normal; }
  .psch-plan .fp-site .fp-gate { border:1px solid #FFFFFF; padding:0 5px; font-size:9px; font-weight:normal; }
  .psch-plan .fp-yard { background:#D9D9C0; color:#3f3f00; font-size:9px; text-align:center;
                        padding:4px; border:1px solid #808080; margin-top:2px; }
  .psch-plan .fp-building { display:grid; grid-template-columns:1.1fr 2.2fr 0.9fr 1.2fr;
                            gap:4px; margin-top:4px; }
  .psch-plan .fp-zone { border:2px solid #404040; padding:6px; min-height:120px; }
  .psch-plan .fp-zone-title { font-weight:bold; font-size:10px; text-align:center;
                              border-bottom:2px solid #808080; margin-bottom:5px; padding-bottom:2px; }
  .psch-plan .fp-sub { font-size:9px; font-weight:bold; color:#404040; margin-top:5px; }
  .psch-plan .fp-lanes { font-size:11px; font-weight:bold; }
  .psch-plan .fp-haz { font-size:8px; color:#B07000; font-weight:bold; }
  .psch-plan .fp-in { background:#CFE3F2; }
  .psch-plan .fp-store { background:#E4E4E4; }
  .psch-plan .fp-cold { background:#D6E7F7; border-color:#1F4E79; }
  .psch-plan .fp-cold .fp-zone-title { color:#1F4E79; }
  .psch-plan .fp-out { background:#CFE3F2; }
  .psch-plan .fp-support { background:#C0C0C0; border:2px inset #FFFFFF; font-size:9px; text-align:center;
                           padding:3px; margin-top:4px; color:#404040; }
  .psch-plan .fp-flow { border-top:2px solid #808080; background:#C0C0C0; font-size:9px; padding:2px 6px;
                        text-align:center; color:#404040; margin-top:4px; }
  .psch-plan .fp-asrs { background:#DCEAEA; color:#1E4B4B; font-size:9px; text-align:center;
                        padding:4px; border:1px solid #808080; margin-top:4px; }
  .psch-plan .asrs-chip { display:inline-block; border:1px solid #808080; padding:0 4px;
                          margin:2px 2px 0 2px; font-size:8px; cursor:default; }
  .psch-plan .asrs-chip.working { background:#DFF2DF; }
  .psch-plan .asrs-chip.charging { background:#FDEBD9; }

  /* Numbered process strip — the one-line "how cargo moves" onboarding aid */
  .proc-strip { display:flex; flex-wrap:wrap; gap:4px; align-items:stretch; font-size:9px; }
  .proc-step { border:1px solid #404040; padding:3px 6px; background:#E4E4E4; flex:1 1 auto;
               min-width:92px; text-align:center; }
  .proc-step .step-n { display:inline-block; background:#000080; color:#FFFFFF; border-radius:8px;
                       width:14px; height:14px; line-height:14px; font-size:9px; margin-right:3px; }
  .proc-step .proc-name { font-weight:bold; font-size:10px; }
  .proc-step .proc-sub { color:#404040; font-size:8px; margin-top:1px; }
  /* All seven flow steps share one colour (INBOUND -> RECEIVING -> PUTAWAY ->
     STORAGE -> PICK -> RELEASING -> OUTBOUND) so the strip reads as a single
     pipeline — the same navy as the facility headers — with the step number
     chip inverted for contrast. */
  .proc-step.s-in, .proc-step.s-rcv, .proc-step.s-pa, .proc-step.s-store,
  .proc-step.s-pick, .proc-step.s-rel, .proc-step.s-out { background:#000080; color:#FFFFFF; }
  .proc-step.s-in .proc-sub, .proc-step.s-rcv .proc-sub, .proc-step.s-pa .proc-sub,
  .proc-step.s-store .proc-sub, .proc-step.s-pick .proc-sub, .proc-step.s-rel .proc-sub,
  .proc-step.s-out .proc-sub { color:#D0E0FF; }
  .proc-step.s-in .step-n, .proc-step.s-rcv .step-n, .proc-step.s-pa .step-n,
  .proc-step.s-store .step-n, .proc-step.s-pick .step-n, .proc-step.s-rel .step-n,
  .proc-step.s-out .step-n { background:#FFFFFF; color:#000080; }
  .proc-arrow { align-self:center; font-weight:bold; color:#404040; }

  /* Receiving lanes: horizontal blocks, one container number per line, never
     wrapped; the row scrolls sideways when it is wider than the pane. The
     blocks are only as wide as the 8px numbers need, and the text is
     centre-aligned to match the releasing lanes. Clicking a lane opens the
     receiving & putaway plan of the container being unloaded there. */
  .psch-rel-lane.rcv { background:#CFE3F2; border-color:#2F6FB2; width:78px;
                       height:auto; min-height:112px; cursor:default; }
  .psch-rel-lane.rcv .psch-rel-group { white-space:nowrap; word-break:normal;
                                       word-wrap:normal; font-size:8px; text-align:center; }
  .psch-rel-lane.rcv.empty { background:#EDEDED; }

  /* Room footer line (plain-language note about what the room is for) */
  .psch-room-note { font-size:8px; color:#404040; padding:2px 6px; background:#F5F5F5;
                    border-top:1px solid #C0C0C0; }

  /* Collapsed FYI container for the stacker charging schedule */
  .asrs-fyi { border:1px solid #808080; background:#F2F2F2; padding:3px 6px;
              font-size:9px; color:#404040; }
  .asrs-fyi summary { cursor:pointer; font-weight:bold; color:#1E4B4B; }
  .asrs-note { margin:4px 0 2px 0; }
  .asrs-table { border-collapse:collapse; width:100%; margin-top:2px; }
  .asrs-table th { background:#DCEAEA; color:#1E4B4B; border:1px solid #808080;
                   padding:2px 6px; font-size:9px; text-align:left; }
  .asrs-table td { border:1px solid #808080; padding:2px 6px; font-size:9px; background:#FFFFFF; }
  .asrs-table tr.asrs-row-working td { background:#DFF2DF; }
  .asrs-table tr.asrs-row-charging td { background:#FDEBD9; }
  .asrs-now { margin-top:4px; font-weight:bold; }

  /* ---- PSA Intelligence (Gemini-style positioning, Y2K theme) ---- */
  #view-intel { background:#C0C0C0; }
  .intel-shell { border:2px outset #FFFFFF; background:#C0C0C0; }
  /* Left icon rail, like Gemini's collapsed sidebar */
  .intel-rail { width:56px; background:#C0C0C0; border-right:2px groove #808080;
                vertical-align:top; }
  .intel-rail-inner { display:flex; flex-direction:column; align-items:center;
                      padding:8px 4px; gap:8px; }
  .intel-rail-icon { width:30px; height:30px; border:2px outset #FFFFFF; background:#C0C0C0;
                     display:flex; align-items:center; justify-content:center;
                     font-size:9px; color:#000000; cursor:pointer; font-weight:bold; }
  .intel-rail-icon:active { border-style:inset; }
  .intel-rail-icon:hover { background:#FFFFCC; }
  .intel-rail-icon.active { background:#000080; color:#FFFFFF; border-style:inset; }
  .intel-rail-spacer { flex:1; min-height:12px; }
  /* Top bar: title left, action buttons right (like Gemini's header) */
  .intel-topbar { width:100%; border-bottom:2px groove #808080; padding:4px 10px;
                  background:#C0C0C0; }
  .intel-topbar td { vertical-align:middle; }
  .intel-title { font-size:16px; font-weight:bold; color:#000080; }
  .intel-tagline { color:#404040; font-size:10px; }
  .intel-topbtns .btn { margin-left:4px; }
  .intel-brain { font-size:10px; color:#404040; }
  .intel-brain b { color:#000080; }
  /* Centered hero: heading + prompt + quick links */
  .intel-main { vertical-align:top; background:#C0C0C0; }
  .intel-hero { text-align:center; padding:44px 20px 26px 20px; }
  .intel-hero h2 { font-size:24px; color:#000080; margin:0 0 6px 0; }
  .intel-hero .sub { color:#404040; font-size:11px; margin:0 0 16px 0; max-width:620px;
                     margin-left:auto; margin-right:auto; }
  .intel-promptwrap { margin:8px auto 0 auto; width:100%; max-width:820px;
                      padding:0 6px; box-sizing:border-box; }
  .intel-prompt { margin:0 auto; width:100%; border:2px inset #808080;
                  background:#FFFFFF; padding:4px; }
  .intel-prompt td { vertical-align:middle; }
  .intel-input { font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:12px;
                 padding:3px 5px; border:1px solid #808080; background:#FFFFFF;
                 width:100%; box-sizing:border-box; }
  .intel-send { border:2px outset #FFFFFF; background:#C0C0C0;
                font-family:"MS Sans Serif","Tahoma",sans-serif; font-size:11px;
                padding:3px 16px; cursor:pointer; font-weight:bold; }
  .intel-send:active { border-style:inset; }
  .intel-send:disabled { color:#808080; }
  .intel-quick { margin-top:10px; font-size:10px; color:#404040; }
  .intel-quick a { color:#0000EE; text-decoration:underline; margin:0 6px;
                   white-space:nowrap; }
  .intel-quick a:hover { background:#FFFFCC; }
  /* Conversation (appears once the first question is asked) */
  .intel-thread { display:none; margin:0 auto; width:100%; max-width:820px;
                  border:2px inset #808080; background:#FFFFFF; padding:6px;
                  min-height:200px; max-height:460px; overflow:auto; box-sizing:border-box; }
  .intel-thread .hint { color:#404040; font-size:10px; }
  .intel-msg { margin-bottom:8px; }
  .intel-msg .who { font-weight:bold; color:#000080; font-size:10px; }
  .intel-msg.user .who { color:#008000; }
  .intel-msg .bubble { border:2px outset #FFFFFF; background:#EFEFEF; padding:4px 6px;
                       margin-top:1px; white-space:pre-wrap; word-wrap:break-word;
                       font-size:11px; line-height:1.4; }
  .intel-msg.assistant .bubble { background:#FFFFFF; }
  .intel-msg.thinking .bubble { color:#404040; font-style:italic; background:#FFFFCC; }
  .intel-msg .meta { font-size:9px; color:#404040; margin-top:2px; }
  .intel-h { margin-top:5px; color:#000080; font-size:11px; }
  .intel-msg .bubble code { background:#EFEFEF; border:1px solid #C0C0C0; padding:0 2px;
                            font-family:Consolas,monospace; font-size:10px; }
  .intel-msg .bubble pre.intel-code { background:#EFEFEF; border:1px solid #808080; padding:4px;
                            margin:4px 0; white-space:pre-wrap; font-family:Consolas,monospace;
                            font-size:10px; }
  .intel-approve-row { margin-top:4px; }
  /* One pending plan change = one labelled block with its own Approve/Reject
     pair, so multi-action replies read as N clearly-separated proposals. */
  .intel-proposal { display:block; margin:6px 0; border:1px solid #B0B0B0;
                    background:#F5F5F5; padding:4px 6px; }
  .intel-proposal-head { font-size:9px; color:#000080; font-weight:bold; }
  .intel-proposal-label { margin:2px 0; }
  .intel-proposal-actions { margin-top:3px; }
  .intel-approve-row .btn { margin-right:4px; }

  /* Agent reasoning: structured labelled rows instead of a wall of text.
     Each line is either "Label: value" (rendered as a two-cell row) or a
     plain sentence; [agent] lines are plan-change notes from the tools. */
  .reasoning { width:100%; border-collapse:collapse; font-size:10px; }
  .reasoning td { padding:1px 4px 1px 0; vertical-align:top; }
  .reasoning .rk { color:#000080; font-weight:bold; white-space:nowrap; padding-right:6px; }
  .reasoning .rv { color:#202020; }
  .reasoning tr.rsn-agent td { color:#8B5A00; font-weight:bold; padding-top:3px; }
  .reasoning tr.rsn-warn td { color:#B00020; font-weight:bold; padding-top:3px; }
  .reasoning tr.rsn-plain td { padding-top:3px; }

  /* Execution trace: actor categories, filters, approval lifecycle badges */
  .trace-live { font-size:10px; font-weight:bold; color:#006400; vertical-align:middle; }
  .trace-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:#00A000;
               animation:traceBlink 1.6s ease-in-out infinite; }
  @keyframes traceBlink {
    0%,100% { opacity:1; }
    50% { opacity:.15; }
  }
  .trace-live.flash { color:#000080; }
  .trace-live.flash .trace-dot { background:#FFFFFF; box-shadow:0 0 6px 3px rgba(0,160,0,0.8); }
  .trace-filters .btn { margin-right:4px; }
  .trace-filters .btn.active { background:#000080; color:#FFFFFF; border-style:inset; }
  tr.trace-cat td { background:#D0D0D0; color:#000080; font-weight:bold; border:1px solid #808080; }

  /* Attention needed (exception watch): category groups + AI action buttons */
  tr.exc-group td { background:#D0D0D0; color:#000080; font-weight:bold;
                    border:1px solid #808080; }
  tr.exc-cat-in td { background:#E7EFFF; }
  tr.exc-cat-customs td { background:#F5F0E0; }
  tr.exc-cat-out td { background:#E8F0E8; }
  tr.exc-cat-vessel td { background:#EFEFFF; }
  tr.exc-cat-other td { background:#EFEFF0; }
  .exc-act { margin-top:4px; }
  .exc-act .btn { margin-right:4px; font-size:10px; }
  .exc-noaction { color:#404040; font-size:10px; }
  .exc-noaction a { color:#0000EE; text-decoration:underline; }
  .exc-done { color:#006400; font-size:10px; }
  .actor-badge { font-weight:bold; font-size:9px; padding:0 4px; border:1px solid #808080; }
  .actor-agent { background:#E7EFFF; color:#000080; }
  .actor-runtime { background:#E8F0E8; color:#006400; }
  .actor-operator { background:#F5F0E0; color:#8B5A00; }
  .actor-system { background:#EFEFF0; color:#404040; }
  .tr-badge { font-weight:bold; font-size:9px; padding:0 4px; border:1px solid #000000; margin-right:4px; }
  .tr-badge.ok { color:#006400; background:#DFF0D8; }
  .tr-badge.bad { color:#8B0000; background:#F2DEDE; }
  .tr-badge.pend { color:#8B5A00; background:#FCF8E3; }
  .intel-foot { color:#404040; font-size:10px; margin:6px auto 8px auto;
                max-width:820px; }
  .intel-foot b { color:#000080; }

  {{PSCH_CSS}}
</style>
</head>
<body>

<table class="banner"><tr><td>
  <font size="5" color="#FFFFFF"><b>PSA Control Tower</b></font>
  <font size="2" color="#FFFFFF">&nbsp;·&nbsp;Port - PSCH Integration</font>
</td></tr></table>
<div class="tickerstrip" id="tickerStrip"><span id="ticker" style="position:relative;white-space:nowrap">*** LOADING ***</span></div>

<table class="navbar"><tr>
  <td class="navlink nav-group">
    <a id="nav-psch" href="#" onclick="showView('in');return false;">PSCH <span class="arrow">▾</span></a>
    <div class="nav-sub">
      <a id="nav-in" href="#" onclick="showView('in');return false;">Inbound</a>
      <a id="nav-store" href="#" onclick="showView('store');return false;">Storage</a>
      <div class="nav-group2">
        <a id="nav-out2" href="#" onclick="showView('dist');return false;">Outbound <span class="arrow2">▸</span></a>
        <div class="nav-sub2">
          <a id="nav-mcc" href="#" onclick="showView('mcc');return false;">MCC</a>
          <a id="nav-dist" href="#" onclick="showView('dist');return false;">Distribution</a>
          <a id="nav-top" href="#" onclick="showView('top');return false;">Top Up</a>
        </div>
      </div>
      <a id="nav-qc" href="#" onclick="showView('qc');return false;">Quality Control</a>
    </div>
  </td>
  <td class="navlink"><a id="nav-tower" href="#" onclick="showView('tower');return false;">Control Tower</a></td>
  <td class="navlink"><a id="nav-trace" href="#" onclick="showView('trace');return false;">Execution Trace</a></td>
  <td class="navlink"><a id="nav-intel" href="#" onclick="showView('intel');return false;">PSA Intelligence</a></td>
  <td align="right"><b>SIM CLOCK:</b> <span id="simClock">—</span></td>
</tr></table>

<table width="100%" cellpadding="3"><tr>
  <td nowrap class="toolbar" style="position:relative">
    <input type="button" class="btn" value="&#8635; Regenerate" onclick="regenerate()">
    <input type="button" class="btn" value="&#9654; Run Hub Planner" onclick="runPlanner()">
    <input type="button" class="btn" id="fsBtn" value="&#x26F6; Full Screen" onclick="toggleFullScreen()">
    <input type="button" class="btn" id="flashBtn" value="&#9881; Display" onclick="toggleFlashSettings()">
    <div class="flash-settings" id="flashSettings">
      <h4>Flash display &mdash; live value alerts</h4>
      <label><input type="checkbox" id="flashEnabled" onchange="setFlashSetting('enabled', this.checked)"> Live value flashes</label>
      <label>Speed
        <select id="flashSpeed" onchange="setFlashSetting('speed', this.value)">
          <option value="slow">Slow (4s fade)</option>
          <option value="normal" selected>Normal (2.5s)</option>
          <option value="fast">Fast (1.1s)</option>
        </select></label>
      <label>Brightness
        <select id="flashBright" onchange="setFlashSetting('brightness', this.value)">
          <option value="subtle">Subtle</option>
          <option value="normal" selected>Normal</option>
          <option value="vivid">Vivid</option>
        </select></label>
      <label>Palette
        <select id="flashPalette" onchange="setFlashSetting('palette', this.value)">
          <option value="classic" selected>Classic (green/red/amber)</option>
          <option value="cb">Colour-blind safe (blue/orange/yellow)</option>
          <option value="mono">Mono (blue/grey)</option>
        </select></label>
    </div>
  </td>
  <td align="right" nowrap>
    <input type="text" id="goalInput" class="toolbar-goal" size="34"
           placeholder="Send a goal to the agent…" autocomplete="off"
           onkeydown="if(event.key==='Enter'){toolbarAsk();}">
    <input type="button" class="btn" id="goalRunBtn" value="&#9654; Run" onclick="toolbarAsk()">
  </td>
</tr></table>

<!-- Agent-goal result line: appears after a Run Agent Goal click. -->
<div id="agentGoalResult" style="display:none;margin-top:3px"></div>

<!-- ================= PSCH INBOUND ================= -->
<div id="view-in">
  <h3>All Inbound Containers bound for PSCH</h3>
  <div class="kpi-strip" id="kpiStrip"></div>
  <div class="flow-chips" id="flowChips"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Inbound Containers (all flows)</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="incomingSearch" class="searchbox"
                 placeholder="Search container / vessel / status / destination…" autocomplete="off"
                 onfocus="openIncomingDrop()" onclick="openIncomingDrop()"
                 oninput="filterIncoming()" onkeydown="incomingKey(event)"
                 onblur="closeIncomingDropSoon()">
          <div id="incomingDrop" class="search-drop"></div>
        </div>
        <div id="berthInfo" style="margin-top:6px;min-height:36px"></div>
        <div id="incomingSelInfo" class="incoming-selinfo"></div>
      </fieldset>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Viewer — Click on a berth to inspect it</legend>
        <div id="facilityMap" style="position:relative;width:100%;max-width:560px;aspect-ratio:1;min-height:380px;border:1px solid #000000;overflow:hidden;background:#D9D9C0"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Vessel &amp; Container Details</legend>
        <div id="inspector" style="min-height:420px">Select an inbound container on the left to open its vessel and container details.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= PSCH STORAGE ================= -->
<div id="view-store" style="display:none">
  <h3>PSCH Storage — receiving, AS/RS stacker putaway &amp; picking (24/7 · agent-orchestrated)</h3>
  <div class="kpi-strip" id="pschKpis"></div>
  <table width="100%" cellspacing="4" style="table-layout:fixed"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Inbound Cargo by Container ID</legend>
        <div id="pschSelInfo" class="incoming-selinfo">Select a container to open its receiving &amp; putaway plan.</div>
        <div class="psch-inc-sub">Includes Bin Location and Stacker responsible</div>
        <div class="incoming-search-wrap">
          <input type="text" id="pschSearch" class="searchbox"
                 placeholder="Search container / vessel / status / destination…" autocomplete="off">
          <div id="pschDrop" class="search-drop"></div>
        </div>
        <div id="pschIncoming" style="max-height:430px;overflow:auto"></div>
      </fieldset>
    </td>
    <td width="42%" valign="top" class="map-col">
      <div id="pschFlow" class="proc-strip"></div>
      <fieldset style="margin-top:4px"><legend>Receiving Lanes — trucks unload here</legend>
        <div id="pschRcvLanes"></div>
      </fieldset>
      <div id="pschOutboundSec" style="margin-top:4px"></div>
      <div style="margin-top:4px"><fieldset><legend>Aisle Details — bin grid</legend>
        <div id="pschRackDetail" style="min-height:150px;overflow-x:auto">Select an aisle in the facility view to inspect its bins.</div>
      </fieldset></div>
      <details class="asrs-fyi" style="margin-top:4px">
        <summary>&#9889; AS/RS — Stacker Charging (FYI)</summary>
        <div id="asrsDetail"></div>
      </details>
      <div style="font-size:8px;color:#404040;margin-top:3px">Bins are named <b>AISLE-LEVEL-BAY</b> (e.g. 1-12-2A = Aisle 1, Level 12, Bay 2A); aisles 1-24 (ambient 1-21, cold room 22-24; aisle 21 DG-segregated). Slotting agent stores soon-to-release cargo at floor level, slow movers higher. Yellow bin = reserved · green = arrived · grey = empty. Whole-container flows (FCL / Top Up / Transload) are staged in yard slots and bays, not rack bins.</div>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Container Plan Detail</legend>
        <div id="pschInspector" style="min-height:300px;overflow-x:auto">Select a container to open its receiving &amp; putaway plan.</div>
      </fieldset>
      <fieldset style="margin-top:4px"><legend>Agent Reasoning</legend>
        <div id="pschReasoning" style="min-height:80px;overflow-x:auto">Select a container to see the agent reasoning for its plan.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= PSCH OUTBOUND MCC ================= -->
<div id="view-mcc" style="display:none">
  <h3>PSCH Outbound — MCC consolidation containers (back to port)</h3>
  <div class="kpi-strip" id="mccKpis"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>MCC Outbound</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="mccSearch" class="searchbox"
                 placeholder="Search container / destination / status / vessel…" autocomplete="off">
          <div id="mccDrop" class="search-drop"></div>
        </div>
        <div id="mccList" style="max-height:430px;overflow:auto"></div>
      </fieldset>
      <div id="mccSelInfo" class="incoming-selinfo">Select a container to open its vessel &amp; container details — berth, ETD, loading cell.</div>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Viewer</legend>
        <div id="mccMap" style="position:relative;width:100%;max-width:560px;aspect-ratio:1;min-height:380px;border:1px solid #000000;overflow:hidden;background:#D9D9C0"></div>
        <div id="mccBerthInfo" style="margin-top:6px;min-height:36px"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Vessel &amp; Container Details</legend>
        <div id="mccInspector" style="min-height:420px">Select a consolidation container on the left to open its vessel &amp; container details.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= PSCH DISTRIBUTION ================= -->
<div id="view-dist" style="display:none">
  <h3>PSCH Distribution — local LCL delivery · FCL/LCL land release</h3>
  <div class="kpi-strip" id="distKpis"></div>
  <div class="flow-chips" id="distChips"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Outbound Releases (by land)</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="distSearch" class="searchbox"
                 placeholder="Search release / destination / status…" autocomplete="off">
          <div id="distDrop" class="search-drop"></div>
        </div>
        <div id="distList" style="max-height:430px;overflow:auto"></div>
      </fieldset>
      <div id="distSelInfo" class="incoming-selinfo">Select a release to inspect its cargo grouping, destination and timing.</div>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Distribution Board — land releases by destination</legend>
        <div id="distBoard"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Release Detail</legend>
        <div id="distInspector" style="min-height:420px">Select a release on the left to open its detail.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= PSCH TOP UP ================= -->
<div id="view-top" style="display:none">
  <h3>PSCH Top Up — container re-consolidation (topping up)</h3>
  <div class="kpi-strip" id="topKpis"></div>
  <div class="flow-chips" id="topChips"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Re-consolidation Jobs</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="topSearch" class="searchbox"
                 placeholder="Search job / container / destination / status…" autocomplete="off">
          <div id="topDrop" class="search-drop"></div>
        </div>
        <div id="topList" style="max-height:430px;overflow:auto"></div>
      </fieldset>
      <div id="topSelInfo" class="incoming-selinfo">Select a job to open its re-consolidation detail.</div>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Top-up Bays — re-consolidation workflow</legend>
        <div id="topBoard"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Job Detail</legend>
        <div id="topInspector" style="min-height:420px">Select a job on the left to open its detail.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= PSCH QUALITY CONTROL ================= -->
<div id="view-qc" style="display:none">
  <h3>PSCH Quality Control — cargo survey · sampling · repack · rework</h3>
  <div class="kpi-strip" id="qcKpis"></div>
  <div class="flow-chips" id="qcChips"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>QC Tasks</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="qcSearch" class="searchbox"
                 placeholder="Search task / container / kind / status…" autocomplete="off">
          <div id="qcDrop" class="search-drop"></div>
        </div>
        <div id="qcList" style="max-height:430px;overflow:auto"></div>
      </fieldset>
      <div id="qcSelInfo" class="incoming-selinfo">Select a task to open its QC detail.</div>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>QC Stations — survey · sampling · repack · rework</legend>
        <div id="qcBoard"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Task Detail</legend>
        <div id="qcInspector" style="min-height:420px">Select a task on the left to open its detail.</div>
      </fieldset>
    </td>
  </tr></table>
</div>
<!-- ================= CONTROL TOWER ================= -->
<div id="view-tower" style="display:none">
  <h3>Control Tower KPIs — MCC cargo pipeline</h3>
  <div class="kpi-strip" id="kpiRows"></div>
  <div style="margin-top:4px"><fieldset><legend>Attention needed — live findings from agent</legend>
    <div id="excChips" class="trace-filters" style="margin-bottom:3px"></div>
    <table class="grid">
      <thead><tr><th>Severity</th><th>Issue</th><th>Container / Vessel</th><th>Detail, recommendation &amp; action</th></tr></thead>
      <tbody id="exceptionRows"></tbody>
    </table>
    <div class="hint" style="margin-top:3px"><a href="#" onclick="askExceptionAgent();return false;">Ask the agent</a> — or ask anything in PSA Intelligence.</div>
  </fieldset></div>
  <table width="100%"><tr>
    <td width="33%" valign="top"><fieldset><legend>Inbound journey stages</legend><table class="grid"><tbody id="journeyRows"></tbody></table></fieldset></td>
    <td width="34%" valign="top"><fieldset><legend>Outbound consolidation status</legend><table class="grid"><tbody id="outboundStatusRows"></tbody></table></fieldset></td>
    <td width="33%" valign="top"><fieldset><legend>PSCH hub flows (24/7)</legend><table class="grid"><tbody id="hubRows"></tbody></table></fieldset></td>
  </tr></table>
</div>

<!-- ================= TRACE ================= -->
<div id="view-trace" style="display:none">
  <h3>Execution Trace&nbsp;&nbsp;<span class="trace-live" id="traceLive" title="Live — updates on every state poll"><span class="trace-dot"></span>&nbsp;LIVE</span></h3>
  <table width="100%"><tr>
    <td class="trace-filters" id="traceFilters"></td>
    <td align="right" nowrap><input type="button" class="btn" value="Clear Trace" onclick="clearTrace()"></td>
  </tr></table>
  <div class="hint" style="margin-top:2px;margin-bottom:4px">Traces are grouped by actor. <b>AI Changes</b> shows the full lifecycle of every plan change: proposal (approval required) &rarr; your decision (approved / rejected) &rarr; executed tool call.</div>
  <table class="grid">
    <thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Detail</th></tr></thead>
    <tbody id="traceRows"></tbody>
  </table>
</div>

<!-- ================= PSA INTELLIGENCE ================= -->
<div id="view-intel" style="display:none">
  <table width="100%" cellspacing="0" class="intel-shell"><tr>
    <td class="intel-rail">
      <div class="intel-rail-inner">
        <div class="intel-rail-icon active" title="Ask — PSA Intelligence" onclick="showView('intel')">Ask</div>
        <div class="intel-rail-icon" title="Log — Execution trace" onclick="showView('trace')">Log</div>
        <div class="intel-rail-icon" title="Ctrl — Control Tower" onclick="showView('tower')">Ctrl</div>
        <div class="intel-rail-spacer"></div>
        <div class="intel-rail-icon" title="You — profile" style="border-radius:50%;padding:0">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="#606060" style="vertical-align:middle">
            <circle cx="12" cy="8.5" r="4"/>
            <path d="M4.5 20.5c0-4.1 3.4-6.6 7.5-6.6s7.5 2.5 7.5 6.6z"/>
          </svg>
        </div>
      </div>
    </td>
    <td class="intel-main">
      <table width="100%" class="intel-topbar"><tr>
        <td>
          <span class="intel-title">PSA Intelligence</span>
          <div class="intel-tagline">Ask anything about the terminal — containers, vessels, plans, delays.</div>
        </td>
        <td align="right" nowrap class="intel-topbtns">
          <span class="intel-brain">Brain: <b><span id="intelBrain2">connecting&hellip;</span></b>
            &nbsp;&middot;&nbsp; autonomy: <b><span id="intelAutonomy">advisory</span></b></span>
          <input type="button" class="btn" value="Clear" onclick="intelClear()">
        </td>
      </tr></table>

      <div class="intel-hero" id="intelHero">
        <h2>Where should we start?</h2>
        <p class="sub">Track a container, explain a plan, inspect the warehouse, review the
          pipeline, or see what changed recently. Answers are computed live from the simulation data.</p>
        <div class="intel-quick" id="intelQuick"></div>
      </div>

      <div class="intel-thread" id="intelThread"></div>

      <!-- The prompt bar stays visible at the bottom, like any chat interface,
           so you can keep asking without clearing the conversation. -->
      <div class="intel-promptwrap">
        <table class="intel-prompt" cellspacing="0"><tr>
          <td width="100%">
            <input type="text" id="intelAsk" class="intel-input"
                   placeholder="Ask anything about the terminal…" autocomplete="off"
                   onkeydown="if(event.key==='Enter' && !event.shiftKey){ intelSend(); event.preventDefault(); }">
          </td>
          <td nowrap>&nbsp;<input type="button" id="intelSendBtn" class="intel-send" value="Ask" onclick="intelSend()"></td>
        </tr></table>
      </div>

      <div class="intel-foot">Answers are computed live through the same agent layer as the rest of the
        tower. &nbsp;<a href="#" onclick="showView('trace');return false;">execution trace</a></div>
    </td>
  </tr></table>
</div>

<table class="statusbar"><tr>
  <td id="statusText" width="80%">Connecting...</td>
  <td id="lastUpdated" align="right">—</td>
</tr></table>

<div class="footer">© 2026 PSA Control Tower · Best viewed at 1024×768 in Netscape 4.0+ · All data synthetic</div>

<script>
var state = null;
var view = 'in';
var lastView = null;
var selected = null; // {kind:'incoming'|'outbound'|'berth', id:...}
var selectedBerth = null;
// Hub page filters / selections.
var flowFilter = 'all';   // inbound flow chips
var distFilter = 'all';   // distribution kind chips
var topFilter = 'all';    // top-up status chips
var distQuery = '';       // distribution list search box
var topQuery = '';        // top-up list search box
var qcFilter = 'all';     // QC kind chips
var distSel = null;       // selected release {kind, id}
var topSel = null;        // selected top-up job id
var qcSel = null;         // selected QC task id
var mccSel = null;        // selected MCC outbound container id

function $(id){ return document.getElementById(id); }

function setDetail(open){
  var el = document.getElementById('view-' + view);
  if(el) el.classList.toggle('detail-open', !!open);
}

// Restore a hub sub-page to its original arrangement (columns at their normal
// widths, nothing selected, search cleared, dropdown closed). Called when the
// user navigates back to the page so stale detail-open reflow and selections
// do not survive a page switch.
function resetInView(){
  selected = null;
  selectedBerth = null;
  closeIncomingDrop();
  $('incomingSearch').value = '';
  incomingQuery = '';
  flowFilter = 'all';
  setDetail(false);
}
function resetStoreView(){
  selected = null;
  pschSelRack = null;
  pschSelBin = null;
  setDetail(false);
}
function resetDistView(){
  distSel = null;
  distFilter = 'all';
  setDetail(false);
}
function resetTopView(){
  topSel = null;
  topFilter = 'all';
  setDetail(false);
}
function resetQcView(){
  qcSel = null;
  qcFilter = 'all';
  setDetail(false);
}
function resetMccView(){
  mccSel = null;
  setDetail(false);
}

function isoDT(iso){ return iso ? iso.replace('T',' ').slice(0,16) : '—'; }
function isoDTS(iso){ return iso ? iso.replace('T',' ').slice(0,19) : '—'; }
function isoTime(iso){ return iso ? iso.slice(11,16) : '—'; }

async function api(path, body){
  var opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  var resp = await fetch(path, opt);
  if(!resp.ok) throw new Error('HTTP ' + resp.status);
  return resp.json();
}

async function refresh(){
  captureValues();            // remember what is on screen right now
  state = await api('/api/state');
  render();
  applyFlashes();             // flash every value that just changed
}

function render(){
  renderClock();
  renderIn();
  renderStore();
  renderMcc();
  renderDist();
  renderTop();
  renderQc();
  renderTower();
  renderTrace();
  renderIntel();
  renderTicker();
  renderStatus();
  showView(view);
}

// ---- Inbound (master container list + berth map + ship tracker) ------------
function renderIn(){
  renderKpiStrip();
  renderFlowChips();
  renderIncoming();
  renderMap();
  renderInspector();
}

// ---- Storage (the agent's receiving / putaway / picking playground) --------
function renderStore(){
  renderPsch();
  if(selected && selected.kind === 'incoming'){
    var m = null;
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) m = x; });
    if(m) $('pschSelInfo').innerHTML = 'Selected: <b>' + m.container_id + '</b> ' + badge(m.status) + ' · ' + m.receiving_area;
  } else {
    $('pschSelInfo').innerHTML = 'Select a container to open its receiving &amp; putaway plan.';
  }
}

function renderClock(){
  $('simClock').innerText = isoDTS(state.sim_now) + 'Z';
}

function badge(status){
  var map = {
    'En Route (Sea)':'st-sea', 'Unloaded':'st-unload', 'Depot':'st-depot',
    'En Route (Road)':'st-road', 'Arrived':'st-arr',
    'loaded':'st-loaded', 'staged':'st-staged', 'released':'st-road', 'in_transit':'st-road',
    'delivered':'st-arr', 'pending':'st-sea', 'in_progress':'st-unload', 'done':'st-arr'
  };
  var cls = map[status] || '';
  return '<span class="st-badge ' + cls + '">' + status + '</span>';
}

function countdownHtml(h){
  if(h === null || h === undefined) return '—';
  var txt = (h < 0 ? 'OVERDUE' : h.toFixed(1) + 'h');
  var cls = h < 6 ? 'countdown crit' : (h < 12 ? 'countdown warn' : 'countdown');
  return '<span class="' + cls + '">' + txt + '</span>';
}

function kpiStripHtml(cells){
  // One compact strip cell per metric (label + value), hugging its content
  // instead of a full-width table of stacked rows.
  return cells.map(function(c){
    return '<span class="kpi-cell" data-kpi="' + c[0] + '"><span class="k">' + c[0] + '</span> <span class="metric">' + c[1] + '</span></span>';
  }).join('');
}

// ---- Bloomberg-style change flashes -----------------------------------------
// The page re-renders from a fresh /api/state every 8s. Before re-rendering we
// snapshot the visible values (keyed by element identity), then after render we
// compare: anything whose value moved gets a short flash (green ▲ up, red ▼
// down, amber for a status/new change) so the viewer sees *what* just changed.
var prevVals = {};

function stripArrows(t){ return (t || '').replace(/[▲▼]/g, ''); }
function firstNum(t){
  var m = stripArrows(t).match(/-?\d+(\.\d+)?/);
  return m ? parseFloat(m[0]) : null;
}
function deltaDir(oldT, newT){
  var a = firstNum(oldT), b = firstNum(newT);
  if(a === null || b === null || a === b) return 0;
  return b > a ? 1 : -1;
}

// ---- Flash display settings (Bloomberg PDF <GO> style) ---------------------
// Palette, speed, brightness and an on/off switch, persisted in localStorage
// and applied through CSS variables so the flash keyframes stay declarative.
var FLASH_SPEEDS = {slow:'4s', normal:'2.5s', fast:'1.1s'};
var FLASH_LEVELS = {
  subtle: {a:0.45, glow:4},
  normal: {a:1,    glow:8},
  vivid:  {a:1,    glow:14}
};
var FLASH_PALETTES = {
  classic: {
    up:   {bg:'#C8F7C5', glow:'#2E8B57', text:'#2E8B57'},
    down: {bg:'#FDE2E2', glow:'#C0504D', text:'#C0504D'},
    chg:  {bg:'#FFF3B8', glow:'#DAA520'}
  },
  cb: {  // colour-blind safe: blue = up, orange = down, yellow = change
    up:   {bg:'#CFE3F2', glow:'#2F6FB2', text:'#1F5B9E'},
    down: {bg:'#FDE2D9', glow:'#B9702F', text:'#9E4E1E'},
    chg:  {bg:'#FFF3B8', glow:'#DAA520'}
  },
  mono: {  // blue = any movement, grey = change (for high-focus sessions)
    up:   {bg:'#CFE3F2', glow:'#2F6FB2', text:'#1F5B9E'},
    down: {bg:'#D9D9D9', glow:'#606060', text:'#404040'},
    chg:  {bg:'#EFEFEF', glow:'#808080'}
  }
};
var flashSettings = {enabled:true, speed:'normal', brightness:'normal', palette:'classic'};
function hexA(hex, a){
  var h = (hex || '').replace('#','');
  if(h.length !== 6) return hex;
  var r = parseInt(h.substr(0,2),16), g = parseInt(h.substr(2,2),16), b = parseInt(h.substr(4,2),16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
}
function loadFlashSettings(){
  try{
    var s = JSON.parse(localStorage.getItem('psa-flash-settings') || '{}');
    if(s && typeof s === 'object'){
      if('enabled' in s) flashSettings.enabled = !!s.enabled;
      if(FLASH_SPEEDS[s.speed]) flashSettings.speed = s.speed;
      if(FLASH_LEVELS[s.brightness]) flashSettings.brightness = s.brightness;
      if(FLASH_PALETTES[s.palette]) flashSettings.palette = s.palette;
    }
  }catch(e){}
}
function saveFlashSettings(){
  try{ localStorage.setItem('psa-flash-settings', JSON.stringify(flashSettings)); }catch(e){}
}
function applyFlashSettings(){
  var s = flashSettings, pal = FLASH_PALETTES[s.palette], lvl = FLASH_LEVELS[s.brightness];
  var root = document.documentElement.style;
  root.setProperty('--flash-dur', FLASH_SPEEDS[s.speed]);
  root.setProperty('--flash-glow', lvl.glow + 'px');
  root.setProperty('--flash-up-bg', hexA(pal.up.bg, lvl.a));
  root.setProperty('--flash-up-glow', pal.up.glow);
  root.setProperty('--flash-up-text', pal.up.text);
  root.setProperty('--flash-down-bg', hexA(pal.down.bg, lvl.a));
  root.setProperty('--flash-down-glow', pal.down.glow);
  root.setProperty('--flash-down-text', pal.down.text);
  root.setProperty('--flash-chg-bg', hexA(pal.chg.bg, lvl.a));
  root.setProperty('--flash-chg-glow', pal.chg.glow);
  document.body.classList.toggle('flashes-off', !s.enabled);
  var en = $('flashEnabled'); if(en) en.checked = s.enabled;
  var sp = $('flashSpeed'); if(sp) sp.value = s.speed;
  var br = $('flashBright'); if(br) br.value = s.brightness;
  var pa = $('flashPalette'); if(pa) pa.value = s.palette;
}
function setFlashSetting(key, value){
  flashSettings[key] = value;
  saveFlashSettings();
  applyFlashSettings();
}
function toggleFlashSettings(){
  var p = $('flashSettings');
  if(p) p.style.display = (p.style.display === 'block') ? 'none' : 'block';
}

function captureValues(){
  prevVals = {};
  document.querySelectorAll('.kpi-strip').forEach(function(strip){
    var sid = strip.id;
    strip.querySelectorAll('.kpi-cell').forEach(function(cell){
      var m = cell.querySelector('.metric');
      prevVals['kpi|' + sid + '|' + (cell.getAttribute('data-kpi') || '')] =
        m ? stripArrows(m.textContent) : '';
    });
  });
  document.querySelectorAll('.mp-item').forEach(function(row){
    var cidEl = row.querySelector('.cid');
    if(!cidEl) return;
    var badge = row.querySelector('.st-badge');
    prevVals['row|' + cidEl.textContent.trim() + '|status'] = badge ? badge.textContent : '';
  });
  document.querySelectorAll('.asrs-table tr[data-sid]').forEach(function(row){
    var charge = row.querySelector('td:nth-child(2)');
    prevVals['asrs|' + row.getAttribute('data-sid') + '|charge'] =
      charge ? stripArrows(charge.textContent) : '';
  });
  document.querySelectorAll('.psch-rel-lane').forEach(function(chip){
    var kind = chip.classList.contains('rcv') ? 'rcv' : 'rel';
    var no = chip.getAttribute('data-rel') || chip.getAttribute('data-lane') || '';
    var count = chip.querySelector('.psch-rel-pallets');
    prevVals['lane|' + kind + '|' + no] = count ? stripArrows(count.textContent) : '';
  });
  document.querySelectorAll('.psch-bin-grid td[data-bin]').forEach(function(td){
    prevVals['bin|' + td.getAttribute('data-bin')] = td.className;
  });
  // Tower tables: journey-stage / outbound-status / hub-flow count rows.
  ['journeyRows', 'outboundStatusRows', 'hubRows'].forEach(function(tid){
    var tbody = document.getElementById(tid);
    if(!tbody) return;
    tbody.querySelectorAll('tr').forEach(function(tr){
      var label = tr.querySelector('td') ? tr.querySelector('td').textContent : '';
      var count = tr.querySelector('td:nth-child(2)');
      prevVals['tower|' + tid + '|' + label] = count ? stripArrows(count.textContent) : '';
    });
  });
}

function flashEl(el, cls){
  el.classList.remove('flash-up', 'flash-down', 'flash-change');
  // Remember the element's own resting background so the flash fades back to
  // exactly that colour instead of showing the page behind it (which turned
  // grey before snapping to white). getComputedStyle returns the resolved
  // colour at the moment of flashing — after the new state has rendered.
  try{
    el.style.setProperty('--flash-end', getComputedStyle(el).backgroundColor || 'transparent');
  }catch(e){ el.style.setProperty('--flash-end', 'transparent'); }
  el.classList.add(cls);
}

function applyFlashes(){
  if(!flashSettings.enabled) return;  // flashes switched off: keep values in sync, show nothing
  // KPI cells — flash up/down/change and append a small ▲/▼ delta marker.
  document.querySelectorAll('.kpi-strip').forEach(function(strip){
    var sid = strip.id;
    strip.querySelectorAll('.kpi-cell').forEach(function(cell){
      var m = cell.querySelector('.metric');
      if(!m) return;
      var key = 'kpi|' + sid + '|' + (cell.getAttribute('data-kpi') || '');
      var val = stripArrows(m.textContent);
      var prev = prevVals[key];
      if(prev !== undefined && prev !== val){
        var d = deltaDir(prev, val);
        if(d > 0){ flashEl(cell, 'flash-up'); appendDelta(m, '▲', 'delta-up'); }
        else if(d < 0){ flashEl(cell, 'flash-down'); appendDelta(m, '▼', 'delta-down'); }
        else flashEl(cell, 'flash-change');
      }
    });
  });
  // Container rows — amber flash when a container's journey status changed.
  document.querySelectorAll('.mp-item').forEach(function(row){
    var cidEl = row.querySelector('.cid');
    if(!cidEl) return;
    var badge = row.querySelector('.st-badge');
    var key = 'row|' + cidEl.textContent.trim() + '|status';
    var val = badge ? badge.textContent : '';
    var prev = prevVals[key];
    if(prev !== undefined && prev !== val) flashEl(row, 'flash-change');
  });
  // AS/RS stacker charge cells — green when charging up, red when draining.
  document.querySelectorAll('.asrs-table tr[data-sid]').forEach(function(row){
    var charge = row.querySelector('td:nth-child(2)');
    if(!charge) return;
    var key = 'asrs|' + row.getAttribute('data-sid') + '|charge';
    var val = stripArrows(charge.textContent);
    var prev = prevVals[key];
    if(prev !== undefined && prev !== val){
      var d = deltaDir(prev, val);
      if(d > 0){ flashEl(charge, 'flash-up'); appendDelta(charge, '▲', 'delta-up'); }
      else if(d < 0){ flashEl(charge, 'flash-down'); appendDelta(charge, '▼', 'delta-down'); }
      else flashEl(charge, 'flash-change');
    }
  });
  // Lane chips — flash when a lane's staged container count changed.
  document.querySelectorAll('.psch-rel-lane').forEach(function(chip){
    var kind = chip.classList.contains('rcv') ? 'rcv' : 'rel';
    var no = chip.getAttribute('data-rel') || chip.getAttribute('data-lane') || '';
    var count = chip.querySelector('.psch-rel-pallets');
    if(!count) return;
    var key = 'lane|' + kind + '|' + no;
    var val = stripArrows(count.textContent);
    var prev = prevVals[key];
    if(prev !== undefined && prev !== val) flashEl(chip, 'flash-change');
  });
  // Bin cells — amber when a bin's occupancy state changed (reserved -> arrived).
  document.querySelectorAll('.psch-bin-grid td[data-bin]').forEach(function(td){
    var key = 'bin|' + td.getAttribute('data-bin');
    var prev = prevVals[key];
    if(prev !== undefined && prev !== td.className) flashEl(td, 'flash-change');
  });
  // Tower tables — flash the count cell when a journey/outbound/hub count moved.
  ['journeyRows', 'outboundStatusRows', 'hubRows'].forEach(function(tid){
    var tbody = document.getElementById(tid);
    if(!tbody) return;
    tbody.querySelectorAll('tr').forEach(function(tr){
      var label = tr.querySelector('td') ? tr.querySelector('td').textContent : '';
      var count = tr.querySelector('td:nth-child(2)');
      if(!count) return;
      var key = 'tower|' + tid + '|' + label;
      var val = stripArrows(count.textContent);
      var prev = prevVals[key];
      if(prev !== undefined && prev !== val){
        var d = deltaDir(prev, val);
        if(d > 0){ flashEl(count, 'flash-up'); appendDelta(count, '▲', 'delta-up'); }
        else if(d < 0){ flashEl(count, 'flash-down'); appendDelta(count, '▼', 'delta-down'); }
        else flashEl(count, 'flash-change');
      }
    });
  });
}

function appendDelta(el, arrow, cls){
  var s = document.createElement('span');
  s.className = 'delta ' + cls;
  s.textContent = arrow;
  el.appendChild(s);
}

// Hub flow categories shown to the user: LCL and FCL containers are both
// "Distribution" (they leave PSCH by land); internally the data still tracks
// the specific lcl/fcl flow.
function flowKey(f){ return {mcc:'mcc', lcl:'distribution', fcl:'distribution', topup:'topup', transload:'transload'}[f] || f; }
function flowLabel(f){ return {mcc:'MCC', lcl:'Distribution', fcl:'Distribution', topup:'Top Up', transload:'Transload'}[f] || (f || '—'); }
function flowClass(f){ return {mcc:'mcc', lcl:'dist', fcl:'dist', topup:'topup', transload:'transload'}[f] || 'yard'; }
function filterLabel(f){ return {all:'All', mcc:'MCC', distribution:'Distribution', topup:'Top Up', transload:'Transload'}[f] || f; }
function flowCount(f){ return state.inbound.filter(function(e){ return flowKey(e.flow) === f; }).length; }

function renderKpiStrip(){
  var k = state.kpis;
  var cells = [
    ['Inbound total (24h)', state.inbound.length],
    ['MCC', flowCount('mcc')],
    ['Distribution', flowCount('distribution')],
    ['Top Up', flowCount('topup')],
    ['Transload', flowCount('transload')],
    ['At sea', k.journey_counts['En Route (Sea)']],
    ['Arrived @ PSCH', k.arrived_at_psch],
    ['Arrival rate (next 6h)', k.arrival_rate + ' ctn/h'],
    ['Avg sea→PSCH pipeline', k.avg_pipeline_h + ' h'],
    ['Bin util', k.bin_util + '%']
  ];
  $('kpiStrip').innerHTML = kpiStripHtml(cells);
}

function renderFlowChips(){
  renderChips('flowChips', flowFilter, [
    ['all', 'All'], ['mcc', 'MCC'], ['distribution', 'Distribution'],
    ['topup', 'Top Up'], ['transload', 'Transload']
  ], 'setFlowFilter');
}
function setFlowFilter(f){
  flowFilter = f;
  renderFlowChips();
  renderIncoming();
  openIncomingDrop(); // show the filtered list so the filter is visibly working
}

function renderChips(elId, current, options, onclick){
  var html = options.map(function(o){
    return '<button type="button" class="chip' + (o[0] === current ? ' active' : '') +
      '" onclick="' + onclick + '(\'' + o[0] + '\')">' + o[1] + '</button>';
  }).join('');
  $(elId).innerHTML = html;
}

function flowPill(f){
  return '<span class="flow-pill ' + flowClass(f) + '">' + flowLabel(f) + '</span>';
}

var incomingQuery = '';

function incomingMatches(m, q){
  q = q.toLowerCase();
  var hay = (m.container_id + ' ' + (m.size || '') + ' ' + (m.type || '') + ' ' + m.vessel_name + ' ' +
             m.vessel_id + ' ' + m.status + ' ' + (m.berth_id || '') + ' ' + (m.bay_label || '') + ' ' +
             (m.destination || '') + ' ' + flowLabel(m.flow) + ' ' + (m.flow || '')).toLowerCase();
  return q === '' || hay.indexOf(q) !== -1;
}

function renderIncoming(){
  var q = incomingQuery;
  var items = state.inbound.filter(function(m){
    return (flowFilter === 'all' || flowKey(m.flow) === flowFilter) && incomingMatches(m, q);
  });
  var html = '';
  items.forEach(function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (m.status === 'Arrived') ? 'arrived' : countdownHtml(m.hours_until);
    var destTxt = (m.flow === 'mcc') ? 'bound ' + (m.destination || '—')
      : ((m.destination || 'transload') === 'transload' ? 'transload @ PSCH' : '→ ' + (m.destination || '—'));
    html += '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickIncoming(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + flowPill(m.flow) + ' &nbsp;' + m.size + ' · ' + m.type + '<br>' +
      badge(m.status) + ' &nbsp;via <b>' + m.vessel_name + '</b> · ' + destTxt + '<br>' +
      'PSCH receipt ETA: ' + isoDT(m.psch_receipt_eta) + ' (' + etaTxt + ') · ' + m.cargoes + ' cargoes' +
      '</button>';
  });
  if(!items.length) html = '<div class="no-match">No inbound containers match &lsquo;' + (q || '') + '&rsquo;' + (flowFilter !== 'all' ? ' in ' + filterLabel(flowFilter) : '') + '.</div>';
  html += '<div class="drop-foot">' + items.length + ' of ' + state.inbound.length + ' inbound containers in the pipeline</div>';
  $('incomingDrop').innerHTML = html;
  var info = 'Click to search from all inbound containers, use filter to view flow specific containers';
  if(flowFilter !== 'all' || incomingQuery){
    // a filter or search is active: show the matching count against the total
    info += ' — ' + items.length + ' of ' + state.inbound.length + ' inbound containers shown';
  } else {
    // live total, updated with the simulation lifecycle on every poll
    info += ' — ' + state.inbound.length + ' inbound containers';
  }
  if(selected && selected.kind === 'incoming'){
    state.inbound.forEach(function(m){ if(m.container_id === selected.id) info = 'Selected: <b>' + m.container_id + '</b> ' + badge(m.status); });
  }
  $('incomingSelInfo').innerHTML = info;
}

function openIncomingDrop(){ $('incomingDrop').style.display = 'block'; renderIncoming(); }
function closeIncomingDrop(){ $('incomingDrop').style.display = 'none'; }
function closeIncomingDropSoon(){ setTimeout(function(){ closeIncomingDrop(); }, 120); }
function filterIncoming(){ incomingQuery = $('incomingSearch').value; $('incomingDrop').style.display = 'block'; renderIncoming(); }
function incomingKey(ev){
  if(ev.key === 'Escape'){ closeIncomingDrop(); $('incomingSearch').blur(); }
  else if(ev.key === 'Enter'){
    var first = state.inbound.filter(function(m){ return incomingMatches(m, incomingQuery); })[0];
    if(first) pickIncoming(first.container_id);
  }
}
function pickIncoming(cid){ closeIncomingDrop(); $('incomingSearch').blur(); $('incomingSearch').value = ''; incomingQuery = ''; selectIncoming(cid); }

// ---- Inbound-style type-ahead combobox (shared by every hub sub-page) ------
// Each sub-page search box behaves exactly like the PSCH Inbound one: focusing
// or typing opens a dropdown of live matches, Enter picks the first match,
// Escape / blur closes it, and picking an item selects it and clears the box.
// The static browse list below each box stays as the full list.
var combos = {};
function makeCombo(name, cfg){
  var input = $(cfg.input), drop = $(cfg.drop);
  function items(){ return cfg.items(); }
  function matches(it, q){ return cfg.matches(it, q); }
  function renderDrop(){
    var q = input.value;
    var list = items().filter(function(it){ return matches(it, q); });
    var html = list.map(cfg.row).join('');
    if(!list.length) html = '<div class="no-match">' + cfg.noMatch(q) + '</div>';
    html += '<div class="drop-foot">' + list.length + ' of ' + items().length + ' ' + cfg.label + '</div>';
    drop.innerHTML = html;
  }
  function open(){ drop.style.display = 'block'; renderDrop(); }
  function reset(){ drop.style.display = 'none'; input.blur(); input.value = ''; }
  input.onfocus = open;
  input.onclick = open;
  input.oninput = open;
  input.onkeydown = function(ev){
    if(ev.key === 'Escape'){ reset(); }
    else if(ev.key === 'Enter'){
      var first = items().filter(function(it){ return matches(it, input.value); })[0];
      if(first) cfg.pick(first);
    }
  };
  input.onblur = function(){ setTimeout(function(){ drop.style.display = 'none'; }, 120); };
  combos[name] = {open: open, reset: reset, render: renderDrop};
}

// PSCH Storage — search the same inbound list the storage page browses.
makeCombo('psch', {
  input: 'pschSearch', drop: 'pschDrop',
  items: function(){ return state.inbound; },
  matches: function(m, q){
    q = q.toLowerCase();
    return q === '' || (m.container_id + ' ' + (m.size || '') + ' ' + (m.type || '') + ' ' + m.vessel_name + ' ' +
      m.status + ' ' + (m.destination || '') + ' ' + flowLabel(m.flow) + ' ' + (m.flow || '') + ' ' +
      (m.bin_location || '') + ' ' + (m.receiving_area || '')).toLowerCase().indexOf(q) !== -1;
  },
  row: function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    return '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickPschFromDrop(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + flowPill(m.flow) + ' &nbsp;' + badge(m.status) + '<br>' +
      '<b>' + m.cargoes + '</b> cargoes · putaway <b>' + m.bin_location + '</b> · ' + m.putaway_robot + '<br>' +
      'Receiving ' + m.receiving_area + ' · ETA ' + isoDT(m.psch_receipt_eta) + '</button>';
  },
  noMatch: function(q){ return 'No inbound containers match &lsquo;' + (q || '') + '&rsquo;.'; },
  label: 'inbound containers',
  pick: function(m){ pickPschFromDrop(m.container_id); }
});
function pickPschFromDrop(cid){ combos.psch.reset(); pickPschIncoming(cid); }

// PSCH MCC — search the consolidation containers.
makeCombo('mcc', {
  input: 'mccSearch', drop: 'mccDrop',
  items: function(){ return state.outbound; },
  matches: function(o, q){
    q = q.toLowerCase();
    return q === '' || (o.container_id + ' ' + o.destination + ' ' + o.status + ' ' +
      o.bound_vessel_name + ' ' + (o.bound_vessel_id || '') + ' ' + (o.berth_id || '') + ' ' +
      (o.vessel_etd || '') + ' ' + (o.eta_loading_area || '')).toLowerCase().indexOf(q) !== -1;
  },
  row: function(o){
    var cls = (mccSel === o.container_id) ? 'mp-item sel' : 'mp-item';
    return '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickMccFromDrop(\'' + o.container_id + '\')">' +
      '<span class="cid">' + o.container_id + '</span> &nbsp;<span class="flow-pill mcc">MCC</span><br>' +
      badge(o.status) + ' &nbsp;→ <b>' + o.destination + '</b><br>' +
      'Bound: <b>' + o.bound_vessel_name + '</b> · ETA loading ' + isoDT(o.eta_loading_area) + '</button>';
  },
  noMatch: function(q){ return 'No consolidation containers match &lsquo;' + (q || '') + '&rsquo;.'; },
  label: 'consolidation containers',
  pick: function(o){ pickMccFromDrop(o.container_id); }
});
function pickMccFromDrop(cid){ combos.mcc.reset(); pickMcc(cid); }

// PSCH Distribution — search the land releases (respects the kind chips).
makeCombo('dist', {
  input: 'distSearch', drop: 'distDrop',
  items: function(){ return distItems().filter(function(d){ return distFilter === 'all' || d.kind === distFilter; }); },
  matches: function(d, q){
    q = q.toLowerCase();
    return q === '' || (d.id + ' ' + d.destination + ' ' + d.status + ' ' + d.kind).toLowerCase().indexOf(q) !== -1;
  },
  row: function(d){
    var cls = (distSel && distSel.kind === d.kind && distSel.id === d.id) ? 'mp-item sel' : 'mp-item';
    return '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickDistFromDrop(\'' + d.kind + '\',\'' + d.id + '\')">' +
      '<span class="cid">' + d.id + '</span> &nbsp;<span class="flow-pill ' + (d.kind === 'LCL delivery' ? 'lcl' : 'fcl') + '">' + d.kind + '</span><br>' +
      badge(d.status) + ' &nbsp;→ <b>' + d.destination + '</b><br>' +
      'ETA destination ' + isoDT(d.eta) + ' · ' + d.pallets + ' pallets</button>';
  },
  noMatch: function(q){ return 'No land releases match &lsquo;' + (q || '') + '&rsquo;.'; },
  label: 'land releases',
  pick: function(d){ pickDistFromDrop(d.kind, d.id); }
});
function pickDistFromDrop(kind, id){ combos.dist.reset(); pickDist(kind, id); }

// PSCH Top Up — search the re-consolidation jobs (respects the status chips).
makeCombo('top', {
  input: 'topSearch', drop: 'topDrop',
  items: function(){ return state.top_up.filter(function(j){ return topFilter === 'all' || j.status === topFilter; }); },
  matches: function(j, q){
    q = q.toLowerCase();
    return q === '' || (j.job_id + ' ' + (j.container_id || '') + ' ' + (j.destination || '') + ' ' + j.status).toLowerCase().indexOf(q) !== -1;
  },
  row: function(j){
    var cls = (topSel === j.job_id) ? 'mp-item sel' : 'mp-item';
    return '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickTopFromDrop(\'' + j.job_id + '\')">' +
      '<span class="cid">' + j.job_id + '</span> &nbsp;' + flowPill('topup') + '<br>' +
      badge(j.status) + ' &nbsp;container <b>' + j.container_id + '</b><br>' +
      '→ <b>' + j.destination + '</b> · ' + isoDT(j.window_start) + '–' + isoDT(j.window_end) + '</button>';
  },
  noMatch: function(q){ return 'No top-up jobs match &lsquo;' + (q || '') + '&rsquo;.'; },
  label: 'top-up jobs',
  pick: function(j){ pickTopFromDrop(j.job_id); }
});
function pickTopFromDrop(id){ combos.top.reset(); pickTop(id); }

// PSCH Quality Control — search the QC tasks (respects the kind chips).
makeCombo('qc', {
  input: 'qcSearch', drop: 'qcDrop',
  items: function(){ return state.qc.filter(function(t){ return qcFilter === 'all' || t.kind === qcFilter; }); },
  matches: function(t, q){
    q = q.toLowerCase();
    return q === '' || (t.task_id + ' ' + (t.container_id || '') + ' ' + (t.kind || '') + ' ' + t.status + ' ' +
      (t.flow || '') + ' ' + (t.station || '') + ' ' + (t.destination || '')).toLowerCase().indexOf(q) !== -1;
  },
  row: function(t){
    var cls = (qcSel === t.task_id) ? 'mp-item sel' : 'mp-item';
    return '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickQcFromDrop(\'' + t.task_id + '\')">' +
      '<span class="cid">' + t.task_id + '</span> &nbsp;' + qcKindPill(t.kind) + ' &nbsp;' + flowPill(t.flow) + '<br>' +
      badge(t.status) + ' &nbsp;container <b>' + t.container_id + '</b><br>' +
      t.station + ' · ' + isoDT(t.window_start) + '–' + isoDT(t.window_end) + '</button>';
  },
  noMatch: function(q){ return 'No QC tasks match &lsquo;' + (q || '') + '&rsquo;.'; },
  label: 'QC tasks',
  pick: function(t){ pickQcFromDrop(t.task_id); }
});
function pickQcFromDrop(id){ combos.qc.reset(); pickQc(id); }

// ---- Map -------------------------------------------------------------------
var BERTHS = {{BERTHS_JSON}};

function renderMap(){ renderMapInto('facilityMap', 'berthInfo', 'in'); }
function renderMccMap(){ renderMapInto('mccMap', 'mccBerthInfo', 'mcc'); }

function renderMapInto(mapEl, infoEl, page){
  var html = '<img src="/map.png" width="100%" height="100%" style="position:absolute;left:0;top:0;display:block" alt="Tuas berth plan">';
  var highlightBerth = null;
  if(page === 'in'){
    if(selected && selected.kind === 'outbound'){
      state.outbound.forEach(function(o){ if(o.container_id === selected.id && o.berth_id) highlightBerth = o.berth_id; });
    } else if(selected && selected.kind === 'incoming'){
      state.inbound.forEach(function(m){ if(m.container_id === selected.id && m.berth_id) highlightBerth = m.berth_id; });
    }
  } else if(mccSel){
    state.outbound.forEach(function(o){ if(o.container_id === mccSel && o.berth_id) highlightBerth = o.berth_id; });
  }
  state.berths.forEach(function(b){
    var v = b.vessel;
    var cls = v ? (v.status === 'docked' ? 'berth occ' : 'berth inb') : 'berth free';
    var sel = (page === 'in') ? selectedBerth : null;
    if(sel === b.id) cls += ' sel';
    else if(highlightBerth === b.id) cls += ' hl';
    var title = b.id + ': ' + (v ? v.vessel_name + ' (' + v.status + ')' : 'berth available');
    // Only the Inbound page offers per-berth inspection; on the MCC page the
    // berths are a static viewer (no ship detail per berth there), so they
    // render without a click handler. The selected container's own berth is
    // still highlighted below.
    var click = (page === 'in') ? "selectBerth('" + b.id + "')" : '';
    html += '<div class="' + cls + '" style="left:' + b.x + '%;top:' + b.y + '%;width:' + b.w + '%;height:' + b.h + '%"' +
      (click ? ' onclick="' + click + '"' : '') + ' title="' + title + '">' +
      '<font>' + b.id + '</font></div>';
  });
  var d = state.drayage;
  html +=
    '<div class="map-kpi" style="left:2px;top:2px">Yard util <b>' + state.kpis.avg_yard_util + '%</b></div>' +
    '<div class="map-kpi" style="left:2px;top:22px">Drayage <b>' + d.available_trucks + '/' + d.total_trucks + '</b> PM</div>' +
    '<div style="position:absolute;right:2px;bottom:2px;font-size:8px;color:#FFFFFF;background:#000080;padding:1px 3px">Static berth plan — no GPS tracking</div>';
  $(mapEl).innerHTML = html;
  renderBerthInfoInto(infoEl, page);
}
function selectBerth(id){
  selectedBerth = id;
  selected = {kind:'berth', id:id};
  setDetail(false);
  closeIncomingDrop();
  api('/api/berth/inspect', {berth: id}); // log the inspection to the trace
  renderMap();
  renderInspector();
}
function renderBerthInfo(){ renderBerthInfoInto('berthInfo', 'in'); }
function renderBerthInfoInto(infoEl, page){
  var b = null;
  if(page === 'in'){
    if(selected && selected.kind === 'berth'){
      state.berths.forEach(function(x){ if(x.id === selectedBerth) b = x; });
    } else if(selected && (selected.kind === 'incoming' || selected.kind === 'outbound')){
      var hl = null;
      var arr = (selected.kind === 'incoming') ? state.inbound : state.outbound;
      arr.forEach(function(e){ if(e.container_id === selected.id && e.berth_id) hl = e.berth_id; });
      state.berths.forEach(function(x){ if(x.id === hl) b = x; });
    }
  } else {
    // MCC page: berths are not clickable; the info slot only reports the
    // berth of the selected consolidation container (highlighted on the map).
    if(mccSel){
      var hl = null;
      state.outbound.forEach(function(e){ if(e.container_id === mccSel && e.berth_id) hl = e.berth_id; });
      state.berths.forEach(function(x){ if(x.id === hl) b = x; });
    }
  }
  if(!b){ $(infoEl).innerHTML = ''; return; }
  var v = b.vessel;
  var html = '<b>Berth ' + b.id + '</b> (Pier ' + b.pier + '): ';
  if(v && v.status === 'docked'){
    html += '<font color="#000080"><b>OCCUPIED</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + '), alongside since ' + (v.eta || '').slice(0,16) + ', ETD ' + (v.etd || '—').slice(0,16) + ', ' + v.moves_planned + ' TEU to work.';
  } else if(v && v.status === 'inbound'){
    html += '<font color="#B07000"><b>BOOKED</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + ') inbound, ETA ' + (v.eta || '').slice(0,16) + ', ' + v.moves_planned + ' TEU planned, ' + v.distance_nm + ' nm out at ' + v.speed_knots + ' kn.';
  } else {
    html += '<font color="#008000"><b>AVAILABLE</b></font> — no vessel assigned.';
  }
  $(infoEl).innerHTML = html;
}

// ---- Ship-tracker inspector -------------------------------------------------
function row(l, v){ return '<tr><td width="42%">' + l + '</td><td>' + v + '</td></tr>'; }
function selectIncoming(cid){
  if(selected && selected.kind === 'incoming' && selected.id === cid){
    // clicking the selected container again unselects it and restores the layout
    selected = null; selectedBerth = null; setDetail(false);
    renderIncoming(); renderMap(); renderInspector(); return;
  }
  selected = {kind:'incoming', id:cid}; selectedBerth = null; setDetail(true); renderIncoming(); renderMap(); renderInspector();
}
function selectOutbound(cid){ selected = {kind:'outbound', id:cid}; selectedBerth = null; setDetail(true); renderMap(); renderInspector(); }

var STAGES = ['En Route (Sea)', 'Unloaded', 'Depot', 'En Route (Road)', 'Arrived'];

function pad2(n){ return (n === null || n === undefined) ? '—' : (n < 10 ? '0' + n : '' + n); }

function renderInspector(){
  if(!selected || (selected.kind !== 'incoming' && selected.kind !== 'outbound')){
    $('inspector').innerHTML = 'Select an inbound container on the left to open its ship-tracker detail.';
    return;
  }
  if(selected.kind === 'incoming'){
    var m = null;
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) m = x; });
    if(!m){ $('inspector').innerHTML = 'Container no longer in the pipeline.'; return; }
    $('inspector').innerHTML = inspectorIncoming(m); ensureBayplan(m.container_id);
  } else {
    var o = null;
    state.outbound.forEach(function(x){ if(x.container_id === selected.id) o = x; });
    if(!o){ $('inspector').innerHTML = 'Container no longer in the schedule.'; return; }
    $('inspector').innerHTML = inspectorOutbound(o); ensureBayplan(o.container_id);
  }
}

// What PSCH does with this container, per its service flow.
function flowPanel(m){
  var d = m.destination || '—';
  if(m.flow === 'lcl'){
    return '<fieldset><legend>Distribution — LCL land delivery</legend><table class="grid" width="100%"><tbody>' +
      row('Destination', '<b>' + d + '</b> (by land)') +
      row('Delivery unit', m.consolidation_group || '—') +
      row('Cargoes deconsolidated', m.cargoes) +
      row('Release lane', m.release_lane) +
      '</tbody></table></fieldset>';
  }
  if(m.flow === 'fcl'){
    return '<fieldset><legend>Distribution — FCL land release</legend><table class="grid" width="100%"><tbody>' +
      row('Destination', '<b>' + d + '</b> (by land)') +
      row('Handling', 'Whole container — no deconsolidation') +
      row('Staged at', m.bin_location) +
      row('Released via', m.release_lane + ' at ' + isoDT(m.pallet_pick_time)) +
      row('Cargoes in box', m.cargoes) +
      '</tbody></table></fieldset>';
  }
  if(m.flow === 'topup'){
    return '<fieldset><legend>Top Up — re-consolidation</legend><table class="grid" width="100%"><tbody>' +
      row('Destination', '<b>' + d + '</b> (by land)') +
      row('Re-consolidation bay', m.bin_location) +
      row('Seal / release', m.release_lane + ' at ' + isoDT(m.pallet_pick_time)) +
      row('Cargo added', m.cargoes + ' cargo units consolidated in') +
      '</tbody></table></fieldset>';
  }
  if(m.flow === 'transload'){
    return '<fieldset><legend>Transloading — container-to-container</legend><table class="grid" width="100%"><tbody>' +
      row('Transfer bay', m.bin_location) +
      row('Target container', m.consolidation_group || '—') +
      row('Transfer window', isoTime(m.staging_start) + '–' + isoTime(m.move_end) + 'Z') +
      row('Cargo moved', m.cargoes + ' cargo units') +
      '</tbody></table></fieldset>';
  }
  return '';
}

function journeyTimeline(m){
  var idx = STAGES.indexOf(m.status);
  var stages = [
    {label:'En Route (Sea)', time:m.sea_arrival},
    {label:'Unloaded', time:m.depot_arrive},
    {label:'Depot', time:m.road_depart},
    {label:'En Route (Road)', time:m.psch_receipt_eta},
    {label:'Arrived', time:m.psch_receipt_eta}
  ];
  var html = '<table class="journey">';
  stages.forEach(function(s, i){
    var cls = i < idx ? 'stage-done' : (i === idx ? 'stage-now' : 'stage-wait');
    var mark = i < idx ? '&#10003;' : (i === idx ? '&#9679;' : '&#9675;');
    html += '<tr class="' + cls + '"><td>' + mark + ' ' + s.label + '</td><td align="right">' + isoDT(s.time) + '</td></tr>';
  });
  return html + '</table>';
}

// Bay plans are served on demand (/api/bayplan) and cached per container id,
// so the 8s state poll never carries the heavy stowage data.
var bayplanCache = {};
function ensureBayplan(cid){
  if(!cid) return;
  if(bayplanCache.hasOwnProperty(cid)){
    // The 8s poll rebuilds the inspector HTML, which resets a shown plan back
    // to its "loading stowage plan…" placeholder — re-apply the cached plan so
    // it stays visible instead of flickering away on the next poll.
    var el0 = $('bayplan-' + cid);
    if(el0 && bayplanCache[cid]) el0.innerHTML = bayplanCache[cid];
    return;
  }
  bayplanCache[cid] = null; // mark as requested; re-applied on arrival / next poll
  api('/api/bayplan', {container_id: cid}).then(function(resp){
    bayplanCache[cid] = (resp && resp.html) || '';
    var el = $('bayplan-' + cid);
    if(el && bayplanCache[cid]) el.innerHTML = bayplanCache[cid];
  }).catch(function(){ bayplanCache[cid] = ''; });
}

function inspectorIncoming(m, withReasoning){
  var docked = (m.vessel_status === 'docked');
  var berthTxt = m.berth_id ? 'Berth ' + m.berth_id + (docked ? ' (alongside)' : ' (planned)') : '—';
  var posTxt = docked ? 'Alongside (0 nm)' : (m.distance_nm + ' nm from Tuas');
  var speedTxt = docked ? 'Moored' : (m.speed_knots + ' kn');
  var html =
    '<table width="100%"><tr><td><b>' + m.container_id + '</b></td><td align="right">' + badge(m.status) + '</td></tr></table>' +
    '<fieldset><legend>Vessel details</legend><table class="grid" width="100%"><tbody>' +
    row('Vessel', '<b>' + m.vessel_name + '</b> (' + m.vessel_id + ')') +
    row('Berth at Tuas', berthTxt) +
    row('Distance from Tuas', posTxt) +
    row('Speed Over Ground', speedTxt) +
    row('Vessel ETA at berth', isoDT(m.sea_arrival)) +
    row('Vessel next port', m.destination || '—') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Stowage Plan</legend>' +
    '<div id="bayplan-' + m.container_id + '" style="min-height:20px;font-size:9px;color:#606060">' +
    'loading stowage plan…</div>' +
    '</fieldset>' +
    '<fieldset><legend>Journey to PSCH</legend>' + journeyTimeline(m) +
    '<table class="grid" width="100%"><tbody>' +
    row('ETA at PSCH doorstep', '<b>' + isoDT(m.psch_receipt_eta) + '</b> (' + countdownHtml(m.hours_until) + ')') +
    '</tbody></table></fieldset>';
  html += flowPanel(m);
  if(m.consolidation_group){
    html += '<fieldset><legend>Agent PSCH plan (ready before arrival)</legend><table class="grid" width="100%"><tbody>' +
      row('Receiving area', m.receiving_area) +
      row('Staging wait', isoTime(m.staging_start) + '–' + isoTime(m.staging_end) + 'Z') +
      row('Move to bin', isoTime(m.move_start) + '–' + isoTime(m.move_end) + 'Z') +
      row('AS/RS putaway bin', '<b>' + m.bin_location + '</b> · ' + m.putaway_robot) +
      row('Pallet pick time', isoDT(m.pallet_pick_time)) +
      row('Release lane', m.release_lane + ' (for this container number)') +
      row('Consolidation group', m.consolidation_group) +
      '</tbody></table></fieldset>';
  }
  if(withReasoning !== false){
    html += '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + reasoningHtml(m.reasoning) + '</div></fieldset>';
  }
  return html;
}

function inspectorOutbound(o, withReasoning){
  // Headers and row labels follow the same conventions as the PSCH Inbound
  // inspector (Vessel details / Stowage Plan / ...) so the two pages read
  // consistently.
  var html =
    '<table width="100%"><tr><td><b>' + o.container_id + '</b></td><td align="right">' + badge(o.status) + '</td></tr></table>' +
    '<fieldset><legend>Vessel details</legend><table class="grid" width="100%"><tbody>' +
    row('Vessel', '<b>' + o.bound_vessel_name + '</b> (' + o.bound_vessel_id + ')') +
    row('Berth at Tuas', o.berth_id ? 'Berth ' + o.berth_id + ' (highlighted on map)' : '—') +
    row('Vessel leaves port (ETD)', isoDT(o.vessel_etd)) +
    row('Destination', o.destination) +
    row('Equipment size/type', (o.size || '40HC') + ' · GP') +
    row('Loading cell on vessel', '<b>' + (o.stow_position || '—') + '</b>') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Consolidation &amp; release plan</legend><table class="grid" width="100%"><tbody>' +
    row('Stuffing / pallet-pick window', isoDT(o.stuffing_start) + ' → ' + isoDT(o.stuffing_end)) +
    row('Loading lane released', o.loading_lane + ' at ' + isoDT(o.lane_release_time)) +
    row('ETA to arrive at loading area', '<b>' + isoDT(o.eta_loading_area) + '</b> (' +
        ((o.status === 'loaded') ? 'loaded' : countdownHtml(o.hours_to_loading)) + ')') +
    row('Staging lanes (PSCH releasing)', o.staging_lane_start
        ? o.staging_lane_start +
          (o.staging_lane_end && o.staging_lane_end !== o.staging_lane_start
            ? '–' + o.staging_lane_end : '') +
          ' · ' + o.source_shipments.length + ' pallets staged'
        : '—') +
    row('Staged cargoes on these lanes', o.source_containers.join(', ')) +
    row('Pallet locations (aisles)', pschAislesOfOutbound(o.container_id).join(', ') || '—') +
    row('Pallets routed', o.source_shipments.length) +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Stowage Plan</legend>' +
    '<div style="margin-bottom:2px">Bay ' + (o.bay_label || '—') + ' · Row ' + pad2(o.stow_row) + ' · Tier ' + pad2(o.stow_tier) + '</div>' +
    '<div id="bayplan-' + o.container_id + '" style="min-height:20px;font-size:9px;color:#606060">' +
    'loading stowage plan…</div>' +
    '</fieldset>';
  if(withReasoning !== false){
    html += '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + reasoningHtml(o.reasoning) + '</div></fieldset>';
  }
  return html;
}

// ---- Distribution (local LCL delivery · FCL/LCL land release) ----------------
function distItems(){
  var items = [];
  state.distribution.forEach(function(d){
    items.push({kind: d.kind, id: d.container_id, container_id: d.container_id,
                destination: d.destination, status: d.status, data: d,
                eta: d.eta_destination, pallets: d.pallets,
                source_containers: d.source_containers});
  });
  return items;
}

function renderDist(){
  renderDistKpis();
  renderDistChips();
  renderDistList();
  renderDistBoard();
  renderDistInspector();
}

function renderDistKpis(){
  var land = state.distribution;
  var by = {staged:0, released:0, in_transit:0, delivered:0};
  land.forEach(function(d){ by[d.status] = (by[d.status] || 0) + 1; });
  $('distKpis').innerHTML = kpiStripHtml([
    ['Land releases', land.length + ' (LCL ' + land.filter(function(d){return d.kind==='LCL delivery';}).length +
      ' · FCL ' + land.filter(function(d){return d.kind==='FCL land release';}).length + ')'],
    ['In transit (land)', by.in_transit],
    ['Delivered', by.delivered],
    ['Pending build', by.staged],
    ['Land destinations', Object.keys(land.reduce(function(a,d){ a[d.destination]=1; return a; }, {})).length],
    ['24/7 gate flow', 'continuous']
  ]);
}

function renderDistChips(){
  renderChips('distChips', distFilter, [
    ['all', 'All'], ['LCL delivery', 'LCL delivery'], ['FCL land release', 'FCL land release']
  ], 'setDistFilter');
}
function setDistFilter(f){ distFilter = f; renderDistChips(); renderDistList(); renderDistBoard(); if(combos.dist) combos.dist.open(); }

function pickDist(kind, id){
  if(distSel && distSel.kind === kind && distSel.id === id){
    distSel = null;
  } else {
    distSel = {kind: kind, id: id};
  }
  setDetail(!!distSel);
  renderDistList();
  renderDistBoard();
  renderDistInspector();
}

function renderDistList(){
  var q = distQuery.toLowerCase();
  var items = distItems().filter(function(d){
    if(distFilter !== 'all' && d.kind !== distFilter) return false;
    return q === '' ||
      (d.id + ' ' + d.destination + ' ' + d.status + ' ' + d.kind).toLowerCase().indexOf(q) !== -1;
  });
  var html = '';
  items.forEach(function(d){
    var cls = (distSel && distSel.kind === d.kind && distSel.id === d.id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (d.status === 'delivered' || d.status === 'loaded') ? d.status : countdownHtml(hoursUntil(d.eta));
    html += '<button type="button" class="' + cls + '" onclick="pickDist(\'' + d.kind + '\',\'' + d.id + '\')">' +
      '<span class="cid">' + d.id + '</span> &nbsp;<span class="flow-pill ' + (d.kind === 'LCL delivery' ? 'lcl' : 'fcl') + '">' + d.kind + '</span><br>' +
      badge(d.status) + ' &nbsp;→ <b>' + d.destination + '</b><br>' +
      'ETA destination ' + isoDT(d.eta) + ' (' + etaTxt + ') · ' + d.pallets + ' pallets · ' + d.source_containers.length + ' source cargoes' +
      '</button>';
  });
  if(!items.length) html = '<div class="no-match">No releases match the filter or search.</div>';
  html += '<div class="drop-foot">' + items.length + ' of ' + state.distribution.length + ' land releases</div>';
  $('distList').innerHTML = html;
  $('distSelInfo').innerHTML = distSel
    ? 'Selected: <b>' + distSel.id + '</b> ' + badge(itemStatus(distSel))
    : 'Select a release to inspect its cargo grouping, destination and timing.';
}
function itemStatus(sel){
  var items = distItems();
  for(var i=0;i<items.length;i++){ if(items[i].kind === sel.kind && items[i].id === sel.id) return items[i].status; }
  return '';
}
function hoursUntil(iso){
  if(!iso) return null;
  var t = new Date(iso).getTime() - new Date(state.sim_now).getTime();
  return t / 3600000;
}

function renderDistBoard(){
  var land = state.distribution;
  var byDest = {};
  land.forEach(function(d){
    (byDest[d.destination] = byDest[d.destination] || []).push(d);
  });
  var html = '';
  Object.keys(byDest).sort().forEach(function(dest){
    var units = byDest[dest];
    html += '<div class="board-box land"><div class="bb-head">TO ' + dest + ' (by land) — ' + units.length + ' release(s)</div>';
    units.forEach(function(d){
      var cls = 'unit-chip' + (distSel && distSel.kind === d.kind && distSel.id === d.container_id ? ' sel' : '') +
        (d.status === 'delivered' ? ' done' : (d.status === 'in_transit' ? ' transit' : ''));
      html += '<span class="' + cls + '" onclick="pickDist(\'' + d.kind + '\',\'' + d.container_id + '\')">' +
        d.container_id + ' <span class="u-status">' + d.status + '</span> · ETA ' + isoDT(d.eta_destination) + '</span>';
    });
    html += '</div>';
  });
  html = '<div style="font-size:9px;color:#404040;margin-bottom:4px">Central &amp; regional distribution: LCL deliveries and FCL land releases leave PSCH by road (Tuas → local Singapore areas or Johor / Malaysia). Vessel-bound MCC consolidation containers live on the MCC page under Outbound. Click a chip for the full release detail.</div>' + html;
  $('distBoard').innerHTML = html;
}

function renderDistInspector(){
  if(!distSel){ $('distInspector').innerHTML = 'Select a release on the left to open its detail.'; return; }
  var d = null;
  state.distribution.forEach(function(x){ if(x.container_id === distSel.id) d = x; });
  if(!d){ $('distInspector').innerHTML = 'Release no longer in the schedule.'; return; }
  $('distInspector').innerHTML =
    '<table width="100%"><tr><td><b>' + d.container_id + '</b></td><td align="right">' + badge(d.status) + '</td></tr></table>' +
    '<fieldset><legend>' + d.kind + '</legend><table class="grid" width="100%"><tbody>' +
    row('Destination', '<b>' + d.destination + '</b> (by land)') +
    row('Cargo grouped into this release', d.source_containers.join(', ')) +
    row('Pallets routed', d.pallets) +
    row('Build window', isoDT(d.build_start) + ' → ' + isoDT(d.release_time)) +
    row('Released (leaves PSCH)', isoDT(d.release_time)) +
    row('On the road', isoDT(d.road_depart)) +
    row('ETA at destination', '<b>' + isoDT(d.eta_destination) + '</b>') +
    row('Truck tracking', 'destination + ETA only (port↔PSCH is the focus, not road tracking)') +
    '</tbody></table></fieldset>';
}

// ---- MCC Outbound (back-to-port consolidation tracker) -----------------------
function renderMcc(){
  renderMccKpis();
  renderMccList();
  renderMccMap();
  renderMccInspector();
}

function renderMccKpis(){
  var port = state.outbound;
  var by = {staged:0, released:0, in_transit:0, loaded:0};
  port.forEach(function(o){ by[o.status] = (by[o.status] || 0) + 1; });
  var vessels = {};
  port.forEach(function(o){ vessels[o.bound_vessel_id] = 1; });
  var next = null;
  port.forEach(function(o){
    if(o.status === 'loaded' || !o.hours_to_loading) return;
    if(next === null || o.hours_to_loading < next) next = o.hours_to_loading;
  });
  $('mccKpis').innerHTML = kpiStripHtml([
    ['MCC consolidation (back to port)', port.length],
    ['Staged @ PSCH', by.staged],
    ['Released / in transit', by.released + by.in_transit],
    ['Loaded on vessel', by.loaded],
    ['Bound vessels', Object.keys(vessels).length],
    ['Next loading', next === null ? '—' : countdownHtml(next)]
  ]);
}

function pickMcc(cid){
  mccSel = (mccSel === cid) ? null : cid;
  setDetail(!!mccSel);
  renderMccList();
  renderMccMap();
  renderMccInspector();
}

function renderMccList(){
  var items = state.outbound;
  var html = '';
  items.forEach(function(o){
    var cls = (mccSel === o.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (o.status === 'loaded') ? o.status : countdownHtml(o.hours_to_loading);
    html += '<button type="button" class="' + cls + '" onclick="pickMcc(\'' + o.container_id + '\')">' +
      '<span class="cid">' + o.container_id + '</span> &nbsp;<span class="flow-pill mcc">MCC</span><br>' +
      badge(o.status) + ' &nbsp;→ <b>' + o.destination + '</b><br>' +
      'Bound: <b>' + o.bound_vessel_name + '</b> · vessel ETD ' + isoDT(o.vessel_etd) + ' · ETA loading ' + isoDT(o.eta_loading_area) + ' (' + etaTxt + ')' +
      '</button>';
  });
  if(!items.length) html = '<div class="no-match">No consolidation containers yet — run the planner.</div>';
  html += '<div class="drop-foot">' + items.length + ' consolidation containers</div>';
  $('mccList').innerHTML = html;
  var st = '';
  if(mccSel){ state.outbound.forEach(function(o){ if(o.container_id === mccSel) st = o.status; }); }
  $('mccSelInfo').innerHTML = mccSel
    ? 'Selected: <b>' + mccSel + '</b> ' + badge(st)
    : 'Select a container to open its vessel-bound detail — berth, ETD, loading cell.';
}

function renderMccInspector(){
  if(!mccSel){ $('mccInspector').innerHTML = 'Select a consolidation container on the left to open its vessel-bound detail.'; return; }
  var o = null;
  state.outbound.forEach(function(x){ if(x.container_id === mccSel) o = x; });
  if(!o){ $('mccInspector').innerHTML = 'Container no longer in the schedule.'; return; }
  $('mccInspector').innerHTML = inspectorOutbound(o); ensureBayplan(o.container_id);
}

// ---- Top Up (container re-consolidation / topping up) ------------------------
function renderTop(){
  renderTopKpis();
  renderTopChips();
  renderTopList();
  renderTopBoard();
  renderTopInspector();
}

function renderTopKpis(){
  var jobs = state.top_up;
  var by = {pending:0, in_progress:0, done:0};
  jobs.forEach(function(j){ by[j.status] = (by[j.status] || 0) + 1; });
  var added = jobs.reduce(function(a, j){ return a + j.pallets_added; }, 0);
  $('topKpis').innerHTML = kpiStripHtml([
    ['Re-consolidation jobs', jobs.length],
    ['Pending', by.pending], ['In progress', by.in_progress], ['Done', by.done],
    ['Cargo units consolidated', added],
    ['Bays in use (10)', Math.min(10, jobs.filter(function(j){ return j.status !== 'done'; }).length)]
  ]);
}

function renderTopChips(){
  renderChips('topChips', topFilter, [
    ['all', 'All'], ['pending', 'Pending'], ['in_progress', 'In progress'], ['done', 'Done']
  ], 'setTopFilter');
}
function setTopFilter(f){ topFilter = f; renderTopChips(); renderTopList(); if(combos.top) combos.top.open(); }

function pickTop(id){
  if(topSel === id){ topSel = null; } else { topSel = id; }
  setDetail(!!topSel);
  renderTopList();
  renderTopBoard();
  renderTopInspector();
}

function renderTopList(){
  var q = topQuery.toLowerCase();
  var jobs = state.top_up.filter(function(j){
    if(topFilter !== 'all' && j.status !== topFilter) return false;
    return q === '' ||
      (j.job_id + ' ' + (j.container_id || '') + ' ' + (j.destination || '') + ' ' + j.status).toLowerCase().indexOf(q) !== -1;
  });
  var html = '';
  jobs.forEach(function(j){
    var cls = (topSel === j.job_id) ? 'mp-item sel' : 'mp-item';
    html += '<button type="button" class="' + cls + '" onclick="pickTop(\'' + j.job_id + '\')">' +
      '<span class="cid">' + j.job_id + '</span> &nbsp;' + flowPill('topup') + '<br>' +
      badge(j.status) + ' &nbsp;container <b>' + j.container_id + '</b><br>' +
      '→ <b>' + j.destination + '</b> · window ' + isoDT(j.window_start) + '–' + isoDT(j.window_end) +
      '</button>';
  });
  if(!jobs.length) html = '<div class="no-match">No re-consolidation jobs match the filter or search.</div>';
  $('topList').innerHTML = html + '<div class="drop-foot">' + jobs.length + ' of ' + state.top_up.length + ' top-up jobs</div>';
  $('topSelInfo').innerHTML = topSel ? 'Selected: <b>' + topSel + '</b>' : 'Select a job to open its re-consolidation detail.';
}

function renderTopBoard(){
  var jobs = state.top_up;
  var statuses = {pending:0, in_progress:0, done:0};
  jobs.forEach(function(j){ statuses[j.status] = (statuses[j.status] || 0) + 1; });
  var html = '<div class="wf-strip">' +
    '<span class="wf-step">RECEIVE</span>' +
    '<span class="wf-step">STAGING</span>' +
    '<span class="wf-step now">TOP-UP BAY</span>' +
    '<span class="wf-step">SEAL</span>' +
    '<span class="wf-step">RELEASE</span></div>' +
    '<div class="board-box"><div class="bb-head">Top-up bays — one job per bay (10 bays, 24/7)</div>';
  var bays = {};
  jobs.forEach(function(j){ (bays[j.bay] = bays[j.bay] || []).push(j); });
  for(var i=1;i<=10;i++){
    var key = 'Top-up Bay ' + i;
    var list = bays[key] || [];
    html += '<div style="margin:2px 0"><b style="font-size:9px">' + key + '</b>: ';
    if(!list.length){ html += '<span style="color:#909090;font-size:9px">idle</span>'; }
    list.forEach(function(j){
      var cls = 'unit-chip' + (topSel === j.job_id ? ' sel' : '') +
        (j.status === 'done' ? ' done' : (j.status === 'in_progress' ? ' transit' : ''));
      html += '<span class="' + cls + '" onclick="pickTop(\'' + j.job_id + '\')">' + j.job_id + ' <span class="u-status">' + j.status + '</span></span>';
    });
    html += '</div>';
  }
  html += '</div>';
  html += '<div style="font-size:9px;color:#404040">Re-consolidation (top up): a partially filled container is received, staged, additional cargo consolidated in at the bay, sealed, and released. Pending ' + statuses.pending + ' · in progress ' + statuses.in_progress + ' · done ' + statuses.done + '. Workflow is planned separately per job and pre-wired for the AI agent.</div>';
  $('topBoard').innerHTML = html;
}

function renderTopInspector(){
  if(!topSel){ $('topInspector').innerHTML = 'Select a job on the left to open its detail.'; return; }
  var j = null;
  state.top_up.forEach(function(x){ if(x.job_id === topSel) j = x; });
  if(!j){ $('topInspector').innerHTML = 'Job no longer in the schedule.'; return; }
  $('topInspector').innerHTML =
    '<table width="100%"><tr><td><b>' + j.job_id + '</b></td><td align="right">' + badge(j.status) + '</td></tr></table>' +
    '<fieldset><legend>Re-consolidation job</legend><table class="grid" width="100%"><tbody>' +
    row('Container', j.container_id) +
    row('Destination', '<b>' + j.destination + '</b> (by land)') +
    row('Bay', j.bay) +
    row('Re-consolidation window', isoDT(j.window_start) + ' → ' + isoDT(j.window_end)) +
    row('Cargo units consolidated in', j.pallets_added) +
    row('Sealed', isoDT(j.seal_time)) +
    row('Release ETA', '<b>' + isoDT(j.release_eta) + '</b>') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + reasoningHtml(j.reasoning) + '</div></fieldset>';
}

// ---- Quality Control (survey · sampling · repack · rework) -------------------
function renderQc(){
  renderQcKpis();
  renderQcChips();
  renderQcList();
  renderQcBoard();
  renderQcInspector();
}

function renderQcKpis(){
  var tasks = state.qc;
  var byKind = {survey:0, sampling:0, repack:0, rework:0};
  var byStatus = {pending:0, in_progress:0, done:0};
  tasks.forEach(function(t){ byKind[t.kind] = (byKind[t.kind] || 0) + 1; byStatus[t.status] = (byStatus[t.status] || 0) + 1; });
  $('qcKpis').innerHTML = kpiStripHtml([
    ['QC tasks', tasks.length],
    ['Survey', byKind.survey], ['Sampling', byKind.sampling],
    ['Repack', byKind.repack], ['Rework', byKind.rework],
    ['In progress', byStatus.in_progress], ['Done', byStatus.done]
  ]);
}

function renderQcChips(){
  renderChips('qcChips', qcFilter, [
    ['all', 'All'], ['survey', 'Survey'], ['sampling', 'Sampling'],
    ['repack', 'Repack'], ['rework', 'Rework']
  ], 'setQcFilter');
}
function setQcFilter(f){ qcFilter = f; renderQcChips(); renderQcList(); if(combos.qc) combos.qc.open(); }

function pickQc(id){
  if(qcSel === id){ qcSel = null; } else { qcSel = id; }
  setDetail(!!qcSel);
  renderQcList();
  renderQcBoard();
  renderQcInspector();
}

function qcKindPill(k){
  return '<span class="qc-kind ' + k + '">' + k.toUpperCase() + '</span>';
}

function renderQcList(){
  var tasks = state.qc.filter(function(t){ return qcFilter === 'all' || t.kind === qcFilter; });
  var html = '';
  tasks.forEach(function(t){
    var cls = (qcSel === t.task_id) ? 'mp-item sel' : 'mp-item';
    html += '<button type="button" class="' + cls + '" onclick="pickQc(\'' + t.task_id + '\')">' +
      '<span class="cid">' + t.task_id + '</span> &nbsp;' + qcKindPill(t.kind) + ' &nbsp;' + flowPill(t.flow) + '<br>' +
      badge(t.status) + ' &nbsp;container <b>' + t.container_id + '</b><br>' +
      t.station + ' · ' + isoDT(t.window_start) + '–' + isoDT(t.window_end) +
      '</button>';
  });
  if(!tasks.length) html = '<div class="no-match">No QC tasks match this filter.</div>';
  $('qcList').innerHTML = html + '<div class="drop-foot">' + tasks.length + ' of ' + state.qc.length + ' QC tasks</div>';
  $('qcSelInfo').innerHTML = qcSel ? 'Selected: <b>' + qcSel + '</b>' : 'Select a task to open its QC detail.';
}

function renderQcBoard(){
  var tasks = state.qc;
  var kinds = [
    ['survey', 'SURVEY — cargo & container condition, damage / shortfall noted'],
    ['sampling', 'SAMPLING — quality & customs verification, samples logged'],
    ['repack', 'REPACK — damaged / loose cartons into fresh palletised units'],
    ['rework', 'REWORK — mislabelled / mis-sorted cargo corrected']
  ];
  var html = '';
  kinds.forEach(function(k){
    var list = tasks.filter(function(t){ return t.kind === k[0]; });
    html += '<div class="board-box"><div class="bb-head">' + k[1] + ' — ' + list.length + ' task(s)</div>';
    list.forEach(function(t){
      var cls = 'unit-chip' + (qcSel === t.task_id ? ' sel' : '') +
        (t.status === 'done' ? ' done' : (t.status === 'in_progress' ? ' transit' : ''));
      html += '<span class="' + cls + '" onclick="pickQc(\'' + t.task_id + '\')">' + t.task_id + ' · ' + t.container_id + ' <span class="u-status">' + t.status + '</span></span>';
    });
    html += '</div>';
  });
  html = '<div style="font-size:9px;color:#404040;margin-bottom:4px">Cargo survey, sampling, repack and rework run continuously at the QC bays (24/7) before putaway or release. Click a task chip for its detail.</div>' + html;
  $('qcBoard').innerHTML = html;
}

function renderQcInspector(){
  if(!qcSel){ $('qcInspector').innerHTML = 'Select a task on the left to open its QC detail.'; return; }
  var t = null;
  state.qc.forEach(function(x){ if(x.task_id === qcSel) t = x; });
  if(!t){ $('qcInspector').innerHTML = 'Task no longer in the schedule.'; return; }
  $('qcInspector').innerHTML =
    '<table width="100%"><tr><td><b>' + t.task_id + '</b></td><td align="right">' + badge(t.status) + '</td></tr></table>' +
    '<fieldset><legend>' + t.kind.toUpperCase() + ' task</legend><table class="grid" width="100%"><tbody>' +
    row('Task type', qcKindPill(t.kind)) +
    row('Container', t.container_id) +
    row('Flow', flowPill(t.flow)) +
    row('Destination', t.destination) +
    row('Station', t.station) +
    row('Window', isoDT(t.window_start) + ' → ' + isoDT(t.window_end)) +
    row('Cargo units', t.cargoes) +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Scope</legend><div style="padding:2px">' + t.note + '</div></fieldset>';
}

// ---- PSCH Space --------------------------------------------------------------
// PSCH is a two-room CFS (AMBIENT + COLD ROOM) laid out Receiving -> Storage ->
// Dispatch with a one-way flow, racks/bins named AISLE-LEVEL-BAY like a
// distribution centre, and staging lanes at the inbound (receiving) / outbound
// (releasing) areas. Selecting a rack drills into its bin grid; selecting a container
// (from the list, a rack, or a bin) highlights its planned bin and opens the
// ship-tracker detail.
var pschSelRack = null; // aisle id, e.g. "1"
var pschSelBin = null;  // bin id, e.g. "1-12-2A"

function pschRoomOfAisle(a){
  for(var i=0;i<state.psch.rooms.length;i++){
    var r = state.psch.rooms[i];
    for(var j=0;j<r.aisles.length;j++){ if(r.aisles[j].id === a) return r.id; }
  }
  return 'ambient';
}
function pschAisleOfBin(bid){ return (bid || '').split('-')[0]; }
function pschBinIdOf(bl){ return (bl || '').replace(/^Bin /,''); }
function pschStatusShort(s){
  return ({'En Route (Sea)':'SEA','Unloaded':'UNLD','Depot':'DEP','En Route (Road)':'ROAD','Arrived':'ARR',
           'staged':'STG','released':'REL','in_transit':'TRANSIT','loaded':'LOADED'}[s] || s || '');
}
function pschCidBin(cid){
  for(var i=0;i<state.inbound.length;i++){
    if(state.inbound[i].container_id === cid) return pschBinIdOf(state.inbound[i].bin_location);
  }
  return null;
}
function pschAislesOfOutbound(cid){
  // The aisles holding the pallets staged for an outbound consolidation
  // container (its source cargo can sit across several aisles).
  var aisles = {};
  state.outbound.forEach(function(o){
    if(o.container_id === cid) (o.source_containers || []).forEach(function(sc){
      var bid = pschCidBin(sc);
      if(bid) aisles[pschAisleOfBin(bid)] = 1;
    });
  });
  return Object.keys(aisles).sort();
}

function renderPsch(){
  // Re-renders rebuild the bin-grid / lane DOM, which would reset any
  // horizontal scroll the user was looking at; keep the scroll positions so
  // the contents never jump sideways when a bin or container is selected.
  var gridWrap = document.querySelector('#pschRackDetail .psch-grid-wrap');
  var gridScroll = gridWrap ? gridWrap.scrollLeft : 0;
  var relWrap = document.querySelector('#pschRelLanes .psch-rel-grid');
  var relScroll = relWrap ? relWrap.scrollLeft : 0;
  var rcvWrap = document.querySelector('#pschRcvLanes .psch-rel-grid');
  var rcvScroll = rcvWrap ? rcvWrap.scrollLeft : 0;
  var incScroll = $('pschIncoming') ? $('pschIncoming').scrollTop : 0;
  renderPschKpis();
  renderPschFlow();
  renderPschAsrs();
  renderPschRcvLanes();
  renderPschOutbound();
  renderPschIncoming();
  renderPschRackDetail();
  renderPschInspector();
  renderPschReasoning();
  var gw = document.querySelector('#pschRackDetail .psch-grid-wrap');
  if(gw) gw.scrollLeft = gridScroll;
  var rw = document.querySelector('#pschRelLanes .psch-rel-grid');
  if(rw) rw.scrollLeft = relScroll;
  var cw = document.querySelector('#pschRcvLanes .psch-rel-grid');
  if(cw) cw.scrollLeft = rcvScroll;
  if($('pschIncoming')) $('pschIncoming').scrollTop = incScroll;
}

function renderPschFlow(){
  // Numbered process strip: the plain-English "how cargo moves" narrative
  // with live counts, so a new user can follow the flow without reading.
  var a = state.asrs || {};
  var steps = [
    ['in', 'INBOUND', 'Containers arrive at PSCH'],
    ['rcv', 'RECEIVING', 'Unloaded on a conveyer'],
    ['pa', 'PUTAWAY', (a.stackers || []).length + ' AS/RS Stackers put away pallets into bin locations'],
    ['store', 'STORAGE', 'Pallets waiting for consolidation'],
    ['pick', 'PICK', 'Pallets picked to leave for Outbound'],
    ['rel', 'RELEASING', 'Staging at Releasing lanes to prepare for loading into containers'],
    ['out', 'OUTBOUND', 'Loaded and leaves PSCH']
  ];
  $('pschFlow').innerHTML = steps.map(function(st, i){
    return '<div class="proc-step s-' + st[0] + '"><span class="step-n">' + (i + 1) + '</span>' +
      '<span class="proc-name">' + st[1] + '</span><div class="proc-sub">' + st[2] + '</div></div>';
  }).join('<div class="proc-arrow">&#10230;</div>');
}

function renderPschRcvLanes(){
  // Receiving lanes drawn as horizontal blocks, same visual language as the
  // releasing lanes below, for instant recognition. Each block shows only its
  // lane number and the container numbers being unloaded (or staged) there,
  // one per line, centre-aligned and never wrapped — the row scrolls sideways
  // if it is wider than the pane. A lane can hold several containers at once,
  // so the blocks are a read-only view (not clickable); select any listed
  // container from the left list to open its receiving & putaway plan.
  var lanes = state.psch.lanes.receiving || [];
  var html = lanes.map(function(l){
    var ids = l.containers || [];
    var n = ids.length;
    var title = 'Lane ' + l.lane + ' · ' + n + ' container(s)' + (n ? ' · ' + ids.join(', ') : '');
    var inner = '<div class="psch-rel-id">' + l.lane + '</div>' +
      '<div class="psch-rel-group">' + (ids.slice(0, 5).join('<br>') || 'free') + '</div>' +
      '<div class="psch-rel-pallets">' + (n ? n + ' ctn' : '—') + '</div>';
    return '<div class="psch-rel-lane rcv' + (n ? '' : ' empty') + '" data-lane="rcv-' + l.lane + '" title="' + title + '">' + inner + '</div>';
  }).join('');
  $('pschRcvLanes').innerHTML =
    '<div class="psch-rel-grid">' + html + '</div>' +
    '<div class="psch-rel-legend">Each block is one receiving lane (numbered left to right). ' +
    'The container numbers listed are being unloaded (or staged) there; blue = in use, grey = free. ' +
    'A lane can hold several containers at once, so these blocks are a read-only view — ' +
    'select any container from the <b>Inbound Cargo by Container ID</b> list on the left to open its ' +
    'receiving &amp; putaway plan. The row scrolls sideways if it is wider than the pane. ' +
    'Whole-container flows (FCL / Top Up / Transload) are staged in the yard and bays, not here.</div>';
}

function renderPschRelLanes(){
  var selGroup = (selected && selected.kind === 'outbound') ? selected.id : null;
  var colors = ['rel-c1','rel-c2','rel-c3','rel-c4','rel-c5','rel-c6'];
  var colorBy = {};
  (state.psch.releasing_groups || []).forEach(function(g, i){ colorBy[g.container_id] = colors[i % colors.length]; });
  var html = (state.psch.lanes.releasing || []).map(function(lane){
    var gid = lane.group;
    var cls = 'psch-rel-lane' + (gid ? ' ' + (colorBy[gid] || 'rel-c1') : ' empty');
    if(gid && gid === selGroup) cls += ' sel';
    var title = gid ? lane.lane + ' · ' + gid + ' · ' + lane.pallets + ' pallets staged' : lane.lane + ' · free';
    var inner = gid
      ? '<div class="psch-rel-id">' + lane.lane + '</div>' +
        '<div class="psch-rel-group">' + gid + '</div>' +
        '<div class="psch-rel-pallets">' + lane.pallets + ' pal</div>'
      : '<div class="psch-rel-id">' + lane.lane + '</div>' +
        '<div class="psch-rel-group">free</div>' +
        '<div class="psch-rel-pallets">—</div>';
    return '<div class="' + cls + '" data-rel="' + lane.lane + '" data-group="' + (gid || '') + '" title="' + title + '"' +
      ' onclick="pickPschOutbound(\'' + (gid || '') + '\')">' + inner + '</div>';
  }).join('');
  $('pschRelLanes').innerHTML = html
    ? '<div class="psch-rel-grid">' + html + '</div>'
    : '<div style="padding:2px;font-size:9px;color:#404040">No consolidation groups staged yet — run the planner.</div>';
}

function pickPschOutbound(cid){
  if(!cid || (selected && selected.kind === 'outbound' && selected.id === cid)){
    // clicking a free lane, or the lane group already selected, clears the selection
    selected = null;
    pschSelRack = null;
    pschSelBin = null;
    setDetail(false);
    renderPsch();
    return;
  }
  pschSelRack = null; // lane selection is now the focus; clear rack/bin boxes
  pschSelBin = null;
  selectOutbound(cid);
  renderPsch();
}

function renderPschKpis(){
  var s = state.psch.stats;
  var occ = state.kpis.room_occupancy || {};
  var rows = [
    ['Total rack bins', s.bins_used + ' / ' + s.bins_total + ' (' + s.bin_util + '%)'],
    ['AMBIENT occupancy', (occ.ambient !== undefined ? occ.ambient : '—') + '%'],
    ['COLD ROOM occupancy', (occ.cold_room !== undefined ? occ.cold_room : '—') + '%'],
    ['Pallets planned (putaway)', s.pallets_planned],
    ['Pallets in storage (arrived)', s.pallets_in_storage],
    ['Receiving lanes in use', s.lanes_rcv_used + ' / ' + s.lanes_rcv_total],
    ['Releasing lanes in use', s.lanes_rel_used + ' / ' + s.releasing_lanes],
    ['AS/RS stackers', state.asrs.stackers.length + ' (' + state.asrs.charging + ' charging)'],
    ['Charging station', state.asrs.charging + ' of ' + state.asrs.charging_bays + ' bays']
  ];
  $('pschKpis').innerHTML = kpiStripHtml(rows);
}

function fmtMins(m){
  if(m == null) return '—';
  m = Math.round(m);
  var h = Math.floor(m / 60), r = m % 60;
  return h > 0 ? (h + 'h ' + (r < 10 ? '0' : '') + r + 'm') : (m + 'm');
}

function renderPschAsrs(){
  // Charging detail lives in its own collapsed FYI container at the bottom of
  // the page — useful background, not something the operator needs to watch.
  var a = state.asrs || {stackers: [], charging: 0, charging_bays: 2, park_threshold: 45, note: ''};
  var rows = a.stackers.map(function(s){
    var next = (s.status === 'charging')
      ? 'resumes in <b>' + fmtMins(s.mins_to_ready) + '</b> · ' + isoDT(s.ready)
      : 'parks for charging in <b>' + fmtMins(s.mins_to_park) + '</b> · ' + isoDT(s.ready);
    return '<tr class="asrs-row-' + s.status + '" data-sid="' + s.id + '"><td><b>' + s.id + '</b></td><td>' + s.charge_pct +
      '%</td><td>' + s.status + '</td><td>' + next + '</td></tr>';
  }).join('');
  var chargingNow = a.stackers.filter(function(s){ return s.status === 'charging'; })
    .map(function(s){ return s.id; }).join(', ') || 'none';
  $('asrsDetail').innerHTML =
    '<div class="asrs-note">' + a.note + '. While working, charge falls roughly 8%/hour; at ' + a.park_threshold +
    '% the stacker parks itself at the charging station (' + a.charging_bays + ' bays) and tops back up at about 30%/hour before resuming putaway. Everything here is automatic — no operator action needed.</div>' +
    '<table class="asrs-table"><tr><th>Stacker</th><th>Charge</th><th>Status</th><th>Next event</th></tr>' +
    rows + '</table>' +
    '<div class="asrs-now">Now charging: ' + chargingNow + ' · ' + a.charging + ' of ' + a.charging_bays + ' charging bays in use.</div>';
}

function pschRackHtml(a, roomId, selAisle, hlAisle, hlOutAisles){
  var isHaz = (roomId === 'ambient' && a.id === state.psch.hazmat_aisle);
  var cls = 'psch-rack' + (roomId === 'cold_room' ? ' cold' : '') + (isHaz ? ' hazmat' : '');
  if(a.id === selAisle) cls += ' sel';
  if(a.id === hlAisle) cls += ' hl';
  if(hlOutAisles && hlOutAisles.indexOf(a.id) >= 0) cls += ' hl-out';
  var tag = isHaz ? '<span class="psch-rack-tag">DG</span>'
                  : (roomId === 'cold_room' ? '<span class="psch-rack-tag">❄</span>' : '');
  var w = Math.max(2, Math.round(a.pct || 0));
  return '<div class="' + cls + '" onclick="selectPschRack(\'' + a.id + '\')" ' +
    'title="Aisle ' + a.id + ' · ' + a.used + '/' + a.cap + ' bins · ' + a.pct + '% used">' +
    '<div class="psch-rack-id">Aisle ' + a.id + tag + '</div>' +
    '<div class="psch-rack-meta">' + a.used + '/' + a.cap + ' bins · ' + a.pct + '%</div>' +
    '<div class="psch-occ-bar"><i style="width:' + w + '%"></i></div></div>';
}

function pschRoomHtml(room, selAisle, hlAisle, hlOutAisles){
  var cold = room.id === 'cold_room' ? ' cold' : '';
  // The room is now purely a storage area: header names it in plain words
  // (temperature + occupancy), the body is just its rack grid. Receiving and
  // releasing happen in the dedicated lane blocks above, so there is no
  // duplicated RECEIVING/RELEASING zone inside each room.
  var head = '<div class="psch-room-head' + cold + '">' + room.label + ' — ' + room.temp + ' · ' +
    room.used + '/' + room.cap + ' bins (' + room.pct + '% occupied)</div>';
  var racks = room.aisles.map(function(a){ return pschRackHtml(a, room.id, selAisle, hlAisle, hlOutAisles); }).join('');
  var store = '<div class="psch-zone-title">STORAGE RACKS</div>' +
    '<div class="psch-rack-grid">' + racks + '</div>' +
    '<div class="psch-zone-note">racks named AISLE-LEVEL-BAY · click a rack to view its bins in the bin grid below</div>';
  return '<div class="psch-room">' + head +
    '<div style="padding:4px">' + store + '</div>' +
    '<div class="psch-room-note">' + room.note + '</div></div>';
}

function renderPschOutbound(){
  // OUTBOUND section: the physical Releasing Lanes first, then the Ambient
  // and Cold Room rackings — stacked in that order, so the releasing lanes
  // sit straight below the Receiving Lanes section above.
  var hlAisle = null, hlOut = null;
  if(selected && selected.kind === 'incoming'){
    var bid = pschCidBin(selected.id);
    if(bid) hlAisle = pschAisleOfBin(bid);
  } else if(selected && selected.kind === 'outbound'){
    // Yellow-highlight the aisles holding this consolidation container's
    // staged pallets (its source cargo can sit across several aisles).
    hlOut = pschAislesOfOutbound(selected.id);
  }
  var roomBox = {};
  state.psch.rooms.forEach(function(r){
    roomBox[r.id] = '<div class="psch-facility">' +
      pschRoomHtml(r, pschSelRack, hlAisle, hlOut) + '</div>';
  });
  $('pschOutboundSec').innerHTML =
    '<fieldset><legend>Releasing Lanes — outbound loads staged here</legend>' +
    '<div id="pschRelLanes"></div>' +
    '<div class="psch-rel-legend">Each vertical block is one physical lane, parked side by side like dock doors along a warehouse wall (numbered 1…26, left to right). The agent allocates a consolidation container one lane, or a contiguous group of adjacent lanes (colour-coded), to stage the pallets waiting to be loaded into it. <b>Click a lane (or its container)</b>: the <b>red outline</b> boxes that container\'s allocated lanes, the facility <b>yellow-highlights the aisles</b> holding its staged pallets, and the plan detail shows the cargo staged on those lanes. Click a free lane, or the selected lane group again, to clear. Grey = free lane.</div>' +
    '</fieldset>' +
    '<fieldset style="margin-top:4px"><legend>Ambient Room — normal-temperature storage</legend>' + (roomBox['ambient'] || '') + '</fieldset>' +
    '<fieldset style="margin-top:4px"><legend>Cold Room — chilled &amp; frozen storage</legend>' + (roomBox['cold_room'] || '') + '</fieldset>';
  renderPschRelLanes();
}

function renderPschReasoning(){
  var txt = null;
  if(selected && selected.kind === 'incoming'){
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) txt = x.reasoning; });
  } else if(selected && selected.kind === 'outbound'){
    state.outbound.forEach(function(x){ if(x.container_id === selected.id) txt = x.reasoning; });
  }
  $('pschReasoning').innerHTML = txt
    ? '<div style="padding:2px">' + reasoningHtml(txt) + '</div>'
    : 'Select a container to see the agent reasoning for its plan.';
}

function selectPschRack(aisle){
  if(pschSelRack === aisle){ // clicking the selected rack again unselects it
    pschSelRack = null;
    pschSelBin = null;
    selected = null;
    setDetail(false);
    renderPsch();
    return;
  }
  pschSelRack = aisle;
  pschSelBin = null;
  selected = null; // one selection at a time: picking a rack clears the rest
  setDetail(false); // rack/bin selection does not need the container reflow
  api('/api/psch/inspect', {aisle: aisle}); // log the rack inspection to the trace
  renderPsch();
}

function selectPschBin(bid){
  if(pschSelBin === bid){ // clicking the selected bin again unselects it
    pschSelBin = null;
    if(selected && selected.kind === 'incoming'){
      var b = state.psch.bins[bid];
      if(b && b.container_id === selected.id) selected = null;
    } else if(selected && selected.kind === 'stock' && selected.id === bid){
      selected = null;
    }
    setDetail(false);
    renderPsch();
    return;
  }
  pschSelBin = bid;
  var bin = state.psch.bins[bid];
  if(bin && bin.stock){
    // Dwell-stock bin (prior-wave carryover): show the stock inspector, not
    // the inbound-container flow (these pallets are not part of this wave).
    selected = {kind:'stock', id:bid};
    pschSelRack = pschAisleOfBin(bid);
    setDetail(false);
    renderPsch();
    return;
  }
  if(bin && bin.container_id){
    selectIncoming(bin.container_id);
  }
  renderPsch();
}

function pickPschIncoming(cid){
  if(selected && selected.kind === 'incoming' && selected.id === cid){
    // clicking the selected incoming container again unselects it
    selected = null;
    pschSelRack = null;
    pschSelBin = null;
    setDetail(false);
    renderPsch();
    return;
  }
  var bid = pschCidBin(cid);
  pschSelRack = bid ? pschAisleOfBin(bid) : null;
  pschSelBin = bid;
  selectIncoming(cid);
  renderPsch();
}

function renderPschRackDetail(){
  if(!pschSelRack){
    $('pschRackDetail').innerHTML = 'Select an aisle in the facility view to see its AISLE-LEVEL-BAY bin grid.';
    return;
  }
  var aisle = pschSelRack;
  var room = pschRoomOfAisle(aisle);
  var r = null, a = null;
  state.psch.rooms.forEach(function(x){
    if(x.id === room) x.aisles.forEach(function(y){ if(y.id === aisle){ r = x; a = y; } });
  });
  if(!a){ $('pschRackDetail').innerHTML = 'Rack not found.'; return; }
  var w = Math.max(2, Math.round(a.pct || 0));
  var head = '<b>Aisle ' + aisle + '</b> (' + r.label + ' · ' + r.temp + ') — ' +
    a.used + '/' + a.cap + ' bins · ' + a.pct + '% occupied · ' +
    '<span style="color:#404040">levels 1–' + a.levels + ' × bays 1–' + a.bays + ' (boxes A/B/C)</span>' +
    '<div class="psch-occ-bar"><i style="width:' + w + '%"></i></div>' +
    '<div style="font-size:8px;color:#404040;margin-top:2px">slotting: cargo released soon sits at floor level, slow movers higher</div>';
  var bays = a.bays, levels = a.levels, boxes = ['A','B','C'];
  // Two header rows: bay groups spanning its three box columns, then A/B/C.
  var headRow = '<tr><th class="psch-b-box" rowspan="2">Level</th>';
  for(var b=1;b<=bays;b++) headRow += '<th class="psch-b-bay" colspan="3">Bay ' + b + '</th>';
  headRow += '</tr><tr>';
  for(var b=1;b<=bays;b++){ for(var bi=0;bi<boxes.length;bi++) headRow += '<th>' + boxes[bi] + '</th>'; }
  headRow += '</tr>';
  var rows = '';
  for(var lvl=levels;lvl>=1;lvl--){ // top level first, like a rack elevation
    rows += '<tr><td class="psch-b-box">' + lvl + '</td>';
    for(var b=1;b<=bays;b++){
      for(var bi=0;bi<boxes.length;bi++){
        var bid = aisle + '-' + pad2(lvl) + '-' + b + boxes[bi];
        var bin = state.psch.bins[bid];
        var cls = 'psch-bin-empty';
        var inner = '<span class="psch-bin-id">' + b + boxes[bi] + '</span><br>—';
        var title = bid + ' · empty';
        if(bin){
          cls = bin.arrived ? 'psch-bin-occupied' : 'psch-bin-reserved';
          if(bid === pschSelBin) cls += ' sel';
          else if(selected && selected.kind === 'incoming' && bin.container_id === selected.id) cls += ' hl';
          title = bin.container_id + ' · ' + bin.status + ' · ' + (bin.pallets || 0) + ' pallets · ' +
            'receipt ' + isoDT(bin.receipt_eta) + ' · pick ' + isoDT(bin.pallet_pick_time);
          inner = '<span class="psch-bin-id">' + b + boxes[bi] + '</span><br>' +
            '<span class="psch-bin-cid">' + bin.container_id + '</span><br>' +
            '<span class="psch-bin-meta">' + pschStatusShort(bin.status) + '</span>';
        }
        rows += '<td class="' + cls + '" data-bin="' + bid + '" onclick="selectPschBin(\'' + bid + '\')" ' +
          'title="' + title.replace(/'/g, '') + '">' + inner + '</td>';
      }
    }
    rows += '</tr>';
  }
  $('pschRackDetail').innerHTML = head +
    '<div class="psch-grid-wrap"><table class="psch-bin-grid">' + headRow + rows + '</table></div>' +
    '<div style="font-size:8px;color:#606060;margin-top:3px">yellow = bin reserved by the agent before arrival · ' +
    'green = cargo arrived in bin · grey = empty · click a bin to open its container</div>';
}

function renderPschIncoming(){
  // Storage list: first the inbound containers and the cargoes being unloaded
  // from each, then the bin locations they are put away to (or, for whole-
  // container flows, the slot / bay they are staged in).
  var html = '<table width="100%">';
  state.inbound.forEach(function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (m.status === 'Arrived') ? 'arrived' : countdownHtml(m.hours_until);
    html += '<tr><td><button type="button" class="' + cls + '" onclick="pickPschIncoming(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + flowPill(m.flow) + ' &nbsp;' + badge(m.status) + '<br>' +
      '<b>' + m.cargoes + '</b> cargoes unloading · putaway <b>' + m.bin_location + '</b> · ' + m.putaway_robot + '<br>' +
      'Receiving ' + m.receiving_area + ' · ETA ' + isoDT(m.psch_receipt_eta) + ' (' + etaTxt + ')</button></td></tr>';
  });
  $('pschIncoming').innerHTML = state.inbound.length
    ? html + '</table>'
    : '<div style="padding:4px">No inbound containers in the pipeline.</div>';
}

function inspectorStock(b){
  return '<table width="100%"><tr><td><b>' + b.container_id + '</b></td>' +
    '<td align="right">' + badge('Arrived') + '</td></tr></table>' +
    '<fieldset><legend>Dwell stock (prior waves)</legend>' +
    '<table class="grid" width="100%"><tbody>' +
    row('Bin location', '<b>' + b.id + '</b> (AISLE-LEVEL-BAY)') +
    row('Pallets stored', b.pallets) +
    row('Room', (pschRoomOfAisle(pschAisleOfBin(b.id)) === 'cold_room' ? 'Cold Room (reefer)' : 'Ambient')) +
    row('Consignment', 'Carryover pallets from previous waves, awaiting consolidation / delivery in this wave') +
    row('Status', 'In storage (Arrived)') +
    '</tbody></table></fieldset>';
}

function renderPschInspector(){
  if(selected && selected.kind === 'stock'){
    var b = state.psch.bins[selected.id];
    if(b){ $('pschInspector').innerHTML = inspectorStock(b); return; }
  }
  if(selected && selected.kind === 'incoming'){
    var m = null;
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) m = x; });
    if(m){ $('pschInspector').innerHTML = inspectorIncoming(m, false); ensureBayplan(m.container_id); return; }
  }
  if(selected && selected.kind === 'outbound'){
    var o = null;
    state.outbound.forEach(function(x){ if(x.container_id === selected.id) o = x; });
    if(o){ $('pschInspector').innerHTML = inspectorOutbound(o, false); ensureBayplan(o.container_id); return; }
  }
  $('pschInspector').innerHTML = 'Select an incoming container (list, rack or bin) to open its ship-tracker detail — ' +
    'which vessel carries it, its exact bay cell on the vessel, the journey to PSCH, and the agent\'s plan.';
}

// ---- Control Tower -----------------------------------------------------------
function renderTower(){
  var k = state.kpis;
  var distBy = {staged:0, released:0, in_transit:0, delivered:0};
  state.distribution.forEach(function(d){ distBy[d.status] = (distBy[d.status] || 0) + 1; });
  var topBy = {pending:0, in_progress:0, done:0};
  state.top_up.forEach(function(j){ topBy[j.status] = (topBy[j.status] || 0) + 1; });
  $('kpiRows').innerHTML = kpiStripHtml([
    ['Containers in pipeline (all flows)', state.inbound.length],
    ['At sea / on road', k.en_route_total],
    ['Arrived at PSCH', k.arrived_at_psch],
    ['Avg sea→PSCH pipeline', k.avg_pipeline_h + ' h'],
    ['Arrival rate (next 6h)', k.arrival_rate + ' ctn/h'],
    ['Bin utilisation', k.bin_util + '%'],
    ['Outbound loaded (port)', k.loaded_outbound + ' / ' + k.outbound_total],
    ['Land releases', state.distribution.length],
    ['Top-up jobs', state.top_up.length],
    ['QC tasks', state.qc.length],
    ['Yard utilisation', k.avg_yard_util + '%'],
    ['Drayage utilisation', k.drayage_util + '%'],
    ['AS/RS stackers charging', state.asrs.charging + ' / ' + state.asrs.stackers.length]
  ]);

  $('journeyRows').innerHTML = Object.keys(k.journey_counts).map(function(s, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + badge(s) + '</td><td class="metric">' + k.journey_counts[s] + '</td></tr>';
  }).join('');
  $('outboundStatusRows').innerHTML = Object.keys(k.outbound_counts).map(function(s, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + badge(s) + '</td><td class="metric">' + k.outbound_counts[s] + '</td></tr>';
  }).join('');

  var hub = [];
  ['staged', 'released', 'in_transit', 'delivered'].forEach(function(s){
    if(distBy[s]) hub.push('<tr><td>Distribution · ' + badge(s) + '</td><td class="metric">' + distBy[s] + '</td></tr>');
  });
  ['pending', 'in_progress', 'done'].forEach(function(s){
    if(topBy[s]) hub.push('<tr><td>Top Up · ' + badge(s) + '</td><td class="metric">' + topBy[s] + '</td></tr>');
  });
  var qcByKind = {survey:0, sampling:0, repack:0, rework:0};
  state.qc.forEach(function(t){ qcByKind[t.kind] = (qcByKind[t.kind] || 0) + 1; });
  Object.keys(qcByKind).forEach(function(kind){
    hub.push('<tr><td>QC · ' + kind + '</td><td class="metric">' + qcByKind[kind] + '</td></tr>');
  });
  hub.push('<tr><td>AS/RS · stackers charging</td><td class="metric">' + state.asrs.charging + ' / ' + state.asrs.stackers.length + '</td></tr>');
  $('hubRows').innerHTML = hub.join('') || '<tr><td colspan="2">No hub flows yet.</td></tr>';

  // Attention needed — live findings grouped by category, with one-click
  // AI-suggested actions (propose -> approve/reject through the same gate).
  var exc = state.exceptions || [];
  var excByCat = {}, catOrder = [];
  exc.forEach(function(e){
    var key = excCatOf(e).label;
    if(!excByCat[key]){ excByCat[key] = []; catOrder.push(key); }
    excByCat[key].push(e);
  });
  var chips = '<input type="button" class="btn' + (excFilter === 'all' ? ' active' : '') +
    '" value="All (' + exc.length + ')" onclick="excSetFilter(\'all\')">';
  catOrder.forEach(function(k){
    chips += '<input type="button" class="btn' + (excFilter === k ? ' active' : '') +
      '" value="' + esc(k) + ' (' + excByCat[k].length + ')" onclick="excSetFilter(\'' + esc(k) + '\')">';
  });
  $('excChips').innerHTML = chips;
  var rows = [];
  catOrder.forEach(function(k){
    if(excFilter !== 'all' && excFilter !== k) return;
    var list = excByCat[k], cat = excCatOf(list[0]);
    rows.push('<tr class="exc-group ' + cat.cls + '"><td colspan="4">' + esc(k) + ' &mdash; ' +
      list.length + (list.length === 1 ? ' item' : ' items') + '</td></tr>');
    list.forEach(function(e, i){
      var pkey = (e.kind || 'x') + '|' + (e.container_id || '');
      excByKey[pkey] = e;
      rows.push(excRowHtml(e, pkey, i));
    });
  });
  $('exceptionRows').innerHTML = rows.join('') ||
    '<tr><td colspan="4">Nothing needs attention — the terminal is running clean.</td></tr>';
}

// ---- Attention needed (exception watch) helpers -----------------------------
var EXC_CATS = {
  'receipt_eta_missed':         {label:'Inbound & Receiving',  cls:'exc-cat-in'},
  'customs_hold':               {label:'Customs & Compliance', cls:'exc-cat-customs'},
  'loading_window_missed':      {label:'Outbound & Loading',   cls:'exc-cat-out'},
  'loading_cutoff_approaching': {label:'Outbound & Loading',   cls:'exc-cat-out'},
  'vessel_etd_slip':            {label:'Vessels & Berths',     cls:'exc-cat-vessel'}
};
var excFilter = 'all';
var excPending = {};
var excByKey = {};
function excCatOf(e){ return EXC_CATS[e.kind] || {label:'Other', cls:'exc-cat-other'}; }
function excRowHtml(e, pkey, i){
  var col = e.severity === 'critical' ? '#B00020' : (e.severity === 'warning' ? '#B45309' : '#000080');
  var act = e.suggested_action, actionHtml = '';
  if(act && act.tool){
    if(excPending[pkey] === 'done'){
      actionHtml = '<div class="exc-act exc-done">Executed — the plan has been updated.</div>';
    } else if(excPending[pkey]){
      actionHtml = '<div class="exc-act"><b>Proposed:</b> ' + esc(act.label) +
        ' <input type="button" class="btn" value="&#10003; Approve" onclick="excAct(\'' + pkey + '\',\'approve\')"> ' +
        '<input type="button" class="btn" value="&#10007; Reject" onclick="excAct(\'' + pkey + '\',\'reject\')"></div>';
    } else {
      actionHtml = '<div class="exc-act"><input type="button" class="btn exc-propose" value="&#9654; Propose: ' +
        esc(act.label) + '" onclick="excPropose(\'' + pkey + '\')"></div>';
    }
  } else {
    var noTxt = e.kind === 'customs_hold'
      ? 'No plan change — this container stays out of the plan until the hold clears.'
      : 'No safe single action right now — review it in PSA Intelligence.';
    actionHtml = '<div class="exc-act exc-noaction"><i>' + noTxt + '</i> <a href="#" onclick="excAsk(\'' +
      esc(e.container_id || '') + '\');return false;">Ask the agent about this</a></div>';
  }
  return '<tr' + (i%2 ? ' class="alt"' : '') + '>' +
    '<td><font color="' + col + '"><b>' + esc(e.severity.toUpperCase()) + '</b></font></td>' +
    '<td><b>' + esc(e.issue) + '</b></td>' +
    '<td>' + esc(e.container_id || '') + '</td>' +
    '<td>' + esc(e.detail || '') + '<br><i>Recommend:</i> ' + esc(e.recommendation || '') + actionHtml + '</td>' +
    '</tr>';
}
async function excPropose(pkey){
  var e = excByKey[pkey];
  if(!e || !e.suggested_action) return;
  var act = e.suggested_action;
  var r = await api('/api/exception/action', {tool: act.tool, args: act.args});
  if(!r || !r.ok){ alert('Action failed: ' + ((r && r.error) || 'unknown error')); return; }
  excPending[pkey] = (r.result && r.result.status === 'pending_approval') ? 'pending' : 'done';
  await refresh();
}
async function excAct(pkey, mode){
  var e = excByKey[pkey];
  var act = e && e.suggested_action;
  if(!act) return;
  await api(mode === 'approve' ? '/api/agent/approve' : '/api/agent/reject', {tool: act.tool, args: act.args});
  delete excPending[pkey];
  await refresh();
}
function excSetFilter(k){ excFilter = k; render(); }
function excAsk(cid){
  showView('intel');
  var input = $('intelAsk');
  if(input){
    input.value = (cid ? 'What is happening with ' + cid + '? ' : '') + 'Why does it need attention and what should I do about it?';
    intelSend();
  }
}

// ---- Agent reasoning ---------------------------------------------------------
// Render a plan-reasoning string as structured rows. The generator emits
// newline-separated "Label: value" lines (journey / timeline / receiving /
// flow / plan ...), plus optional [agent] change notes and ROAD DELAY warns.
// Fallback: a single unlabelled paragraph renders as one wrapped line.
function reasoningHtml(txt){
  if(!txt) return '—';
  var out = '<table class="reasoning"><tbody>';
  String(txt).split('\n').forEach(function(l){
    l = l.trim();
    if(!l) return;
    if(l.indexOf('[agent]') === 0){
      out += '<tr class="rsn-agent"><td colspan="2">&#9650; agent change — ' + esc(l.slice(7).trim()) + '</td></tr>';
      return;
    }
    if(l.indexOf('ROAD DELAY') >= 0 || l.indexOf('⚠') >= 0){
      out += '<tr class="rsn-warn"><td colspan="2">' + esc(l) + '</td></tr>';
      return;
    }
    var m = l.match(/^([^:]{1,60}):\s*(.*)$/);
    if(m && m[2]){
      out += '<tr><td class="rk">' + esc(m[1]) + '</td><td class="rv">' + esc(m[2]) + '</td></tr>';
    } else {
      out += '<tr class="rsn-plain"><td colspan="2">' + esc(l) + '</td></tr>';
    }
  });
  return out + '</tbody></table>';
}

// ---- Trace -------------------------------------------------------------------
// HTML-escape helper (alias of intelEscape, which is hoisted).
function esc(t){ return intelEscape(t); }

// Execution trace is grouped by actor (each actor is its own category) and can
// be filtered. "AI Changes" is the cross-actor view that tracks the full
// lifecycle of every plan change: approval_required (proposal) -> approved /
// rejected (your decision) -> tool_call (execution).
var traceFilter = 'all';
var _lastTraceSig = '';
var ACTOR_META = {
  'agent':    {label:'AGENT',    cls:'actor-agent'},
  'runtime':  {label:'RUNTIME',  cls:'actor-runtime'},
  'operator': {label:'OPERATOR', cls:'actor-operator'},
  'system':   {label:'SYSTEM',   cls:'actor-system'}
};
var EVENT_LABELS = {
  'tool_call': 'tool call', 'approval_required': 'approval required',
  'approved': 'approved', 'rejected': 'rejected',
  'agent_run_start': 'agent run started', 'agent_run_end': 'agent run finished',
  'mcc_plan_computed': 'plan computed', 'slotting_applied': 'slotting applied',
  'consolidation_group_planned': 'group planned',
  'scenario_seeded': 'scenario seeded',
  'wave_complete_regenerating': 'wave regenerated',
  'berth_inspected': 'berth inspected', 'psch_rack_inspected': 'rack inspected'
};

function isChangeEvent(e){
  if(e.event === 'approval_required' || e.event === 'approved' || e.event === 'rejected') return true;
  if(e.event === 'tool_call'){
    var p = (e.detail || {}).permission;
    return p === 'mutate' || p === 'approval';
  }
  return false;
}

function traceInFilter(e){
  if(traceFilter === 'all') return true;
  if(traceFilter === 'changes') return isChangeEvent(e);
  return e.actor === traceFilter;
}

// Per-tool outcome stack (newest-first): lets each approval_required row show
// whether the proposal was later APPROVED, REJECTED, or is still PENDING.
function approvalOutcomes(trace){
  var stack = {};
  trace.forEach(function(e){
    if(e.event === 'approved' || e.event === 'rejected'){
      var t = (e.detail || {}).tool || '?';
      (stack[t] = stack[t] || []).push(e.event);
    }
  });
  return stack;
}

function traceActorBadge(actor){
  var m = ACTOR_META[actor] || {label: actor || '?', cls: ''};
  return '<span class="actor-badge ' + m.cls + '">' + esc(m.label) + '</span>';
}

function traceArgText(d){
  var a = d.args;
  try{ a = (typeof a === 'string') ? JSON.parse(a) : (a || {}); }
  catch(err){ return esc(String(d.args || '').slice(0, 80)); }
  var parts = [];
  if(a.container_id) parts.push(a.container_id);
  if(a.bin_location) parts.push('\u2192 bin ' + a.bin_location);
  if(a.receiving_area) parts.push('\u2192 ' + a.receiving_area);
  if(a.reason) parts.push('(' + esc(String(a.reason).slice(0, 50)) + ')');
  return parts.length ? parts.join(' ') : esc(String(d.args || '').slice(0, 80));
}

function traceApprovalBadge(status){
  var map = {
    'approved': '<span class="tr-badge ok">APPROVED</span>',
    'rejected': '<span class="tr-badge bad">REJECTED</span>',
    'pending':  '<span class="tr-badge pend">PENDING</span>'
  };
  return map[status] || '';
}

function traceRow(e, outcomes, idx){
  var d = e.detail || {};
  var detailHtml = '', evLabel = esc(EVENT_LABELS[e.event] || e.event);
  switch(e.event){
    case 'tool_call': {
      var tool = esc(d.tool || '?');
      var perm = d.permission || '';
      var permTxt = {'read':'read','mutate':'change','approval':'execution'}[perm] || esc(perm);
      var color = perm === 'read' ? '#404040' : (perm === 'mutate' ? '#8B5A00' : '#000080');
      var resultTxt = '';
      if(perm !== 'read'){
        try{
          var r = JSON.parse(d.result || '{}');
          if(r && typeof r === 'object'){
            var cell = r.bin_location || r.receiving_area;
            if(cell) resultTxt = ' \u2192 ' + esc(String(cell));
          }
        }catch(err){}
      }
      detailHtml = '<font color="' + color + '"><b>' + tool + '</b> (' + permTxt + ')</font>' + resultTxt;
      break;
    }
    case 'approval_required': {
      var out = outcomes[d.tool] && outcomes[d.tool].length ? outcomes[d.tool].pop() : null;
      detailHtml = traceApprovalBadge(out === 'approved' ? 'approved' : (out === 'rejected' ? 'rejected' : 'pending')) +
        ' <b>' + esc(d.tool || '?') + '</b> · ' + traceArgText(d);
      break;
    }
    case 'approved':
      detailHtml = traceApprovalBadge('approved') + ' <b>' + esc(d.tool || '?') + '</b> · ' + traceArgText(d);
      break;
    case 'rejected':
      detailHtml = traceApprovalBadge('rejected') + ' <b>' + esc(d.tool || '?') + '</b> · ' + traceArgText(d);
      break;
    case 'agent_run_start':
      detailHtml = 'goal: <i>' + esc(String(d.goal || '').slice(0, 90)) + '</i> · brain: <b>' + esc(d.brain) +
        '</b> · autonomy: ' + esc(d.autonomy) + (d.history ? ' · context: ' + d.history + ' prior msg' : '');
      break;
    case 'agent_run_end':
      detailHtml = (d.ok ? '<font color="#006400"><b>OK</b></font>' : '<font color="#8B0000"><b>FAILED</b></font>') +
        ' · ' + esc(String(d.summary || d.error || '').slice(0, 130));
      break;
    case 'mcc_plan_computed':
      detailHtml = '<b>' + d.containers_planned + '</b> containers planned · <b>' + d.outbound_groups +
        '</b> outbound groups · ' + d.receiving_areas_opened + ' receiving areas · ' + d.arrival_rate_ctn_per_hr + ' ctn/h';
      break;
    case 'slotting_applied':
      detailHtml = esc(String(d.rule || 'dwell-based slotting applied'));
      break;
    case 'consolidation_group_planned':
      detailHtml = esc(d.outbound_container) + ' \u2192 ' + esc(d.destination) + ' · bound ' + esc(d.bound_vessel) +
        ' · ' + d.sources + ' source container(s)';
      break;
    case 'scenario_seeded':
      detailHtml = 'seed <b>' + d.seed + '</b> · ' + d.containers + ' containers · ' + d.bookings + ' bookings';
      break;
    case 'wave_complete_regenerating':
      detailHtml = esc(String(d.note || 'full lifecycle finished — seeding the next wave'));
      break;
    case 'berth_inspected':
      detailHtml = 'inspected berth <b>' + esc(d.berth) + '</b>';
      break;
    case 'psch_rack_inspected':
      detailHtml = 'inspected aisle <b>' + esc(d.aisle) + '</b>';
      break;
    default:
      detailHtml = Object.keys(d).map(function(k){
        return esc(k) + ': ' + esc(String(d[k]).slice(0, 60));
      }).join(' · ') || '—';
  }
  return '<tr' + (idx % 2 ? ' class="alt"' : '') + '><td>' + isoDTS(e.ts).slice(11) + '</td><td>' +
    traceActorBadge(e.actor) + '</td><td>' + evLabel + '</td><td>' + detailHtml + '</td></tr>';
}

function renderTrace(){
  var trace = state.trace || [];
  // Live indicator: the dot pulses gently; it flashes brighter the moment new
  // trace events arrive on the state poll (zero-delay feedback of live action).
  var sig = trace.length + '|' + (trace.length ? String(trace[0].ts) : '');
  if(sig !== _lastTraceSig){
    _lastTraceSig = sig;
    var liveEl = $('traceLive');
    if(liveEl){
      if(!liveEl.classList.contains('flash')){
        liveEl.classList.add('flash');
        setTimeout(function(){ if(liveEl) liveEl.classList.remove('flash'); }, 1200);
      }
      liveEl.title = 'Live — ' + isoDT(trace.length ? trace[0].ts : null) + 'Z';
    }
  }
  // Filter chips with live counts.
  var counts = {agent:0, runtime:0, operator:0, system:0, changes:0};
  trace.forEach(function(e){
    if(counts.hasOwnProperty(e.actor)) counts[e.actor]++;
    if(isChangeEvent(e)) counts.changes++;
  });
  var chips = [['all','All',trace.length],['changes','AI Changes',counts.changes]]
    .concat(['agent','runtime','operator','system'].map(function(k){
      return [k, ACTOR_META[k].label.charAt(0) + ACTOR_META[k].label.slice(1).toLowerCase(), counts[k]];
    }));
  $('traceFilters').innerHTML = chips.map(function(c){
    return '<input type="button" class="btn' + (traceFilter === c[0] ? ' active' : '') +
      '" value="' + esc(c[1]) + ' (' + c[2] + ')" onclick="setTraceFilter(\'' + c[0] + '\')">';
  }).join(' ');
  // Group rows by actor: a section header appears when the actor changes.
  var outcomes = approvalOutcomes(trace);
  var html = '', lastCat = null, idx = 0;
  trace.forEach(function(e){
    if(!traceInFilter(e)) return;
    if(e.actor !== lastCat){
      var m = ACTOR_META[e.actor] || {label: e.actor || '?', cls: ''};
      html += '<tr class="trace-cat"><td colspan="4"><span class="actor-badge ' + m.cls + '">' +
        esc(m.label) + '</span></td></tr>';
      lastCat = e.actor;
    }
    html += traceRow(e, outcomes, idx++);
  });
  $('traceRows').innerHTML = html || '<tr><td colspan="4">No trace events in this category yet.</td></tr>';
}

function setTraceFilter(f){
  traceFilter = f;
  renderTrace();
}

function renderTicker(){
  var k = state.kpis;
  $('ticker').innerHTML = '*** LIVE · 24/7 *** Port ↔ PSCH flow — ' +
    state.inbound.length + ' inbound containers (MCC ' + flowCount('mcc') + ' · Distribution ' + flowCount('distribution') +
    ' · Top Up ' + flowCount('topup') + ' · Transload ' + flowCount('transload') + ') · ' +
    k.arrived_at_psch + ' arrived at PSCH · ' + state.distribution.length + ' land releases · ' +
    state.top_up.length + ' top-up jobs · ' + state.qc.length + ' QC tasks · ' +
    k.loaded_outbound + ' outbound loaded to port ***';
}

// ---- PSA Intelligence (prompt page) -----------------------------------------
// "Ask anything" chat page. Every question goes through the same agent seam
// (AgentRuntime + brain) as the rest of the stack, so it is ready for the
// agentic AI API: the brain reads live data through the tool registry (traced),
// and the page shows the brain + autonomy that answered each message.
var intelThread = [];
var intelBusy = false;
var lastBrainName = '';  // latest brain that answered, for display migration

// The conversation is logged to localStorage so a page refresh (or leaving
// and coming back) keeps the whole thread — like any normal chat interface.
var INTEL_STORE_KEY = 'psa-intel-thread';
function intelSave(){
  try{ localStorage.setItem(INTEL_STORE_KEY, JSON.stringify(intelThread)); }catch(e){}
}
function intelLoad(){
  try{
    var raw = localStorage.getItem(INTEL_STORE_KEY);
    if(raw){
      var t = JSON.parse(raw);
      if(Array.isArray(t)) intelThread = t.filter(function(m){ return m && m.role; });
    }
  }catch(e){}
}

// Last few thread messages as conversation context for the brain, so follow-ups
// ("and what about its vessel?") resolve against what was just discussed.
function intelHistory(n){
  n = n || 10;
  return intelThread.slice(-n).filter(function(m){
    return m.text && m.text !== 'Thinking…' && m.role;
  }).map(function(m){ return {role: m.role, text: m.text}; });
}

function renderIntel(){
  // Restore a logged conversation once, on first render (not on every poll).
  if(!window._intelLoaded){ window._intelLoaded = true; intelLoad(); intelRender(); }
  // Quick-question links are seeded from live state so they always reference
  // real data, and only the first time (they stay put across polls).
  var s = $('intelQuick');
  if(!s || s.childElementCount) return;
  var cid = state.inbound[0] ? state.inbound[0].container_id : 'SEAU9342928';
  var links = [
    'Track: ' + cid,
    'How many containers are at sea?',
    'What is the bin utilisation?',
    'Where is MAERSK EGYPT?',
    'What changed in the last hour?'
  ];
  s.innerHTML = links.map(function(c){
    return '<a href="#" onclick="intelSendFromSuggestion(\'' + c.replace(/'/g, '') + '\');return false;">' + c + '</a>';
  }).join('');
}

function intelBubble(role, text, meta){
  return '<div class="intel-msg ' + role + '"><div><div class="bubble">' + text + '</div>' +
    (meta ? '<div class="meta">' + meta + '</div>' : '') + '</div></div>';
}

function intelEscape(t){
  return (t || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
// Render the agent's markdown-ish text as clean HTML: **bold**, ### headings,
// ``` fenced code blocks and `inline code`, so raw symbols never leak through.
function intelFmt(t){
  t = intelEscape(t);
  t = t.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  t = t.replace(/```([\s\S]*?)```/g, function(m, body){
    return '<pre class="intel-code">' + body.replace(/^\n+|\s+$/g, '') + '</pre>';
  });
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/^(#{1,6})\s+(.+)$/gm, '<div class="intel-h"><b>$2</b></div>');
  return t;
}

// Trim trailing courtesy offers ("Would you like me to ...?") so answers end
// with substance instead of a prompt for more work.
function intelPolish(t){
  if(!t) return t;
  var s = t.replace(/\s+$/, '');
  for(var i = 0; i < 4; i++){
    var before = s;
    s = s.replace(/(\n|^)\s*(would you like|shall i|do you want|should i|let me know|want me to|may i|let's proceed|shall we|if you need|feel free to)[^\n]*\??\s*$/i, '$1');
    if(s === before) break;
  }
  return s.replace(/\s+$/, '');
}

// When an answer carries pending proposals, drop the model's numbered recap
// lines ("1. MAEU4801288 has been proposed to be moved to bin 5-08-1B.") — the
// labelled proposal blocks below carry exactly that info, so the prose doesn't
// repeat it. Only lines that name one of the pending containers are removed.
function intelStripProposalProse(t, pending){
  if(!t || !pending || !pending.length) return t;
  var ids = {};
  pending.forEach(function(e){
    var cid = (((e || {}).args || {}).container_id || '').toUpperCase();
    if(cid) ids[cid] = true;
  });
  var keys = Object.keys(ids);
  if(!keys.length) return t;
  var lines = String(t).split('\n');
  var out = [];
  for(var i = 0; i < lines.length; i++){
    var m = lines[i].match(/^\s*\d+[.)]\s+(.+)$/);
    if(m && keys.some(function(c){ return m[1].indexOf(c) >= 0; })) continue;
    out.push(lines[i]);
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n').replace(/\s+$/, '');
}

// A readable label for one pending plan change, so multi-proposal replies
// show exactly which container / change each Approve / Reject pair is about.
function intelPendingLabel(e){
  var a = (e && e.args) || {};
  var cid = a.container_id;
  if(e.tool === 'reassign_bin' && cid && a.bin_location)
    return 'Reassign <b>' + intelEscape(cid) + '</b> \u2192 bin <b>' + intelEscape(a.bin_location) + '</b>';
  if(e.tool === 'reschedule_receiving_area' && cid && a.receiving_area)
    return 'Reschedule <b>' + intelEscape(cid) + '</b> \u2192 <b>' + intelEscape(a.receiving_area) + '</b>';
  if(e.tool === 'release_lane' && cid)
    return 'Release lane of <b>' + intelEscape(cid) + '</b>';
  return intelEscape(e.tool || 'plan change');
}

// Render each pending plan change as its own labelled block, so a multi-action
// reply shows N clearly-separated proposals — each with its own Approve/Reject
// pair — instead of a wall of numbered prose.
function intelProposalBlocks(pending, mi){
  return '<div class="intel-approve-row">' + pending.map(function(e, ei){
    return '<div class="intel-proposal">' +
      '<div class="intel-proposal-head">Proposal ' + (ei + 1) + ' · ' + intelEscape(e.tool || 'plan change') + '</div>' +
      '<div class="intel-proposal-label">' + intelPendingLabel(e) + '</div>' +
      '<div class="intel-proposal-actions">' +
        '<input type="button" class="btn" value="&#10003; Approve" onclick="intelApprove(' + mi + ',' + ei + ')"> ' +
        '<input type="button" class="btn" value="&#10007; Reject" onclick="intelReject(' + mi + ',' + ei + ')">' +
      '</div></div>';
  }).join('') + '</div>';
}

function intelRender(){
  // Gemini positioning: the centered hero (heading + prompt) shows until the
  // first question; the conversation then takes the stage below the prompt.
  var hero = $('intelHero'), thread = $('intelThread');
  if(!intelThread.length){
    hero.style.display = '';
    thread.style.display = 'none';
    return;
  }
  hero.style.display = 'none';
  thread.style.display = 'block';
  var brainNow = lastBrainName || 'llama';
  thread.innerHTML = intelThread.map(function(m, mi){
    // Historical log entries stored an old brain label; migrate on display.
    var meta = (m.meta || '').replace(/agentic-api|psa-iwx-v1/g, brainNow);
    var text = m.text;
    var extra = '';
    // Pending plan changes (from the rule brain or the LLM) render as one
    // labelled block per proposal with its own Approve / Reject buttons.
    if(m.pending && m.pending.length){
      text = intelStripProposalProse(text, m.pending);
      extra = intelProposalBlocks(m.pending, mi);
    }
    return intelBubble(m.role, intelFmt(intelPolish(text)), meta) + extra;
  }).join('');
  thread.scrollTop = thread.scrollHeight;
  if($('intelAsk')) $('intelAsk').focus();
}

function intelClear(){
  intelThread = [];
  try{ localStorage.removeItem(INTEL_STORE_KEY); }catch(e){}
  if($('intelAsk')) $('intelAsk').value = '';
  intelRender();
}

function intelSendFromSuggestion(q){ $('intelAsk').value = q; intelSend(); }

async function intelSend(){
  var input = $('intelAsk');
  var q = (input.value || '').trim();
  if(!q || intelBusy) return;
  intelBusy = true;
  var btn = $('intelSendBtn');
  if(btn) btn.disabled = true;
  input.value = '';
  var history = intelHistory(10); // capture BEFORE pushing the new question
  intelThread.push({role:'user', text:q});
  intelThread.push({role:'assistant', text:'Thinking…', meta:''});
  intelRender();
  try{
    var r = await api('/api/intel/ask', {question:q, context:{sim_now: state.sim_now}, history: history});
    var meta = 'brain: <b>' + intelEscape(r.brain) + '</b> · autonomy: <b>' + intelEscape(r.autonomy) + '</b>' +
      (r.error ? ' · <font color="#F28B82">error: ' + intelEscape(r.error) + '</font>' : '');
    // Plan-change proposals come back as pending events -> Approve / Reject.
    var pending = (r.events || []).filter(function(e){ return e && e.tool && e.kind !== 'executed'; });
    var answer = intelPolish(r.answer || 'No answer returned.');
    lastBrainName = r.brain || lastBrainName;
    intelThread[intelThread.length - 1] = {role:'assistant', text:answer, meta:meta, pending: pending};
    if($('intelBrain2')) $('intelBrain2').textContent = r.brain || '';
    if($('intelAutonomy')) $('intelAutonomy').textContent = r.autonomy || '';
  }catch(err){
    intelThread[intelThread.length - 1] = {role:'assistant', text:'Sorry — I could not reach the agent: ' + err.message, meta:''};
    if($('intelBrain2')) $('intelBrain2').textContent = 'error';
  }
  intelBusy = false;
  if(btn) btn.disabled = false;
  intelRender();
  intelSave(); // log the thread so it survives a refresh
}

// Approve a pending plan change: the runtime executes the tool (traced), the
// plan updates, and the next 8s poll shows the change across every page.
function intelApprove(mi, ei){
  var m = intelThread[mi];
  if(!m || !m.pending || !m.pending[ei]) return;
  var e = m.pending[ei];
  api('/api/agent/approve', {tool: e.tool, args: e.args || {}}).then(function(r){
    m.pending.splice(ei, 1);
    m.meta = (m.meta || '') + '<br><font color="#006400"><b>&#10003; approved: ' + intelEscape(e.tool) + '</b></font>';
    intelRender(); intelSave();
    renderGoalFromThread();
    refresh(); // pick up the plan change everywhere immediately
  }).catch(function(err){
    m.meta = (m.meta || '') + '<br><font color="#8B0000">approval failed: ' + intelEscape(err.message) + '</font>';
    intelRender(); intelSave();
  });
}

// Reject a pending plan change: recorded to the trace, nothing executes.
function intelReject(mi, ei){
  var m = intelThread[mi];
  if(!m || !m.pending || !m.pending[ei]) return;
  var e = m.pending[ei];
  api('/api/agent/reject', {tool: e.tool, args: e.args || {}}).then(function(r){
    m.pending.splice(ei, 1);
    m.meta = (m.meta || '') + '<br><font color="#8B0000"><b>&#10007; rejected: ' + intelEscape(e.tool) + '</b></font>';
    intelRender(); intelSave();
    renderGoalFromThread();
  }).catch(function(err){});
}

function renderStatus(){
  $('statusText').innerText = 'Ready — ' + state.inbound.length + ' inbound containers, ' +
    state.distribution.length + ' land releases, ' + state.outbound.length + ' back-to-port groups, ' +
    state.top_up.length + ' top-up jobs, ' + state.qc.length + ' QC tasks, ' + state.trace.length + ' trace events.';
  $('lastUpdated').innerText = 'Updated ' + isoDTS(state.sim_now).slice(11);
}

function showView(v){
  var nav = (v !== lastView);
  view = v;
  ['in','store','mcc','dist','top','qc','tower','trace','intel'].forEach(function(name){
    $('view-' + name).style.display = (name === v) ? '' : 'none';
  });
  ['tower','trace','intel'].forEach(function(name){
    $('nav-' + name).className = (name === v) ? 'active' : '';
  });
  // PSCH group: one nav item. Inbound / Storage / Quality Control are direct
  // sub-pages; Outbound opens a second-level menu (MCC, Distribution, Top Up).
  var hub = ['in','store','mcc','dist','top','qc'].indexOf(v) >= 0;
  $('nav-psch').className = hub ? 'active' : '';
  $('nav-in').className = (v === 'in') ? 'active' : '';
  $('nav-store').className = (v === 'store') ? 'active' : '';
  $('nav-out2').className = (v === 'mcc' || v === 'dist' || v === 'top') ? 'active' : '';
  $('nav-mcc').className = (v === 'mcc') ? 'active' : '';
  $('nav-dist').className = (v === 'dist') ? 'active' : '';
  $('nav-top').className = (v === 'top') ? 'active' : '';
  $('nav-qc').className = (v === 'qc') ? 'active' : '';
  if(nav){
    lastView = v;
    // Returning to a hub sub-page restores its original arrangement:
    // columns back to normal widths, selections cleared, dropdowns closed.
    if(v === 'in') resetInView();
    else if(v === 'store') resetStoreView();
    else if(v === 'mcc') resetMccView();
    else if(v === 'dist') resetDistView();
    else if(v === 'top') resetTopView();
    else if(v === 'qc') resetQcView();
  }
}

function toggleFullScreen(){
  var on = document.body.classList.toggle('fullscreen');
  var b = $('fsBtn');
  if(b) b.value = on ? '✕ Exit Full Screen' : '⛶ Full Screen';
}

// Scrolling news strip: drives the ticker text right-to-left continuously via
// a plain interval so it runs in every environment (the deprecated <marquee>
// is disabled under prefers-reduced-motion, CSS animations may be throttled
// in background webviews; an interval always advances).
var tickerX = 0, tickerTimer = null;
function startTicker(){
  var strip = $('tickerStrip'), inner = $('ticker');
  if(!strip || !inner) return;
  if(tickerTimer){ clearInterval(tickerTimer); tickerTimer = null; }
  tickerX = strip.clientWidth;
  tickerTimer = setInterval(function(){
    tickerX -= 1;
    var w = inner.offsetWidth || 300;
    if(tickerX + w < 0) tickerX = strip.clientWidth; // wrap: start again from the right
    inner.style.left = tickerX + 'px';
  }, 30);
}

async function regenerate(){
  $('statusText').innerText = 'Regenerating scenario...';
  await api('/api/seed', {});
  resetInView();
  resetStoreView();
  resetMccView();
  resetDistView();
  resetTopView();
  resetQcView();
  await refresh();
}
async function runPlanner(){
  $('statusText').innerText = 'Running MCC planner...';
  await api('/api/agent/plan', {});
  await refresh();
}
// Toolbar goal bar: every question flows through the SAME shared thread as
// PSA Intelligence, so the user can keep asking from the toolbar (multi-turn),
// see what they just asked, and continue the conversation on the intel page.
async function toolbarAsk(){
  var input = $('goalInput');
  var q = (input.value || '').trim();
  if(!q || intelBusy) return;
  intelBusy = true;
  var btn = $('goalRunBtn');
  if(btn) btn.disabled = true;
  input.value = '';
  var history = intelHistory(10); // capture BEFORE pushing the new question
  intelThread.push({role:'user', text:q});
  intelThread.push({role:'assistant', text:'Thinking…', meta:''});
  intelRender();
  intelSave();
  renderGoalResult(q, 'Thinking…', null);
  try{
    var r = await api('/api/intel/ask', {question:q, context:{sim_now: state.sim_now}, history: history});
    var meta = 'brain: <b>' + intelEscape(r.brain) + '</b> · autonomy: <b>' + intelEscape(r.autonomy) + '</b>' +
      (r.error ? ' · <font color="#F28B82">error: ' + intelEscape(r.error) + '</font>' : '');
    var pending = (r.events || []).filter(function(e){ return e && e.tool && e.kind !== 'executed'; });
    var answer = intelPolish(r.answer || 'No answer returned.');
    lastBrainName = r.brain || lastBrainName;
    intelThread[intelThread.length - 1] = {role:'assistant', text:answer, meta:meta, pending: pending};
    intelRender();
    intelSave();
    renderGoalResult(q, answer, pending);
  }catch(err){
    intelThread[intelThread.length - 1] = {role:'assistant', text:'Sorry — I could not reach the agent: ' + err.message, meta:''};
    intelRender();
    intelSave();
    renderGoalResult(q, 'Sorry — I could not reach the agent: ' + err.message, null);
  }
  intelBusy = false;
  if(btn) btn.disabled = false;
  if(input) input.focus();
}

// Compact Q&A box under the toolbar so the user sees what they just asked
// without leaving the page; the full conversation lives in PSA Intelligence.
function renderGoalResult(q, answer, pending){
  var box = $('agentGoalResult');
  if(!box) return;
  box.style.display = '';
  var mi = intelThread.length - 1;
  var html = '<table class="grid" style="width:100%">' +
    '<tr><td class="metric" width="80">You</td><td>' + intelFmt(intelEscape(q || '')) + '</td></tr>' +
    '<tr><td class="metric" width="80">Agent</td><td>' + intelFmt(intelEscape(intelStripProposalProse(answer, pending))) + '</td></tr>' +
    (pending && pending.length ?
      '<tr><td class="metric" valign="top">Action</td><td>' + intelProposalBlocks(pending, mi) +
      ' <span style="font-size:9px;color:#404040">approve to apply the plan change.</span></td></tr>' : '') +
    '<tr><td class="metric">Continue</td><td>Ask another question above, or open <a href="#" onclick="showView(\'intel\');return false;">PSA Intelligence</a> to see the full conversation.</td></tr>' +
    '</table>';
  box.innerHTML = html;
}

// After an approval/rejection, keep the toolbar box in sync with the thread.
function renderGoalFromThread(){
  var box = $('agentGoalResult');
  if(!box || box.style.display === 'none') return;
  var msgs = intelThread, u = -1, a = -1;
  for(var i = msgs.length - 1; i >= 0; i--){
    if(msgs[i].role === 'assistant'){ if(a < 0) a = i; }
    else if(msgs[i].role === 'user'){ u = i; break; }
  }
  if(u < 0 || a < 0) return;
  renderGoalResult(msgs[u].text, msgs[a].text, msgs[a].pending || null);
}
// Ask the exception agent: jump to PSA Intelligence with the question pre-filled
// and sent, so the user sees the agent's own written findings.
async function askExceptionAgent(){
  showView('intel');
  var input = $('intelAsk');
  if(input){
    input.value = 'What needs attention right now? Show me the current findings.';
    intelSend();
  }
}
async function clearTrace(){
  await api('/api/trace/clear', {});
  await refresh();
}

// Clicking anywhere that is not a selectable/interactive element unselects the
// current container and restores the page's original arrangement.
document.addEventListener('click', function(e){
  var t = e.target;
  if(!t || !t.closest) return;
  if(t.closest('a, button, input, select, [onclick]')) return; // interactive elements handle their own clicks
  var changed = false;
  if(selected){ selected = null; selectedBerth = null; changed = true; }
  if(distSel){ distSel = null; changed = true; }
  if(mccSel){ mccSel = null; changed = true; }
  if(topSel){ topSel = null; changed = true; }
  if(qcSel){ qcSel = null; changed = true; }
  if(pschSelRack || pschSelBin){ pschSelRack = null; pschSelBin = null; changed = true; }
  if(!changed) return;
  setDetail(false);
  if(view === 'in'){ renderIncoming(); renderMap(); renderInspector(); }
  else if(view === 'store'){ renderPsch(); }
  else if(view === 'mcc'){ renderMccList(); renderMccMap(); renderMccInspector(); }
  else if(view === 'dist'){ renderDistList(); renderDistBoard(); renderDistInspector(); }
  else if(view === 'top'){ renderTopList(); renderTopBoard(); renderTopInspector(); }
  else if(view === 'qc'){ renderQcList(); renderQcBoard(); renderQcInspector(); }
});

loadFlashSettings();
applyFlashSettings();
refresh().catch(function(e){ $('statusText').innerText = 'Error: ' + e.message; });
setInterval(function(){ refresh().catch(function(){}); }, 8000);
startTicker();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PSA-MCCControlTower/3.0"

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self) -> None:
        page = (
            PAGE.replace("{{BERTHS_JSON}}", json.dumps(BERTHS))
            .replace("{{PSCH_CSS}}", FACILITY_CSS)
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._html()
        elif path == "/api/state":
            self._json(build_state())
        elif path == "/api/health":
            self._json({"ok": True})
        elif path == "/map.png":
            self._map_png()
        elif path.startswith("/static/"):
            self._static(path)
        else:
            self._json({"error": "not found"}, 404)

    def _map_png(self) -> None:
        """Serve the berth-plan graphic (Map.png at the project root)."""
        if not MAP_FILE.is_file():
            self._json({"error": "map file missing: Map.png"}, 404)
            return
        body = MAP_FILE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        """Serve a file from the static/ directory (e.g. the berth-plan map)."""
        name = urlsplit(path).path.removeprefix("/static/")
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = target.read_bytes()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                  ".svg": "image/svg+xml"}.get(
            target.suffix.lower(), "application/octet-stream"
        )
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        body = self._read_body()
        try:
            if path == "/api/seed":
                seed(seed=SEED, n_containers=N_CONTAINERS, db_path=DB_PATH, sim_now=sim_now())
            elif path == "/api/agent/plan":
                _run_planner()
            elif path == "/api/agent/run":
                goal = str(body.get("goal", "")).strip()
                context = body.get("context") or {}
                if not goal:
                    self._json({"error": "empty goal"}, 400)
                    return
                rt = AgentRuntime()
                result = rt.run(goal, context)
                self._json(
                    {
                        "ok": result.ok,
                        "summary": result.summary,
                        "error": result.error,
                        "events": result.events,
                        "brain": rt.brain.name,
                        "autonomy": rt.autonomy,
                    }
                )
                return
            elif path == "/api/agent/exceptions":
                from agents.exception import ExceptionBrain  # lazy

                rt = AgentRuntime(brain=ExceptionBrain())
                result = rt.run("exception watch", body.get("context") or {})
                self._json(
                    {
                        "ok": result.ok,
                        "answer": result.summary,
                        "exceptions": result.events,
                        "brain": rt.brain.name,
                        "autonomy": rt.autonomy,
                    }
                )
                return
            elif path == "/api/agent/approve":
                tool = str(body.get("tool", ""))
                args = body.get("args") or {}
                if not tool:
                    self._json({"error": "tool required"}, 400)
                    return
                result = AgentRuntime().approve(tool, args)
                self._json({"ok": True, "result": result})
                return
            elif path == "/api/agent/reject":
                tool = str(body.get("tool", ""))
                args = body.get("args") or {}
                store.record_event(
                    "operator",
                    "rejected",
                    {"tool": tool, "args": json.dumps(args, default=str)[:300]},
                    DB_PATH,
                )
                self._json({"ok": True})
                return
            elif path == "/api/exception/action":
                # One-click fix from the Attention needed panel: run the
                # suggestion through the same permission gate as any agent
                # proposal, so advisory mode records approval_required and the
                # human approves/rejects inline (identical lifecycle in the trace).
                tool = str(body.get("tool", ""))
                args = body.get("args") or {}
                if not tool:
                    self._json({"error": "tool required"}, 400)
                    return
                try:
                    result = AgentRuntime().gate(tool, args)
                    self._json({"ok": True, "result": result, "tool": tool, "args": args})
                except Exception as ex:  # noqa: BLE001 - surface tool errors to the page
                    self._json({"ok": False, "error": str(ex)}, 400)
                return
            elif path == "/api/trace/clear":
                store.clear_trace(DB_PATH)
            elif path == "/api/berth/inspect":
                berth_id = str(body.get("berth", ""))
                store.record_event(
                    "operator", "berth_inspected", {"berth": berth_id}, DB_PATH,
                )
            elif path == "/api/psch/inspect":
                aisle = str(body.get("aisle", ""))
                store.record_event(
                    "operator", "psch_rack_inspected", {"aisle": aisle}, DB_PATH,
                )
            elif path == "/api/intel/ask":
                question = str(body.get("question", "")).strip()
                context = body.get("context") or {}
                if not question:
                    self._json({"error": "empty question"}, 400)
                    return
                # Conversation context: the last few thread messages (role/text),
                # sanitised so the brain only ever sees well-formed pairs.
                raw_history = body.get("history") or []
                history = []
                if isinstance(raw_history, list):
                    for m in raw_history[-12:]:
                        if (
                            isinstance(m, dict)
                            and m.get("role") in ("user", "assistant")
                            and m.get("text")
                        ):
                            history.append({"role": m["role"], "text": str(m["text"])[:2000]})
                rt = AgentRuntime(brain=default_intel_brain())
                result = rt.run(question, context, history=history)
                self._json(
                    {
                        "ok": result.ok,
                        "answer": result.summary,
                        "error": result.error,
                        "events": result.events,
                        "brain": rt.brain.name,
                        "autonomy": rt.autonomy,
                    }
                )
                return
            elif path == "/api/bayplan":
                cid = str(body.get("container_id", ""))
                self._json({"html": _bayplan_for_container(cid)})
                return
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json(build_state())
        except Exception as exc:  # surface the error to the UI
            self._json({"error": str(exc)}, 500)

    def log_message(self, fmt, *args) -> None:  # keep the console quiet-ish
        pass


def main() -> None:
    store.init_db(DB_PATH)  # create + migrate (adds new columns to existing DBs)
    seed_if_empty(DB_PATH)
    host = os.environ.get("PSA_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"PSA Control Tower — Port - PSCH Integration (classic UI) running on http://127.0.0.1:{PORT}", flush=True)
    print(f"LAN access: http://<your-ip>:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
