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

from config import DB_PATH, N_CONTAINERS, SEED, SIM_NOW
from data import store
from data.facility import build_psch_space
from data.seed import seed, seed_if_empty
from agents import mcc_planner
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


def build_state() -> dict:
    """Assemble the full, JSON-serialisable view state for the frontend."""
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
    kpis = compute_kpis(containers, plans, outbounds, yard, drayage)

    # PSCH space-utilisation view: every rack, bin and staging lane with the
    # agent's assignments colour-coded by the container's journey status.
    shipments = store.get_shipments(DB_PATH)
    psch = build_psch_space(plans, shipments, outbounds)

    vessel_by_id = {v["voyage_id"]: v for v in vessels}
    plan_by_id = {p["container_id"]: p for p in plans}
    stowage = store.get_vessel_stowage(DB_PATH)
    stow_by = {}
    for s in stowage:
        stow_by.setdefault((s["vessel_id"], s["bay"]), []).append(s)

    inbound = []
    for c in containers:
        if c["cargo_flag"] != "deconsolidation_required":
            continue
        p = plan_by_id.get(c["container_id"])
        if p is None:
            continue
        v = vessel_by_id.get(p["carrying_vessel_id"], {})
        status = mcc_planner.journey_status(p)
        hours_until = (p["psch_receipt_eta"] - SIM_NOW).total_seconds() / 3600
        cells = stow_by.get((p["carrying_vessel_id"], c.get("stow_bay")), [])
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
            "reasoning": p["reasoning"],
        }
        entry["bay_cells"] = [
            {
                "stack": s["stack"],
                "tier": s["tier"],
                "container_id": s["container_id"],
                "size": s["size"],
                "destination": s["destination"],
                "weight_t": s["weight_t"],
                "is_mcc": s["is_mcc"],
                "type": s["cargo_type"],
            }
            for s in cells
        ]
        entry["bayplan_html"] = build_bay_plan(entry, clickable=True)
        inbound.append(entry)
    inbound.sort(key=lambda x: x["psch_receipt_eta"])

    outbound = []
    for o in outbounds:
        v = vessel_by_id.get(o["bound_vessel_id"], {})
        status = mcc_planner.outbound_status(o)
        hours = (o["eta_loading_area"] - SIM_NOW).total_seconds() / 3600
        etd = o.get("vessel_etd")
        loading_end = etd - timedelta(hours=2) if etd else None
        cutoff = etd - timedelta(hours=4) if etd else None
        hours_dep = (etd - SIM_NOW).total_seconds() / 3600 if etd else None
        hours_end = (loading_end - SIM_NOW).total_seconds() / 3600 if loading_end else None
        cells = stow_by.get((o["bound_vessel_id"], o.get("stow_bay")), [])
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
        entry["bay_cells"] = [
            {
                "stack": s["stack"], "tier": s["tier"], "container_id": s["container_id"],
                "size": s["size"], "destination": s["destination"], "weight_t": s["weight_t"],
                "is_mcc": s["is_mcc"], "type": s["cargo_type"],
            }
            for s in cells
        ]
        entry["bayplan_html"] = (
            build_bay_plan(
                entry, clickable=False,
                target=(entry["stow_row"], entry["stow_tier"]),
            )
            if entry["stow_row"] else ""
        )
        outbound.append(entry)
    outbound.sort(key=lambda x: x["eta_loading_area"])

    return {
        "sim_now": SIM_NOW.isoformat(),
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
        "trace": [
            {"ts": _iso(e["ts"]), "actor": e["actor"], "event": e["event"], "detail": e["detail"]}
            for e in trace
        ],
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
  .banner marquee { background:#000080; color:#FFFF00; font-weight:bold; margin-top:3px; }

  .navbar { width:100%; background:#C0C0C0; border-bottom:2px groove #808080; }
  .navbar td { padding:3px 10px; border-right:2px groove #808080; }
  .navlink a { font-weight:bold; }
  .navlink a.active { color:#000000; }

  /* MCC nav group: one nav item holding the Inbound / Outbound sub-pages */
  .navlink.nav-group { position:relative; }
  .navlink.nav-group > a .arrow { font-size:9px; }
  .nav-sub { display:none; position:absolute; top:100%; left:0; z-index:60;
             background:#C0C0C0; border:2px solid #000080; min-width:150px; }
  .navlink.nav-group:hover .nav-sub,
  .navlink.nav-group:focus-within .nav-sub { display:block; }
  .nav-sub a { display:block; padding:3px 16px; font-weight:bold; white-space:nowrap; }
  .nav-sub a:hover { background:#000080; color:#FFFFFF; }
  .nav-sub a.active { background:#000080; color:#FFFFFF; }

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
  #incomingDrop, #outboundDrop { position:absolute; top:100%; left:0; width:420px; max-width:80vw; z-index:80;
                  display:none; max-height:420px; overflow:auto; background:#FFFFFF;
                  border:2px solid #000080; }
  #incomingDrop .mp-item, #outboundDrop .mp-item { margin-bottom:0; border-left:none; border-right:none; }
  #incomingDrop .mp-item:first-child, #outboundDrop .mp-item:first-child { border-top:1px solid #808080; }
  #incomingDrop .no-match, #outboundDrop .no-match { padding:4px; font-size:10px; color:#404040; }
  #incomingDrop .drop-foot, #outboundDrop .drop-foot { padding:2px 4px; background:#E8E8E8; color:#404040; font-size:9px; }
  .incoming-selinfo { margin-top:2px; font-size:10px; color:#404040; word-wrap:break-word; }

  /* Reflow: when a container detail is open the left column collapses so the
     map slides left and the ship-tracker inspector widens (no window resize). */
  table.cols { table-layout:fixed; }
  td.incoming-col { width:30%; }
  td.map-col     { width:42%; }
  td.inspector-col { width:28%; }
  td.incoming-col, td.map-col, td.inspector-col { transition:width 0.25s ease; }
  #view-mcc.detail-open td.incoming-col, #view-out.detail-open td.incoming-col { width:13%; }
  #view-mcc.detail-open td.map-col,     #view-out.detail-open td.map-col     { width:44%; }
  #view-mcc.detail-open td.inspector-col, #view-out.detail-open td.inspector-col { width:43%; }

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

  {{PSCH_CSS}}
</style>
</head>
<body>

<table class="banner"><tr><td>
  <font size="5" color="#FFFFFF"><b>PSA Control Tower</b></font>
  <font size="2" color="#FFFFFF">&nbsp;·&nbsp;Port - PSCH Integration</font>
  <div><marquee id="ticker">*** LOADING ***</marquee></div>
</td></tr></table>

<table class="navbar"><tr>
  <td class="navlink nav-group">
    <a id="nav-mcc" href="#" onclick="showView('mcc');return false;">MCC <span class="arrow">▾</span></a>
    <div class="nav-sub">
      <a id="nav-inbound" href="#" onclick="showView('mcc');return false;">Inbound</a>
      <a id="nav-outbound" href="#" onclick="showView('out');return false;">Outbound</a>
    </div>
  </td>
  <td class="navlink"><a id="nav-plan" href="#" onclick="showView('plan');return false;">PSCH Plan</a></td>
  <td class="navlink"><a id="nav-psch" href="#" onclick="showView('psch');return false;">PSCH Space</a></td>
  <td class="navlink"><a id="nav-tower" href="#" onclick="showView('tower');return false;">Control Tower</a></td>
  <td class="navlink"><a id="nav-trace" href="#" onclick="showView('trace');return false;">Execution Trace</a></td>
  <td align="right"><b>SIM CLOCK:</b> <span id="simClock">—</span></td>
</tr></table>

<table width="100%" cellpadding="3"><tr>
  <td><input type="button" class="btn" value="&#8635; Regenerate" onclick="regenerate()"></td>
  <td><input type="button" class="btn" value="&#9654; Run MCC Planner" onclick="runPlanner()"></td>
  <td id="agentBadge" nowrap>&nbsp;</td>
  <td align="right" nowrap>seed <span id="seedLabel">—</span></td>
</tr></table>

<!-- ================= MCC TRACKER ================= -->
<div id="view-mcc">
  <h3>MCC Tracker — Inbound Containers &amp; Vessel Tracking</h3>
  <div class="kpi-strip" id="kpiStrip"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Inbound Containers (MCC cargo)</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="incomingSearch" class="searchbox"
                 placeholder="Search container / vessel / status…" autocomplete="off"
                 onfocus="openIncomingDrop()" onclick="openIncomingDrop()"
                 oninput="filterIncoming()" onkeydown="incomingKey(event)"
                 onblur="closeIncomingDropSoon()">
          <div id="incomingDrop"></div>
        </div>
        <div id="berthInfo" style="margin-top:6px;min-height:36px">Click a berth rectangle on the map to inspect it — docked, inbound, or available.</div>
        <div id="incomingSelInfo" class="incoming-selinfo"></div>
      </fieldset>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Viewer — Tuas Berth Plan (static map · not GPS tracking)</legend>
        <div id="facilityMap" style="position:relative;width:100%;max-width:560px;aspect-ratio:1;min-height:380px;border:1px solid #000000;overflow:hidden;background:#D9D9C0"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Ship Tracker — Container Detail</legend>
        <div id="inspector" style="min-height:420px">Select an inbound container on the left to open its ship-tracker detail.</div>
      </fieldset>
    </td>
  </tr></table>
</div>

<!-- ================= OUTBOUND TRACKER ================= -->
<div id="view-out" style="display:none">
  <h3>Outbound MCC Tracker — Consolidation Containers &amp; Loading Vessels</h3>
  <div class="kpi-strip" id="outKpiStrip"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Outbound Containers (consolidated MCC)</legend>
        <div class="incoming-search-wrap">
          <input type="text" id="outboundSearch" class="searchbox"
                 placeholder="Search outbound / destination / vessel…" autocomplete="off"
                 onfocus="openOutDrop()" onclick="openOutDrop()" oninput="filterOut()"
                 onkeydown="outKey(event)" onblur="closeOutDropSoon()">
          <div id="outboundDrop"></div>
        </div>
        <div id="berthInfoOut" style="margin-top:6px;min-height:36px">Select an outbound container on the left to highlight the berth of the vessel it sails on.</div>
        <div id="outboundSelInfo" class="incoming-selinfo"></div>
      </fieldset>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>Viewer — Tuas Berth Plan (loading vessels)</legend>
        <div id="facilityMapOut" style="position:relative;width:100%;max-width:560px;aspect-ratio:1;min-height:380px;border:1px solid #000000;overflow:hidden;background:#D9D9C0"></div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Outbound Ship Tracker — Loading Detail</legend>
        <div id="inspectorOut" style="min-height:420px">Select an outbound container on the left to open its loading detail.</div>
      </fieldset>
    </td>
  </tr></table>
</div>

<!-- ================= PSCH PLAN ================= -->
<div id="view-plan" style="display:none">
  <h3>PSCH Plan — Agent-prepared receiving, putaway &amp; consolidation</h3>
  <div class="kpi-strip" id="planSummary"></div>
  <table width="100%" cellspacing="4" class="cols"><tr>
    <td width="30%" valign="top" class="incoming-col">
      <fieldset><legend>Inbound Containers (MCC cargo)</legend>
        <div id="planIncoming" style="max-height:360px;overflow:auto">Loading containers…</div>
      </fieldset>
      <div id="planSelInfo" class="incoming-selinfo">Select a container on the left to open its receiving &amp; putaway plan.</div>
    </td>
    <td width="42%" valign="top" class="map-col">
      <fieldset><legend>PSCH Floorplan — receiving, storage &amp; dispatch (schematic)</legend>
        <div class="psch-plan">
          <div class="fp-site">
            <span>PSA SUPPLY CHAIN HUB — TUAS</span>
            <span class="fp-road">Tuas South Ave 5</span>
            <span class="fp-gate">Gate 1</span>
            <span class="fp-gate">Gate 2</span>
          </div>
          <div class="fp-yard">YARD — truck marshalling · container staging</div>
          <div class="fp-building">
            <div class="fp-zone fp-in">
              <div class="fp-zone-title">INBOUND</div>
              <div class="fp-sub">Receiving docks</div>
              <div class="fp-lanes">10 lanes</div>
              <div class="fp-sub">Receiving areas</div>
              <div class="fp-lanes">R1–R4</div>
              <div class="fp-sub">Staging</div>
            </div>
            <div class="fp-zone fp-store">
              <div class="fp-zone-title">STORAGE — AMBIENT</div>
              <div class="fp-sub">Selective racking</div>
              <div class="fp-lanes">Aisles 1–21</div>
              <div class="fp-haz">Aisle 21 — DG segregated</div>
            </div>
            <div class="fp-zone fp-cold">
              <div class="fp-zone-title">COLD ROOM</div>
              <div class="fp-sub">Chilled / frozen</div>
              <div class="fp-lanes">Aisles 22–24</div>
            </div>
            <div class="fp-zone fp-out">
              <div class="fp-zone-title">OUTBOUND</div>
              <div class="fp-sub">Pick &amp; consolidation</div>
              <div class="fp-sub">Releasing docks</div>
              <div class="fp-lanes">26 lanes</div>
            </div>
          </div>
          <div class="fp-support">SUPPORT — office · WMS · amenities · battery charging · maintenance</div>
          <div class="fp-flow">FLOW — INBOUND ⟶ RECEIVING ⟶ PUTAWAY ⟶ STORAGE ⟶ PICK ⟶ RELEASING ⟶ OUTBOUND</div>
        </div>
      </fieldset>
    </td>
    <td width="28%" valign="top" class="inspector-col">
      <fieldset><legend>Container Plan Detail</legend>
        <div id="planInspector" style="min-height:360px">Select an inbound container on the left to open its plan detail.</div>
      </fieldset>
    </td>
  </tr></table>
  <h4>Receiving &amp; robot putaway plan (per inbound container)</h4>
  <table class="grid">
    <thead><tr><th>Container</th><th>Status</th><th>Receipt ETA</th><th>Receiving area</th><th>Staging</th><th>Move to bin</th><th>Bin / Robot</th><th>Pallet pick</th><th>Release lane</th><th>Consol. group</th></tr></thead>
    <tbody id="planRows"></tbody>
  </table>
  <h4>Outbound consolidation schedule</h4>
  <table class="grid">
    <thead><tr><th>Outbound ctn</th><th>Dest</th><th>Sources</th><th>Bound vessel</th><th>Berth</th><th>Vessel ETD</th><th>Stow cell</th><th>Stuffing window</th><th>Lane release</th><th>ETA loading area</th><th>Status</th></tr></thead>
    <tbody id="outboundRows"></tbody>
  </table>
</div>

<!-- ================= PSCH SPACE ================= -->
<div id="view-psch" style="display:none">
  <h3>PSCH Space — racking, bins &amp; staging lanes (AMBIENT + COLD ROOM)</h3>
  <div class="kpi-strip" id="pschKpis"></div>
  <table width="100%" cellspacing="4" style="table-layout:fixed"><tr>
    <td width="58%" valign="top">
      <div class="psch-sec-head">INBOUND</div>
      <table width="100%" cellspacing="4" style="table-layout:fixed"><tr>
        <td width="50%" valign="top">
          <fieldset><legend>Inbound Receiving Lanes</legend>
            <div id="pschRcvLanes">Loading receiving lanes…</div>
          </fieldset>
        </td>
        <td width="50%" valign="top">
          <fieldset><legend>Inbound Containers</legend>
            <div id="pschIncoming" style="max-height:250px;overflow:auto"></div>
          </fieldset>
        </td>
      </tr></table>
      <div class="psch-sec-head out">OUTBOUND</div>
      <div id="pschOutboundSec">Loading outbound…</div>
    </td>
    <td width="42%" valign="top">
      <fieldset><legend>Ship Tracker — Container Detail</legend>
        <div id="pschInspector" style="min-height:280px;overflow-x:auto">Select a container to open its ship-tracker detail.</div>
      </fieldset>
      <fieldset style="margin-top:4px"><legend>Agent Reasoning</legend>
        <div id="pschReasoning" style="min-height:80px;overflow-x:auto">Select a container to see the agent reasoning for its plan.</div>
      </fieldset>
    </td>
  </tr></table>
  <div style="margin-top:4px"><fieldset><legend>Aisle Details</legend>
    <div id="pschRackDetail" style="min-height:150px;overflow-x:auto">Select an aisle in the facility view to inspect its bins.</div>
  </fieldset></div>
  <div style="font-size:9px;color:#404040;margin-top:4px">Conventions: bins are named <b>AISLE-LEVEL-BAY</b> (e.g. 1-12-2A = Aisle 1, Level 12, Bay 2A — the box letter is written directly on the bay number) as in a distribution centre; aisles numbered 1-24 (ambient 1-21, cold room 22-24; aisle 21 segregated for dangerous goods); every aisle has 12 levels (height), each level has 3 bays and every bay has boxes A/B/C. <b>Slotting agent</b>: the height (level) is optimised from when the cargo will be released — soon-to-release cargo is stored at floor level for fast robot retrieval, slower movers higher up. Yellow bin = reserved by the agent before arrival · green bin = cargo arrived · grey = empty. Staging lanes: receiving lanes numbered 1-10 at the inbound (receiving) area, releasing lanes numbered 1-26 at the outbound (releasing) area.</div>
</div>

<!-- ================= CONTROL TOWER ================= -->
<div id="view-tower" style="display:none">
  <h3>Control Tower KPIs — MCC cargo pipeline</h3>
  <div class="kpi-strip" id="kpiRows"></div>
  <table width="100%"><tr>
    <td width="50%" valign="top"><fieldset><legend>Inbound journey stages</legend><table class="grid"><tbody id="journeyRows"></tbody></table></fieldset></td>
    <td width="50%" valign="top"><fieldset><legend>Outbound consolidation status</legend><table class="grid"><tbody id="outboundStatusRows"></tbody></table></fieldset></td>
  </tr></table>
</div>

<!-- ================= TRACE ================= -->
<div id="view-trace" style="display:none">
  <h3>Execution Trace</h3>
  <table width="100%"><tr><td align="right"><input type="button" class="btn" value="Clear Trace" onclick="clearTrace()"></td></tr></table>
  <table class="grid">
    <thead><tr><th>Time</th><th>Actor</th><th>Event</th><th>Detail</th></tr></thead>
    <tbody id="traceRows"></tbody>
  </table>
</div>

<table class="statusbar"><tr>
  <td id="statusText" width="80%">Connecting...</td>
  <td id="lastUpdated" align="right">—</td>
</tr></table>

<div class="footer">© 2026 PSA Control Tower · Best viewed at 1024×768 in Netscape 4.0+ · All data synthetic</div>

<script>
var state = null;
var view = 'mcc';
var lastView = null;
var selected = null; // {kind:'incoming'|'outbound'|'berth', id:...}
var selectedBerth = null;
var outQuery = '';
var outSelected = null;   // outbound tracker selection {id}
var outBerth = null;      // berth clicked in the outbound tracker

function $(id){ return document.getElementById(id); }

function setDetail(open){
  var el = document.getElementById('view-' + view);
  if(el) el.classList.toggle('detail-open', !!open);
}

// Restore a tracker sub-page to its original arrangement (columns at their
// normal widths, nothing selected, search cleared, dropdown closed). Called
// when the user navigates back to the page so stale detail-open reflow and
// selections do not survive a page switch.
function resetMccView(){
  selected = null;
  selectedBerth = null;
  closeIncomingDrop();
  $('incomingSearch').value = '';
  incomingQuery = '';
  setDetail(false);
}
function resetOutView(){
  outSelected = null;
  outBerth = null;
  closeOutDrop();
  $('outboundSearch').value = '';
  outQuery = '';
  setDetail(false);
}

function isoDT(iso){ return iso ? iso.replace('T',' ').slice(0,16) : '—'; }
function isoTime(iso){ return iso ? iso.slice(11,16) : '—'; }

async function api(path, body){
  var opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
  var resp = await fetch(path, opt);
  if(!resp.ok) throw new Error('HTTP ' + resp.status);
  return resp.json();
}

async function refresh(){
  state = await api('/api/state');
  render();
}

function render(){
  renderClock();
  renderBadge();
  renderKpiStrip();
  renderIncoming();
  renderMap();
  renderInspector();
  renderOutKpis();
  renderOutList();
  renderOutMap();
  renderOutInspector();
  renderPlan();
  renderPsch();
  renderTower();
  renderTrace();
  renderTicker();
  renderStatus();
  showView(view);
}

function renderClock(){
  $('simClock').innerText = isoDT(state.sim_now) + 'Z';
  $('seedLabel').innerText = state.seed;
}

function renderBadge(){
  $('agentBadge').innerHTML = '<b>PLANNER:</b> <font color="#008000">rule-based agent (deterministic, zero-cost)</font>';
}

function badge(status){
  var map = {
    'En Route (Sea)':'st-sea', 'Unloaded':'st-unload', 'Depot':'st-depot',
    'En Route (Road)':'st-road', 'Arrived':'st-arr',
    'loaded':'st-loaded', 'staged':'st-staged', 'released':'st-road', 'in_transit':'st-road'
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
    return '<span class="kpi-cell"><span class="k">' + c[0] + '</span> <span class="metric">' + c[1] + '</span></span>';
  }).join('');
}

function renderKpiStrip(){
  var k = state.kpis;
  var cells = [
    ['At Sea', k.journey_counts['En Route (Sea)']],
    ['Unloaded', k.journey_counts['Unloaded']],
    ['Depot', k.journey_counts['Depot']],
    ['On Road', k.journey_counts['En Route (Road)']],
    ['Arrived @ PSCH', k.arrived_at_psch],
    ['Outbound Loaded', k.loaded_outbound],
    ['Avg pipeline', k.avg_pipeline_h + ' h'],
    ['Recv. areas', k.receiving_areas_opened],
    ['Bin util', k.bin_util + '%']
  ];
  $('kpiStrip').innerHTML = kpiStripHtml(cells);
}

var incomingQuery = '';

function incomingMatches(m, q){
  q = q.toLowerCase();
  var hay = (m.container_id + ' ' + (m.size || '') + ' ' + (m.type || '') + ' ' + m.vessel_name + ' ' +
             m.vessel_id + ' ' + m.status + ' ' + (m.berth_id || '') + ' ' + (m.bay_label || '') + ' ' +
             (m.destination || '')).toLowerCase();
  return q === '' || hay.indexOf(q) !== -1;
}

function renderIncoming(){
  var q = incomingQuery;
  var items = state.inbound.filter(function(m){ return incomingMatches(m, q); });
  var html = '';
  items.forEach(function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (m.status === 'Arrived') ? 'arrived' : countdownHtml(m.hours_until);
    html += '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickIncoming(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + m.size + ' · ' + m.type + '<br>' +
      badge(m.status) + ' &nbsp;via <b>' + m.vessel_name + '</b><br>' +
      'PSCH receipt ETA: ' + isoDT(m.psch_receipt_eta) + ' (' + etaTxt + ')' +
      '</button>';
  });
  if(!items.length) html = '<div class="no-match">No MCC containers match &lsquo;' + (q || '') + '&rsquo;.</div>';
  html += '<div class="drop-foot">' + items.length + ' of ' + state.inbound.length + ' MCC containers in the pipeline</div>';
  $('incomingDrop').innerHTML = html;
  var info = 'Click here to search ' + state.inbound.length + ' inbound MCC containers';
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

// ---- Map -------------------------------------------------------------------
var BERTHS = {{BERTHS_JSON}};

function renderMap(){
  var html = '<img src="/map.png" width="100%" height="100%" style="position:absolute;left:0;top:0;display:block" alt="Tuas berth plan">';
  var highlightBerth = null;
  if(selected && selected.kind === 'outbound'){
    state.outbound.forEach(function(o){ if(o.container_id === selected.id && o.berth_id) highlightBerth = o.berth_id; });
  } else if(selected && selected.kind === 'incoming'){
    state.inbound.forEach(function(m){ if(m.container_id === selected.id && m.berth_id) highlightBerth = m.berth_id; });
  }
  state.berths.forEach(function(b){
    var v = b.vessel;
    var cls = v ? (v.status === 'docked' ? 'berth occ' : 'berth inb') : 'berth free';
    if(selectedBerth === b.id) cls += ' sel';
    else if(highlightBerth === b.id) cls += ' hl';
    var title = b.id + ': ' + (v ? v.vessel_name + ' (' + v.status + ')' : 'berth available');
    html += '<div class="' + cls + '" style="left:' + b.x + '%;top:' + b.y + '%;width:' + b.w + '%;height:' + b.h + '%" onclick="selectBerth(\'' + b.id + '\')" title="' + title + '">' +
      '<font>' + b.id + '</font></div>';
  });
  var d = state.drayage;
  html +=
    '<div class="map-kpi" style="left:2px;top:2px">Yard util <b>' + state.kpis.avg_yard_util + '%</b></div>' +
    '<div class="map-kpi" style="left:2px;top:22px">Drayage <b>' + d.available_trucks + '/' + d.total_trucks + '</b> PM</div>' +
    '<div style="position:absolute;right:2px;bottom:2px;font-size:8px;color:#FFFFFF;background:#000080;padding:1px 3px">Static berth plan — no GPS tracking</div>';
  $('facilityMap').innerHTML = html;
  renderBerthInfo();
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
function renderBerthInfo(){
  var b = null;
  if(selected && selected.kind === 'berth'){
    state.berths.forEach(function(x){ if(x.id === selectedBerth) b = x; });
  } else if(selected && (selected.kind === 'incoming' || selected.kind === 'outbound')){
    var hl = null;
    var arr = (selected.kind === 'incoming') ? state.inbound : state.outbound;
    arr.forEach(function(e){ if(e.container_id === selected.id && e.berth_id) hl = e.berth_id; });
    state.berths.forEach(function(x){ if(x.id === hl) b = x; });
  }
  if(!b){ $('berthInfo').innerHTML = 'Click a berth rectangle on the map to inspect it — docked, inbound, or available.'; return; }
  var v = b.vessel;
  var html = '<b>Berth ' + b.id + '</b> (Pier ' + b.pier + '): ';
  if(v && v.status === 'docked'){
    html += '<font color="#000080"><b>OCCUPIED</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + '), alongside since ' + (v.eta || '').slice(0,16) + ', ETD ' + (v.etd || '—').slice(0,16) + ', ' + v.moves_planned + ' TEU to work.';
  } else if(v && v.status === 'inbound'){
    html += '<font color="#B07000"><b>BOOKED</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + ') inbound, ETA ' + (v.eta || '').slice(0,16) + ', ' + v.moves_planned + ' TEU planned, ' + v.distance_nm + ' nm out at ' + v.speed_knots + ' kn.';
  } else {
    html += '<font color="#008000"><b>AVAILABLE</b></font> — no vessel assigned.';
  }
  $('berthInfo').innerHTML = html;
}

// ---- Outbound MCC Tracker ---------------------------------------------------
function outMatches(o, q){
  q = q.toLowerCase();
  var hay = (o.container_id + ' ' + (o.size || '') + ' ' + o.destination + ' ' + o.status + ' ' +
             o.bound_vessel_name + ' ' + o.bound_vessel_id + ' ' + (o.berth_id || '') + ' ' +
             (o.stow_position || '')).toLowerCase();
  return q === '' || hay.indexOf(q) !== -1;
}

function renderOutList(){
  var q = outQuery;
  var items = state.outbound.filter(function(o){ return outMatches(o, q); });
  var html = '';
  items.forEach(function(o){
    var cls = (outSelected && outSelected.id === o.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (o.status === 'loaded') ? 'loaded' : countdownHtml(o.hours_to_loading);
    html += '<button type="button" class="' + cls + '" onmousedown="event.preventDefault()" onclick="pickOutbound(\'' + o.container_id + '\')">' +
      '<span class="cid">' + o.container_id + '</span> · ' + (o.size || '40HC') + ' → ' + o.destination + ' &nbsp;' + badge(o.status) + '<br>' +
      'Bound: <b>' + o.bound_vessel_name + '</b> · ' + (o.vessel_status === 'inbound' ? 'en route' : 'alongside') + ' · berth ' + (o.berth_id || '—') + '<br>' +
      'Vessel ETD ' + isoDT(o.vessel_etd) + ' · ETA loading area ' + isoDT(o.eta_loading_area) + ' (' + etaTxt + ')' +
      '</button>';
  });
  if(!items.length) html = '<div class="no-match">No outbound containers match &lsquo;' + (q || '') + '&rsquo;.</div>';
  html += '<div class="drop-foot">' + items.length + ' of ' + state.outbound.length + ' outbound consolidation containers</div>';
  $('outboundDrop').innerHTML = html;
  var info = 'Click here to search ' + state.outbound.length + ' outbound MCC containers';
  if(outSelected){
    state.outbound.forEach(function(o){ if(o.container_id === outSelected.id) info = 'Selected: <b>' + o.container_id + '</b> ' + badge(o.status); });
  }
  $('outboundSelInfo').innerHTML = info;
}

function openOutDrop(){ $('outboundDrop').style.display = 'block'; renderOutList(); }
function closeOutDrop(){ $('outboundDrop').style.display = 'none'; }
function closeOutDropSoon(){ setTimeout(function(){ closeOutDrop(); }, 120); }
function filterOut(){ outQuery = $('outboundSearch').value; $('outboundDrop').style.display = 'block'; renderOutList(); }
function outKey(ev){
  if(ev.key === 'Escape'){ closeOutDrop(); $('outboundSearch').blur(); }
  else if(ev.key === 'Enter'){
    var first = state.outbound.filter(function(o){ return outMatches(o, outQuery); })[0];
    if(first) pickOutbound(first.container_id);
  }
}
function pickOutbound(cid){ closeOutDrop(); $('outboundSearch').blur(); $('outboundSearch').value = ''; outQuery = ''; selectOutView(cid); }
function selectOutView(cid){ outSelected = {id:cid}; outBerth = null; setDetail(true); renderOutList(); renderOutMap(); renderOutInspector(); }

function renderOutKpis(){
  var k = state.kpis;
  var cells = [
    ['Outbound total', k.outbound_total],
    ['Staged @ PSCH', k.outbound_counts['staged']],
    ['Lane released', k.outbound_counts['released']],
    ['In transit', k.outbound_counts['in_transit']],
    ['Loaded', k.outbound_counts['loaded']],
    ['Vessels alongside (loading)', state.vessels.filter(function(v){ return v.status === 'docked'; }).length],
    ['Vessels en route', state.vessels.filter(function(v){ return v.status === 'inbound'; }).length]
  ];
  $('outKpiStrip').innerHTML = kpiStripHtml(cells);
}

function renderOutMap(){
  var html = '<img src="/map.png" width="100%" height="100%" style="position:absolute;left:0;top:0;display:block" alt="Tuas berth plan">';
  var highlightBerth = null;
  if(outSelected){
    state.outbound.forEach(function(o){ if(o.container_id === outSelected.id && o.berth_id) highlightBerth = o.berth_id; });
  }
  state.berths.forEach(function(b){
    var v = b.vessel;
    var cls = v ? (v.status === 'docked' ? 'berth occ' : 'berth inb') : 'berth free';
    if(outBerth === b.id) cls += ' sel';
    else if(highlightBerth === b.id) cls += ' hl';
    var title = b.id + ': ' + (v ? v.vessel_name + ' (' + v.status + ')' : 'berth available');
    html += '<div class="' + cls + '" style="left:' + b.x + '%;top:' + b.y + '%;width:' + b.w + '%;height:' + b.h + '%" onclick="selectOutBerth(\'' + b.id + '\')" title="' + title + '">' +
      '<font>' + b.id + '</font></div>';
  });
  var d = state.drayage;
  html +=
    '<div class="map-kpi" style="left:2px;top:2px">Yard util <b>' + state.kpis.avg_yard_util + '%</b></div>' +
    '<div class="map-kpi" style="left:2px;top:22px">Drayage <b>' + d.available_trucks + '/' + d.total_trucks + '</b> PM</div>' +
    '<div style="position:absolute;right:2px;bottom:2px;font-size:8px;color:#FFFFFF;background:#000080;padding:1px 3px">Static berth plan — no GPS tracking</div>';
  $('facilityMapOut').innerHTML = html;
  renderOutBerthInfo();
}
function selectOutBerth(id){
  outBerth = id;
  outSelected = null;
  setDetail(false);
  closeOutDrop();
  api('/api/berth/inspect', {berth: id}); // log the inspection to the trace
  renderOutMap();
  renderOutInspector();
}
function renderOutBerthInfo(){
  var b = null;
  if(outBerth){
    state.berths.forEach(function(x){ if(x.id === outBerth) b = x; });
  } else if(outSelected){
    var hl = null;
    state.outbound.forEach(function(o){ if(o.container_id === outSelected.id && o.berth_id) hl = o.berth_id; });
    state.berths.forEach(function(x){ if(x.id === hl) b = x; });
  }
  if(!b){ $('berthInfoOut').innerHTML = 'Select an outbound container on the left to highlight the berth of the vessel it sails on — docked, inbound, or available.'; return; }
  var v = b.vessel;
  var html = '<b>Berth ' + b.id + '</b> (Pier ' + b.pier + '): ';
  if(v && v.status === 'docked'){
    html += '<font color="#000080"><b>OCCUPIED</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + '), alongside since ' + (v.eta || '').slice(0,16) + ', ETD ' + (v.etd || '—').slice(0,16) + ', ' + v.moves_planned + ' TEU to work.';
  } else if(v && v.status === 'inbound'){
    html += '<font color="#B07000"><b>BOOKED — EN ROUTE</b></font> — ' + v.vessel_name + ' (' + v.voyage_id + ') inbound, ETA ' + (v.eta || '').slice(0,16) + ', ' + v.moves_planned + ' TEU planned, ' + v.distance_nm + ' nm out at ' + v.speed_knots + ' kn.';
  } else {
    html += '<font color="#008000"><b>AVAILABLE</b></font> — no vessel assigned.';
  }
  $('berthInfoOut').innerHTML = html;
}

function outboundTimeline(o){
  var idx = o.status === 'loaded' ? 3 : o.status === 'in_transit' ? 2 : o.status === 'released' ? 1 : 0;
  var stages = [
    {label:'Staged at PSCH', time:o.lane_release_time},
    {label:'Lane released', time:o.road_depart},
    {label:'In transit to quay', time:o.eta_loading_area},
    {label:'Loaded on vessel', time:o.eta_loading_area}
  ];
  var html = '<table class="journey">';
  stages.forEach(function(s, i){
    var cls = i < idx ? 'stage-done' : (i === idx ? 'stage-now' : 'stage-wait');
    var mark = i < idx ? '&#10003;' : (i === idx ? '&#9679;' : '&#9675;');
    html += '<tr class="' + cls + '"><td>' + mark + ' ' + s.label + '</td><td align="right">' + isoDT(s.time) + '</td></tr>';
  });
  return html + '</table>';
}

function renderOutInspector(){
  if(!outSelected){
    $('inspectorOut').innerHTML = 'Select an outbound container on the left to open its loading detail.';
    return;
  }
  var o = null;
  state.outbound.forEach(function(x){ if(x.container_id === outSelected.id) o = x; });
  if(!o){ $('inspectorOut').innerHTML = 'Container no longer in the schedule.'; return; }
  var enRoute = (o.vessel_status === 'inbound');
  var vesselPos = enRoute ? (o.vessel_distance_nm + ' nm from Tuas') : 'Alongside (0 nm)';
  var vesselSpd = enRoute ? (o.vessel_speed_knots + ' kn') : 'Moored — waiting for loading';
  var depTxt = (o.hours_to_departure === null || o.hours_to_departure === undefined) ? '—' : countdownHtml(o.hours_to_departure);
  var endTxt = (o.hours_to_loading_end === null || o.hours_to_loading_end === undefined) ? '—' : countdownHtml(o.hours_to_loading_end);
  var loadTxt = (o.status === 'loaded') ? 'loaded' : countdownHtml(o.hours_to_loading);
  var html =
    '<table width="100%"><tr><td><b>' + o.container_id + '</b> · ' + (o.size || '40HC') + ' → ' + o.destination + '</td><td align="right">' + badge(o.status) + '</td></tr></table>' +
    '<fieldset><legend>Bound for vessel (vessel tracker)</legend><table class="grid" width="100%"><tbody>' +
    row('Vessel', '<b>' + o.bound_vessel_name + '</b> (' + o.bound_vessel_id + ')') +
    row('Vessel status', enRoute ? '<b><font color="#B07000">EN ROUTE to Tuas</font></b> — arriving for loading' : '<b><font color="#000080">ALONGSIDE at berth</font></b> — waiting for loading') +
    row('Berth at Tuas', 'Berth ' + (o.berth_id || '—') + (enRoute ? ' (planned)' : ' (occupied by this vessel)')) +
    row('Distance from Tuas', vesselPos) +
    row('Speed over ground', vesselSpd) +
    row('Vessel ETA at berth', isoDT(o.vessel_eta_berth)) +
    row('Vessel next port', o.vessel_destination || '—') +
    row('Vessel workload', (o.vessel_moves === null || o.vessel_moves === undefined ? '—' : o.vessel_moves) + ' TEU to work') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Loading window — driven by vessel departure (ETD)</legend><table class="grid" width="100%"><tbody>' +
    row('Vessel leaves port (ETD)', '<b>' + isoDT(o.vessel_etd) + '</b> (' + depTxt + ')') +
    row('Loading completes by', isoDT(o.loading_end) + ' (' + endTxt + ') — 2h before sailing') +
    row('At quay by (cutoff)', isoDT(o.loading_cutoff) + ' — 4h before sailing') +
    row('This container ETA at loading area', '<b>' + isoDT(o.eta_loading_area) + '</b> (' + loadTxt + ')') +
    row('Loading lane', o.loading_lane + ' released ' + isoDT(o.lane_release_time)) +
    row('Stuffing window (PSCH)', isoTime(o.stuffing_start) + '–' + isoTime(o.stuffing_end) + 'Z') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Loading cell on vessel — bay plan</legend>' +
    '<div style="margin-bottom:2px">Bay ' + (o.bay_label || '—') + ' · Row ' + pad2(o.stow_row) + ' · Tier ' + pad2(o.stow_tier) + '</div>' +
    (o.bayplan_html || '<div style="padding:2px;color:#606060">Loading cell not yet assigned.</div>') +
    '</fieldset>' +
    '<fieldset><legend>Consolidation lifecycle</legend>' + outboundTimeline(o) +
    '<table class="grid" width="100%"><tbody>' +
    row('Source containers', o.source_containers.join(', ')) +
    row('Pallets routed', o.source_shipments.length) +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + (o.reasoning || '—') + '</div></fieldset>';
  $('inspectorOut').innerHTML = html;
}

// ---- Ship-tracker inspector -------------------------------------------------
function row(l, v){ return '<tr><td width="42%">' + l + '</td><td>' + v + '</td></tr>'; }
function selectIncoming(cid){ selected = {kind:'incoming', id:cid}; selectedBerth = null; setDetail(true); renderIncoming(); renderMap(); renderInspector(); }
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
    $('inspector').innerHTML = inspectorIncoming(m);
  } else {
    var o = null;
    state.outbound.forEach(function(x){ if(x.container_id === selected.id) o = x; });
    if(!o){ $('inspector').innerHTML = 'Container no longer in the schedule.'; return; }
    $('inspector').innerHTML = inspectorOutbound(o);
  }
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

function inspectorIncoming(m, withReasoning){
  var docked = (m.vessel_status === 'docked');
  var berthTxt = m.berth_id ? 'Berth ' + m.berth_id + (docked ? ' (alongside)' : ' (planned)') : '—';
  var posTxt = docked ? 'Alongside (0 nm)' : (m.distance_nm + ' nm from Tuas');
  var speedTxt = docked ? 'Moored' : (m.speed_knots + ' kn');
  var html =
    '<table width="100%"><tr><td><b>' + m.container_id + '</b></td><td align="right">' + badge(m.status) + '</td></tr></table>' +
    '<fieldset><legend>Carried by (vessel tracker)</legend><table class="grid" width="100%"><tbody>' +
    row('Vessel', '<b>' + m.vessel_name + '</b> (' + m.vessel_id + ')') +
    row('Berth at Tuas', berthTxt) +
    row('Distance from Tuas', posTxt) +
    row('Speed over ground', speedTxt) +
    row('Vessel ETA at berth', isoDT(m.sea_arrival)) +
    row('Vessel next port', m.destination || '—') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Stowage cell on vessel — bay plan</legend>' +
    '<div style="margin-bottom:2px">Bay ' + (m.bay_label || '—') + ' · Row ' + pad2(m.stow_row) + ' · Tier ' + pad2(m.stow_tier) +
    '</div>' +
    (m.bayplan_html || '') +
    '</fieldset>' +
    '<fieldset><legend>Journey to PSCH</legend>' + journeyTimeline(m) +
    '<table class="grid" width="100%"><tbody>' +
    row('ETA at PSCH doorstep', '<b>' + isoDT(m.psch_receipt_eta) + '</b> (' + countdownHtml(m.hours_until) + ')') +
    '</tbody></table></fieldset>';
  if(m.consolidation_group){
    html += '<fieldset><legend>Agent PSCH plan (ready before arrival)</legend><table class="grid" width="100%"><tbody>' +
      row('Receiving area', m.receiving_area) +
      row('Staging wait', isoTime(m.staging_start) + '–' + isoTime(m.staging_end) + 'Z') +
      row('Move to bin', isoTime(m.move_start) + '–' + isoTime(m.move_end) + 'Z') +
      row('Robot putaway bin', '<b>' + m.bin_location + '</b> · ' + m.putaway_robot) +
      row('Pallet pick time', isoDT(m.pallet_pick_time)) +
      row('Release lane', m.release_lane + ' (for this container number)') +
      row('Consolidation group', m.consolidation_group) +
      '</tbody></table></fieldset>';
  }
  if(withReasoning !== false){
    html += '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + (m.reasoning || '—') + '</div></fieldset>';
  }
  return html;
}

function inspectorOutbound(o, withReasoning){
  var html =
    '<table width="100%"><tr><td><b>' + o.container_id + '</b></td><td align="right">' + badge(o.status) + '</td></tr></table>' +
    '<fieldset><legend>Bound for vessel — berth ' + (o.berth_id || '—') + ' on the map</legend><table class="grid" width="100%"><tbody>' +
    row('Destination', o.destination) +
    row('Equipment size/type', (o.size || '40HC') + ' · GP') +
    row('Vessel', '<b>' + o.bound_vessel_name + '</b> (' + o.bound_vessel_id + ')') +
    row('Berth (highlighted on map)', o.berth_id || '—') +
    row('Vessel leaves port (ETD)', isoDT(o.vessel_etd)) +
    row('Loading cell on vessel', '<b>' + (o.stow_position || '—') + '</b>') +
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
    '<fieldset><legend>Loading cell on vessel — bay plan</legend>' +
    '<div style="margin-bottom:2px">Bay ' + (o.bay_label || '—') + ' · Row ' + pad2(o.stow_row) + ' · Tier ' + pad2(o.stow_tier) + '</div>' +
    (o.bayplan_html || '<div style="padding:2px;color:#606060">Loading cell not yet assigned.</div>') +
    '</fieldset>';
  if(withReasoning !== false){
    html += '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + (o.reasoning || '—') + '</div></fieldset>';
  }
  return html;
}

// ---- PSCH Plan view ----------------------------------------------------------
function renderPlan(){
  $('planSummary').innerHTML = kpiStripHtml([
    ['Inbound arrival rate (next 6h)', state.kpis.arrival_rate + ' ctn/h'],
    ['Receiving areas opened', state.kpis.receiving_areas_opened + ' / 4'],
    ['Bin utilisation', state.kpis.bin_util + '%'],
    ['Avg sea→PSCH pipeline', state.kpis.avg_pipeline_h + ' h'],
    ['Outbound consolidation groups', state.kpis.outbound_total]
  ]);

  $('planRows').innerHTML = state.inbound.map(function(m, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + m.container_id + '</td><td>' + badge(m.status) + '</td>' +
      '<td>' + isoDT(m.psch_receipt_eta) + '</td><td>' + m.receiving_area + '</td>' +
      '<td>' + isoTime(m.staging_start) + '–' + isoTime(m.staging_end) + 'Z</td>' +
      '<td>' + isoTime(m.move_start) + '–' + isoTime(m.move_end) + 'Z</td>' +
      '<td>' + m.bin_location + ' · ' + m.putaway_robot + '</td>' +
      '<td>' + isoDT(m.pallet_pick_time) + '</td><td>' + m.release_lane + '</td>' +
      '<td>' + (m.consolidation_group || 'next wave') + '</td></tr>';
  }).join('') || '<tr><td colspan="10">Run the planner first.</td></tr>';

  $('outboundRows').innerHTML = state.outbound.map(function(o, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td><b>' + o.container_id + '</b> · ' + (o.size || '40HC') + '</td><td>' + o.destination + '</td>' +
      '<td>' + o.source_containers.length + ' ctn</td><td>' + o.bound_vessel_name + '</td>' +
      '<td>' + (o.berth_id || '—') + '</td><td>' + isoDT(o.vessel_etd) + '</td>' +
      '<td>' + (o.stow_position || '—') + '</td>' +
      '<td>' + isoTime(o.stuffing_start) + '–' + isoTime(o.stuffing_end) + 'Z</td>' +
      '<td>' + o.loading_lane + ' @ ' + isoTime(o.lane_release_time) + 'Z</td>' +
      '<td>' + isoDT(o.eta_loading_area) + '</td><td>' + badge(o.status) + '</td></tr>';
  }).join('') || '<tr><td colspan="11">Run the planner first.</td></tr>';

  renderPlanList();
  renderPlanInspector();
}

function renderPlanList(){
  var html = '<table width="100%">';
  state.inbound.forEach(function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (m.status === 'Arrived') ? 'arrived' : countdownHtml(m.hours_until);
    html += '<tr><td><button type="button" class="' + cls + '" onclick="pickPlanIncoming(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + badge(m.status) + '<br>' +
      'Receiving <b>' + m.receiving_area + '</b> · <b>' + m.bin_location + '</b> · ETA ' + isoDT(m.psch_receipt_eta) + ' (' + etaTxt + ')</button></td></tr>';
  });
  $('planIncoming').innerHTML = state.inbound.length
    ? html + '</table>'
    : '<div style="padding:4px">No MCC containers in the pipeline — run the planner first.</div>';
}

function pickPlanIncoming(cid){
  selected = {kind:'incoming', id:cid};
  renderPlanList();
  renderPlanInspector();
}

function inspectorPlan(m){
  return '<fieldset><legend>Receiving &amp; putaway plan</legend>' +
    '<table class="grid"><tbody>' +
    row('Status', badge(m.status)) +
    row('Receipt ETA', '<b>' + isoDT(m.psch_receipt_eta) + '</b> (' + countdownHtml(m.hours_until) + ')') +
    row('Receiving area', '<b>' + m.receiving_area + '</b>') +
    row('Staging wait', isoTime(m.staging_start) + '–' + isoTime(m.staging_end) + 'Z') +
    row('Move to bin', isoTime(m.move_start) + '–' + isoTime(m.move_end) + 'Z') +
    row('Bin / Robot', '<b>' + m.bin_location + '</b> · ' + m.putaway_robot) +
    row('Pallet pick', isoDT(m.pallet_pick_time)) +
    row('Release lane', m.release_lane) +
    row('Consolidation group', m.consolidation_group || 'next wave') +
    '</tbody></table></fieldset>' +
    '<fieldset><legend>Agent reasoning</legend><div style="padding:2px">' + (m.reasoning || '—') + '</div></fieldset>';
}

function renderPlanInspector(){
  if(selected && selected.kind === 'incoming'){
    var m = null;
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) m = x; });
    if(m){
      $('planInspector').innerHTML = inspectorPlan(m);
      $('planSelInfo').innerHTML = 'Selected: <b>' + m.container_id + '</b> ' + badge(m.status) + ' · ' + m.receiving_area;
      return;
    }
  }
  $('planInspector').innerHTML = 'Select an inbound container on the left to open its receiving &amp; putaway plan — ' +
    'receiving area, staging window, robot putaway bin, pallet pick and release lane.';
  $('planSelInfo').innerHTML = 'Select a container on the left to open its receiving &amp; putaway plan.';
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
  var incScroll = $('pschIncoming') ? $('pschIncoming').scrollTop : 0;
  renderPschKpis();
  renderPschInbound();
  renderPschOutbound();
  renderPschIncoming();
  renderPschRackDetail();
  renderPschInspector();
  renderPschReasoning();
  var gw = document.querySelector('#pschRackDetail .psch-grid-wrap');
  if(gw) gw.scrollLeft = gridScroll;
  var rw = document.querySelector('#pschRelLanes .psch-rel-grid');
  if(rw) rw.scrollLeft = relScroll;
  if($('pschIncoming')) $('pschIncoming').scrollTop = incScroll;
}

function renderPschInbound(){
  // INBOUND section: the receiving lanes (chips, no physical layout) and the
  // incoming container list sit side by side.
  $('pschRcvLanes').innerHTML = pschLaneChips(state.psch.lanes.receiving);
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
    ['Releasing lanes in use', s.lanes_rel_used + ' / ' + s.releasing_lanes]
  ];
  $('pschKpis').innerHTML = kpiStripHtml(rows);
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

function pschZoneInline(room, side, hlCid, hlOutCids){
  var entries = [];
  room.aisles.forEach(function(a){ entries = entries.concat(a.bins); });
  if(side === 'receiving'){
    var html = '';
    entries.slice(0,8).forEach(function(b){
      var cls = b.container_id === hlCid ? ' hl' :
        (hlOutCids && hlOutCids.indexOf(b.container_id) >= 0 ? ' hl-out' : '');
      html += '<div class="psch-inline' + cls + '">' +
        '<b>' + b.container_id + '</b> (' + pschStatusShort(b.status) + ')</div>';
    });
    if(entries.length > 8) html += '<div class="psch-inline">+' + (entries.length - 8) + ' more</div>';
    return html || '<div class="psch-inline">no cargo assigned yet</div>';
  }
  var pallets = 0, groups = {};
  entries.forEach(function(b){
    pallets += (b.pallets || 0);
    if(b.consolidation_group) groups[b.consolidation_group] = 1;
  });
  var g = Object.keys(groups);
  var out = '<div class="psch-inline"><b>' + pallets + ' pallets</b> to be picked</div>';
  out += g.length
    ? '<div class="psch-inline">groups: ' + g.slice(0,4).join(', ') + (g.length > 4 ? ' +' + (g.length - 4) + ' more' : '') + '</div>'
    : '<div class="psch-inline">groups: next wave</div>';
  return out;
}

function pschRoomHtml(room, selAisle, hlAisle, hlCid, hlOutAisles, hlOutCids){
  var cold = room.id === 'cold_room' ? ' cold' : '';
  // The box header ("Ambient Room"/"Cold Room") already names the room, so the
  // inner bar carries only temperature + occupancy — no duplicate header.
  var head = '<div class="psch-room-head' + cold + '">' + room.temp + ' · ' +
    room.used + '/' + room.cap + ' bins (' + room.pct + '% occupied)</div>';
  var racks = room.aisles.map(function(a){ return pschRackHtml(a, room.id, selAisle, hlAisle, hlOutAisles); }).join('');
  var store = '<div class="psch-zone-title">STORAGE RACKS</div>' +
    '<div class="psch-rack-grid">' + racks + '</div>' +
    '<div class="psch-zone-note">racks named AISLE-LEVEL-BAY · click a rack to view its bins</div>';
  var recv = '<div class="psch-zone-title">RECEIVING</div>' + pschZoneInline(room, 'receiving', hlCid, hlOutCids);
  var disp = '<div class="psch-zone-title">RELEASING</div>' + pschZoneInline(room, 'dispatch', hlCid, hlOutCids);
  var flow = '<div class="psch-flow">INBOUND ⟶ RECEIVING ⟶ PUTAWAY ⟶ STORAGE ⟶ PICK ⟶ RELEASING ⟶ OUTBOUND</div>';
  return '<div class="psch-room">' + head +
    '<table class="psch-room-body"><tr>' +
    '<td class="psch-zone psch-zone-recv">' + recv + '</td>' +
    '<td class="psch-zone psch-zone-store">' + store + '</td>' +
    '<td class="psch-zone psch-zone-disp">' + disp + '</td>' +
    '</tr></table>' + flow + '</div>';
}

function pschLaneChips(lanes){
  var html = '';
  lanes.forEach(function(l){
    var ids = l.containers || [], oids = l.outbound || [];
    var shown = ids.slice(0,6).map(function(c){ return '<b>' + c + '</b>'; }).join(', ');
    if(ids.length > 6) shown += ' +' + (ids.length - 6) + ' more';
    oids.forEach(function(c){ shown += ' ' + c + '<i>⬆</i>'; });
    var n = ids.length + oids.length;
    html += '<div class="psch-lane-chip" title="Lane ' + l.lane + ' · ' + n + ' container(s)">' +
      '<span class="psch-lane-id">' + l.lane + '</span><span class="psch-lane-count">' + n + '</span>' +
      '<div class="psch-lane-cids">' + (shown || '—') + '</div></div>';
  });
  return html;
}

function renderPschOutbound(){
  // OUTBOUND section: the physical Releasing Lanes first, then the Ambient
  // and Cold Room rackings — stacked in that order, so the releasing lanes
  // sit straight below the INBOUND section.
  var hlAisle = null, hlCid = null, hlOut = null, hlOutCids = null;
  if(selected && selected.kind === 'incoming'){
    var bid = pschCidBin(selected.id);
    if(bid){ hlAisle = pschAisleOfBin(bid); hlCid = selected.id; }
  } else if(selected && selected.kind === 'outbound'){
    // Yellow-highlight the aisles holding this consolidation container's
    // staged pallets (its source cargo can sit across several aisles).
    hlOut = pschAislesOfOutbound(selected.id);
    state.outbound.forEach(function(o){
      if(o.container_id === selected.id) hlOutCids = o.source_containers || [];
    });
  }
  var roomBox = {};
  state.psch.rooms.forEach(function(r){
    roomBox[r.id] = '<div class="psch-facility">' +
      pschRoomHtml(r, pschSelRack, hlAisle, hlCid, hlOut, hlOutCids) + '</div>';
  });
  $('pschOutboundSec').innerHTML =
    '<fieldset><legend>Releasing Lanes</legend>' +
    '<div id="pschRelLanes"></div>' +
    '<div class="psch-rel-legend">Each vertical block is one physical lane, parked side by side like dock doors along a warehouse wall (numbered 1…26, left to right). The agent allocates a consolidation container one lane, or a contiguous group of adjacent lanes (colour-coded), to stage the pallets waiting to be loaded into it. <b>Click a lane (or its container)</b>: the <b>red outline</b> boxes that container\'s allocated lanes, the facility <b>yellow-highlights the aisles</b> holding its staged pallets (source containers turn yellow in the receiving zones), and the ship tracker shows the cargo staged on those lanes. Click a free lane, or the selected lane group again, to clear. Grey = free lane.</div>' +
    '</fieldset>' +
    '<fieldset style="margin-top:4px"><legend>Ambient Room</legend>' + (roomBox['ambient'] || '') + '</fieldset>' +
    '<fieldset style="margin-top:4px"><legend>Cold Room</legend>' + (roomBox['cold_room'] || '') + '</fieldset>';
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
    ? '<div style="padding:2px">' + txt + '</div>'
    : 'Select a container to see the agent reasoning for its plan.';
}

function selectPschRack(aisle){
  if(pschSelRack === aisle){ // clicking the selected rack again unselects it
    pschSelRack = null;
    pschSelBin = null;
    selected = null;
    renderPsch();
    return;
  }
  pschSelRack = aisle;
  pschSelBin = null;
  selected = null; // one selection at a time: picking a rack clears the rest
  api('/api/psch/inspect', {aisle: aisle}); // log the rack inspection to the trace
  renderPsch();
}

function selectPschBin(bid){
  if(pschSelBin === bid){ // clicking the selected bin again unselects it
    pschSelBin = null;
    if(selected && selected.kind === 'incoming'){
      var b = state.psch.bins[bid];
      if(b && b.container_id === selected.id) selected = null;
    }
    renderPsch();
    return;
  }
  pschSelBin = bid;
  var bin = state.psch.bins[bid];
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
        rows += '<td class="' + cls + '" onclick="selectPschBin(\'' + bid + '\')" ' +
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
  var html = '<table width="100%">';
  state.inbound.forEach(function(m){
    var cls = (selected && selected.kind === 'incoming' && selected.id === m.container_id) ? 'mp-item sel' : 'mp-item';
    var etaTxt = (m.status === 'Arrived') ? 'arrived' : countdownHtml(m.hours_until);
    html += '<tr><td><button type="button" class="' + cls + '" onclick="pickPschIncoming(\'' + m.container_id + '\')">' +
      '<span class="cid">' + m.container_id + '</span> &nbsp;' + badge(m.status) + '<br>' +
      'Bin <b>' + m.bin_location + '</b> · ETA ' + isoDT(m.psch_receipt_eta) + ' (' + etaTxt + ')</button></td></tr>';
  });
  $('pschIncoming').innerHTML = (html + '</table>') || '<div style="padding:4px">No MCC containers in the pipeline.</div>';
}

function renderPschInspector(){
  if(selected && selected.kind === 'incoming'){
    var m = null;
    state.inbound.forEach(function(x){ if(x.container_id === selected.id) m = x; });
    if(m){ $('pschInspector').innerHTML = inspectorIncoming(m, false); return; }
  }
  if(selected && selected.kind === 'outbound'){
    var o = null;
    state.outbound.forEach(function(x){ if(x.container_id === selected.id) o = x; });
    if(o){ $('pschInspector').innerHTML = inspectorOutbound(o, false); return; }
  }
  $('pschInspector').innerHTML = 'Select an incoming container (list, rack or bin) to open its ship-tracker detail — ' +
    'which vessel carries it, its exact bay cell on the vessel, the journey to PSCH, and the agent\'s plan.';
}

// ---- Control Tower -----------------------------------------------------------
function renderTower(){
  var k = state.kpis;
  $('kpiRows').innerHTML = kpiStripHtml([
    ['MCC containers in pipeline', k.mcc_containers],
    ['En route (sea + road)', k.en_route_total],
    ['Arrived at PSCH', k.arrived_at_psch],
    ['Avg sea→PSCH pipeline', k.avg_pipeline_h + ' h'],
    ['Avg remaining ETA (in flight)', k.avg_remaining_h + ' h'],
    ['Arrival rate (next 6h)', k.arrival_rate + ' ctn/h'],
    ['Receiving areas opened', k.receiving_areas_opened],
    ['Bin utilisation', k.bin_util + '%'],
    ['Outbound loaded', k.loaded_outbound + ' / ' + k.outbound_total],
    ['Yard utilisation', k.avg_yard_util + '%'],
    ['Drayage utilisation', k.drayage_util + '%']
  ]);

  $('journeyRows').innerHTML = Object.keys(k.journey_counts).map(function(s, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + badge(s) + '</td><td class="metric">' + k.journey_counts[s] + '</td></tr>';
  }).join('');
  $('outboundStatusRows').innerHTML = Object.keys(k.outbound_counts).map(function(s, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + badge(s) + '</td><td class="metric">' + k.outbound_counts[s] + '</td></tr>';
  }).join('');
}

// ---- Trace -------------------------------------------------------------------
function renderTrace(){
  $('traceRows').innerHTML = state.trace.map(function(e, i){
    return '<tr' + (i%2 ? ' class="alt"' : '') + '><td>' + isoDT(e.ts).slice(11) + '</td><td>' + e.actor + '</td><td>' + e.event + '</td><td>' + JSON.stringify(e.detail).slice(0,180) + '</td></tr>';
  }).join('') || '<tr><td colspan="4">No trace events yet.</td></tr>';
}

function renderTicker(){
  var k = state.kpis;
  $('ticker').innerHTML = '*** LIVE *** MCC cargo pipeline — ' +
    k.journey_counts['En Route (Sea)'] + ' at sea · ' + k.journey_counts['En Route (Road)'] +
    ' on road · ' + k.arrived_at_psch + ' arrived at PSCH · ' + k.loaded_outbound + ' outbound loaded · ' +
    k.receiving_areas_opened + ' receiving areas open ***';
}

function renderStatus(){
  $('statusText').innerText = 'Ready — ' + state.inbound.length + ' inbound MCC containers, ' + state.outbound.length + ' consolidation groups, ' + state.trace.length + ' trace events.';
  $('lastUpdated').innerText = 'Updated ' + new Date().toLocaleTimeString();
}

function showView(v){
  var nav = (v !== lastView);
  view = v;
  ['mcc','out','plan','psch','tower','trace'].forEach(function(name){
    $('view-' + name).style.display = (name === v) ? '' : 'none';
  });
  ['plan','psch','tower','trace'].forEach(function(name){
    $('nav-' + name).className = (name === v) ? 'active' : '';
  });
  // MCC group: one nav item, two sub-pages (Inbound / Outbound).
  $('nav-mcc').className = (v === 'mcc' || v === 'out') ? 'active' : '';
  $('nav-inbound').className = (v === 'mcc') ? 'active' : '';
  $('nav-outbound').className = (v === 'out') ? 'active' : '';
  if(nav){
    lastView = v;
    // Returning to a tracker sub-page restores its original layout:
    // columns back to normal widths, selection cleared, dropdowns closed.
    if(v === 'mcc') resetMccView();
    else if(v === 'out') resetOutView();
  }
}

async function regenerate(){
  $('statusText').innerText = 'Regenerating scenario...';
  await api('/api/seed', {});
  selected = null;
  selectedBerth = null;
  outSelected = null;
  outBerth = null;
  setDetail(false);
  closeIncomingDrop();
  closeOutDrop();
  $('incomingSearch').value = '';
  incomingQuery = '';
  $('outboundSearch').value = '';
  outQuery = '';
  await refresh();
}
async function runPlanner(){
  $('statusText').innerText = 'Running MCC planner...';
  await api('/api/agent/plan', {});
  await refresh();
}
async function clearTrace(){
  await api('/api/trace/clear', {});
  await refresh();
}

refresh().catch(function(e){ $('statusText').innerText = 'Error: ' + e.message; });
setInterval(function(){ refresh().catch(function(){}); }, 8000);
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
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(
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
                seed(seed=SEED, n_containers=N_CONTAINERS, db_path=DB_PATH)
            elif path == "/api/agent/plan":
                _run_planner()
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
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json(build_state())
        except Exception as exc:  # surface the error to the UI
            self._json({"error": str(exc)}, 500)

    def log_message(self, fmt, *args) -> None:  # keep the console quiet-ish
        pass


def main() -> None:
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
