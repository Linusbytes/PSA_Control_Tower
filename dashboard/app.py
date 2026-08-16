"""MCC control-tower dashboard (Streamlit).

Same data layer and MCC planner agent as the classic UI (server.py). The tabs
walk the multi-country consolidation story: MCC Tracker (incoming containers
with ship-tracker detail + outbound consolidation), PSCH Plan (the agent's
receiving / robot putaway / consolidation schedule), Control Tower KPIs, and
the Execution Trace.

Run from the project root:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from config import (  # noqa: E402
    DB_PATH,
    N_CONTAINERS,
    SEED,
    SIM_NOW,
)
from data import store  # noqa: E402
from data.facility import build_psch_space  # noqa: E402
from data.seed import seed  # noqa: E402
from agents import mcc_planner  # noqa: E402
from analysis.bayplan import build_bay_plan  # noqa: E402
from analysis.kpis import compute_kpis  # noqa: E402
from analysis.psch_view import (  # noqa: E402
    FACILITY_CSS,
    bin_grid_html,
    facility_collapsed_html,
    lanes_html,
    rack_summary_html,
    releasing_lanes_html,
    room_blocks_html,
)

st.set_page_config(
    page_title="PSA Control Tower · Port - PSCH Integration",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CSS = """
<style>
.iwx-nav {
  background: #14181d;
  padding: 12px 22px;
  border-radius: 8px;
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 2px;
}
.iwx-logo { color: #ffffff; font-weight: 700; font-size: 17px; letter-spacing: .4px; }
.iwx-sub { color: #8b949e; font-size: 13px; }

.stTabs [data-baseweb="tab-list"] {
  background: #1a2026;
  gap: 2px;
  border-radius: 0 0 8px 8px;
  padding: 0 12px;
}
.stTabs [data-baseweb="tab"] {
  color: #b6bfc7;
  padding: 12px 18px;
  font-weight: 500;
  border-bottom: 3px solid transparent;
}
.stTabs [aria-selected="true"] {
  color: #ffffff !important;
  border-bottom: 3px solid #0E8388 !important;
}

.iwx-card {
  background: #ffffff;
  border: 1px solid #e5e8eb;
  border-radius: 8px;
  padding: 14px 18px;
  box-shadow: 0 1px 2px rgba(16,24,40,.06);
}
.iwx-card h4 { margin: 0 0 10px 0; font-size: 15px; color: #101828; }
.iwx-card div { padding: 3px 0; color: #344054; }

.stButton > button[kind="primary"] {
  background-color: #0E8388;
  border-color: #0E8388;
  color: #ffffff;
}
.stButton > button[kind="primary"]:hover {
  background-color: #0b6f73;
  border-color: #0b6f73;
}

[data-testid="stDataFrame"] [role="columnheader"] {
  background: #1f262c;
  color: #ffffff;
}

/* Industry Bay Plan grid (same visual language as the classic UI) */
.bayplan table.bp-table { border-collapse: collapse; border: 1px solid #404040; }
.bayplan .bp-table caption { font-weight: 700; text-align: left; padding: 2px; }
.bayplan .bp-table td { border: 1px solid #808080; }
.bayplan td.bp-tier { background: #c0c0c0; font-size: 7px; font-weight: 700; text-align: center; padding: 0 2px; }
.bayplan td.bp-stack { background: #d0d0d0; font-size: 7px; text-align: center; padding: 1px 0; }
.bayplan td.bp-cell { min-width: 52px; height: 26px; padding: 0 2px; vertical-align: top; text-align: center; color: #000; }
.bayplan td.bp-empty { background: #ffffff; }
.bayplan td.bp-hatch { background: #a9a9a9; color: #4a4a4a; font-size: 8px; font-weight: 700; vertical-align: middle; text-align: center; }
.bayplan td.bp-sel { outline: 3px solid #FF0000; }
.bayplan .bp-size { font-size: 8px; font-weight: 700; }
.bayplan .bp-id { font-size: 7px; font-weight: 700; }
.bayplan .bp-w { font-size: 7px; color: #222; }
.bayplan td.bp-deck { background: #f5deb3; font-size: 8px; text-align: center; color: #5c4033; }
.bayplan td.bp-void { border: none; background: #efefef; }
.bayplan td.bp-wall-l { border-left: 2px solid #3f3f3f; }
.bayplan td.bp-wall-r { border-right: 2px solid #3f3f3f; }
.bayplan .bp-legend { margin-right: 6px; font-size: 8px; }
.bayplan .bp-legend i { display: inline-block; width: 8px; height: 8px; border: 1px solid #000; margin-right: 2px; vertical-align: middle; }
</style>
"""
st.markdown(CSS + FACILITY_CSS, unsafe_allow_html=True)

_STATUS_EMOJI = {
    "En Route (Sea)": "🌊", "Unloaded": "🏗️", "Depot": "🏭",
    "En Route (Road)": "🚚", "Arrived": "✅",
    "staged": "🔵", "released": "🟠", "in_transit": "🟡", "loaded": "🟢",
}


def _status_badge(status: str) -> str:
    return f"{_STATUS_EMOJI.get(status, '⚪')} `{status}`"


def _type_code(special: list[str]) -> str:
    """Industry container-type labels (ISO 6346 / terminal convention)."""
    if "reefer" in special:
        return "RF"
    if "hazmat" in special:
        return "DG"
    if "oversized" in special:
        return "OOG"
    return "GP"


def _ensure_seeded() -> None:
    if not store.has_data(DB_PATH):
        seed(db_path=DB_PATH)


def _run_planner() -> None:
    mcc_planner.plan(DB_PATH)


_ensure_seeded()

# --- Top nav + control strip ---------------------------------------------------
st.markdown(
    '<div class="iwx-nav"><span class="iwx-logo">PSA Control Tower</span>'
    '<span class="iwx-sub">· Port - PSCH Integration</span></div>',
    unsafe_allow_html=True,
)

c1, c2, c3, c4 = st.columns([1, 1.3, 1, 2.6])
if c1.button("↻ Regenerate", use_container_width=True):
    seed(db_path=DB_PATH)
    st.rerun()
if c2.button("🤖 Run MCC Planner", use_container_width=True, type="primary"):
    with st.spinner("Deriving journey times, receiving plan and consolidation schedule..."):
        _run_planner()
    st.rerun()
c3.caption("🧠 rule-based agent (deterministic, zero-cost)")
c4.caption(f"Sim clock **{SIM_NOW:%Y-%m-%d %H:%M}Z** · seed {SEED} · {N_CONTAINERS} containers")

tab_track, tab_plan, tab_space, tab_tower, tab_trace = st.tabs(
    ["MCC Tracker", "PSCH Plan", "PSCH Space", "Control Tower", "Trace"]
)

# --- Fresh state ---------------------------------------------------------------
containers = store.get_containers(DB_PATH)
plans = store.get_mcc_plans(DB_PATH)
outbounds = store.get_outbound_containers(DB_PATH)
if not plans:
    _run_planner()
    plans = store.get_mcc_plans(DB_PATH)
    outbounds = store.get_outbound_containers(DB_PATH)
vessels = store.get_vessels(DB_PATH)
yard = store.get_yard_status(DB_PATH)
drayage = store.get_drayage(DB_PATH)
kpis = compute_kpis(containers, plans, outbounds, yard, drayage)

vessel_by_id = {v["voyage_id"]: v for v in vessels}
plan_by_id = {p["container_id"]: p for p in plans}
stowage = store.get_vessel_stowage(DB_PATH)
stow_by = {}
for s in stowage:
    stow_by.setdefault((s["vessel_id"], s["bay"]), []).append(s)


def _bay_label(bay) -> str:
    if not bay:
        return "—"
    return f"{bay - 1}({bay})" if bay % 2 == 0 else f"{bay}"


incoming = []
for c in containers:
    if c["cargo_flag"] != "deconsolidation_required":
        continue
    p = plan_by_id.get(c["container_id"])
    if p is None:
        continue
    v = vessel_by_id.get(p["carrying_vessel_id"], {})
    cells = stow_by.get((p["carrying_vessel_id"], c.get("stow_bay")), [])
    bay_entry = {
        "container_id": c["container_id"],
        "bay_label": _bay_label(c.get("stow_bay")),
        "bay_cells": [
            {
                "stack": s["stack"], "tier": s["tier"],
                "container_id": s["container_id"], "size": s["size"],
                "destination": s["destination"], "weight_t": s["weight_t"],
                "is_mcc": s["is_mcc"], "type": s["cargo_type"],
            }
            for s in cells
        ],
    }
    incoming.append(
        {
            "Container": c["container_id"],
            "Size": c["size_type"],
            "Type": _type_code(c["special_handling"]),
            "Status": mcc_planner.journey_status(p),
            "Vessel": p["carrying_vessel_name"],
            "Voyage": p["carrying_vessel_id"],
            "Berth": v.get("berth_id") or "—",
            "Distance (nm)": p["vessel_distance_nm"],
            "Speed (kn)": p["vessel_speed_knots"],
            "Stow cell": p["stow_position"] or "—",
            "Stow": f"Bay {bay_entry['bay_label']} · Row {c.get('stow_row', 0):02d} · Tier {c.get('stow_tier', 0):02d}",
            "Receipt ETA": p["psch_receipt_eta"],
            "Hours": round((p["psch_receipt_eta"] - SIM_NOW).total_seconds() / 3600, 1),
            "Receiving area": p["receiving_area"],
            "Bin": p["bin_location"],
            "Group": p["consolidation_group"] or "next wave",
            "plan": p,
            "bayplan": build_bay_plan(bay_entry, clickable=False),
        }
    )
incoming.sort(key=lambda x: x["Receipt ETA"])

outbound = []
for o in outbounds:
    v = vessel_by_id.get(o["bound_vessel_id"], {})
    cells = stow_by.get((o["bound_vessel_id"], o.get("stow_bay")), [])
    bay_entry = {
        "container_id": o["container_id"],
        "bay_label": _bay_label(o.get("stow_bay")),
        "bay_cells": [
            {
                "stack": s["stack"], "tier": s["tier"],
                "container_id": s["container_id"], "size": s["size"],
                "destination": s["destination"], "weight_t": s["weight_t"],
                "is_mcc": s["is_mcc"], "type": s["cargo_type"],
            }
            for s in cells
        ],
    }
    outbound.append(
        {
            "Container": o["container_id"],
            "Size": o.get("size_type") or "40HC",
            "Dest": o["destination"],
            "Status": mcc_planner.outbound_status(o),
            "Bound vessel": o["bound_vessel_name"],
            "Berth": v.get("berth_id") or "—",
            "Vessel ETD": o["vessel_etd"],
            "Stow cell": o["stow_position"],
            "Stow": f"Bay {_bay_label(o.get('stow_bay'))} · Row {o.get('stow_row', 0):02d} · Tier {o.get('stow_tier', 0):02d}",
            "Stuffing window": f"{o['stuffing_start']:%H:%M}–{o['stuffing_end']:%H:%M}Z",
            "Lane release": f"{o['loading_lane']} @ {o['lane_release_time']:%H:%M}Z",
            "ETA loading area": o["eta_loading_area"],
            "Sources": len(o["source_container_ids"]),
            "bayplan": build_bay_plan(
                bay_entry,
                clickable=False,
                target=(o.get("stow_row"), o.get("stow_tier")),
            ),
            "out": o,
        }
    )
outbound.sort(key=lambda x: x["ETA loading area"])


# ============================== MCC TRACKER ====================================
with tab_track:
    st.markdown("### MCC Tracker — incoming containers & vessel tracking")
    m = st.columns(8)
    m[0].metric("At sea", kpis["journey_counts"]["En Route (Sea)"])
    m[1].metric("Unloaded", kpis["journey_counts"]["Unloaded"])
    m[2].metric("Depot", kpis["journey_counts"]["Depot"])
    m[3].metric("On road", kpis["journey_counts"]["En Route (Road)"])
    m[4].metric("Arrived @ PSCH", kpis["arrived_at_psch"])
    m[5].metric("Outbound loaded", kpis["loaded_outbound"])
    m[6].metric("Avg pipeline", f"{kpis['avg_pipeline_h']} h")
    m[7].metric("Recv. areas", kpis["receiving_areas_opened"])

    st.markdown("#### Incoming MCC containers — click one for ship-tracker detail")
    if not incoming:
        st.info("No MCC containers in the pipeline. Run the planner first.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {k: v for k, v in r.items() if k != "plan"}
                    for r in incoming
                ]
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Container": st.column_config.TextColumn(width="medium"),
                "Receipt ETA": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                "Hours": st.column_config.NumberColumn(format="%.1f h"),
                "Stow cell": st.column_config.TextColumn(width="medium"),
                "Speed (kn)": st.column_config.NumberColumn(format="%.1f"),
                "Distance (nm)": st.column_config.NumberColumn(format="%.1f"),
            },
        )

        sel = st.selectbox(
            "Inspect container (ship tracker)", [r["Container"] for r in incoming],
            key="ship_tracker_sel",
        )
        r = next(x for x in incoming if x["Container"] == sel)
        p = r["plan"]
        v = vessel_by_id.get(p["carrying_vessel_id"], {})
        docked = v.get("status") == "docked"
        with st.container(border=True):
            st.markdown(f"**{r['Container']}** — {_status_badge(r['Status'])}")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(
                    f"""<div class="iwx-card"><h4>🚢 Carried by (vessel tracker)</h4>
                    <div><b>{r['Vessel']}</b> ({r['Voyage']})</div>
                    <div>Berth at Tuas: {r['Berth']} ({'alongside' if docked else 'planned'})</div>
                    <div>Distance from Tuas: {('alongside' if docked else f'{p['vessel_distance_nm']} nm')}</div>
                    <div>Speed over ground: {'moored' if docked else f'{p['vessel_speed_knots']} kn'}</div>
                    <div>Vessel ETA at berth: {p['sea_arrival']:%d %b %H:%M}Z</div>
                    <div>Vessel next port: {p['vessel_destination'] or '—'}</div>
                    <div>Stowage cell: <b>{p['stow_position'] or '—'}</b> <small>(Bay·Row·Tier)</small></div></div>""",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='margin-top:6px'>{r['bayplan']}</div>",
                    unsafe_allow_html=True,
                )
            with c2:
                stages = [
                    ("🌊 En Route (Sea)", p["sea_arrival"], r["Status"] == "En Route (Sea)"),
                    ("🏗️ Unloaded", p["depot_arrive"], r["Status"] == "Unloaded"),
                    ("🏭 Depot", p["road_depart"], r["Status"] == "Depot"),
                    ("🚚 En Route (Road)", p["psch_receipt_eta"], r["Status"] == "En Route (Road)"),
                    ("✅ Arrived", p["psch_receipt_eta"], r["Status"] == "Arrived"),
                ]
                current = next((i for i, s in enumerate(stages) if s[2]), 0)
                rows = "".join(
                    f"<div>{'🟢' if i < current else ('🟡' if i == current else '⚪')} "
                    f"{s[0]} &nbsp; <b>{s[1]:%H:%M}Z</b></div>"
                    for i, s in enumerate(stages)
                )
                st.markdown(
                    f"""<div class="iwx-card"><h4>🗺️ Journey to PSCH</h4>{rows}
                    <div style="margin-top:6px">ETA at PSCH doorstep: <b>{p['psch_receipt_eta']:%d %b %H:%M}Z</b>
                    ({r['Hours']} h)</div></div>""",
                    unsafe_allow_html=True,
                )
        with st.container(border=True):
            st.markdown("**🧠 Agent PSCH plan (ready before arrival)**")
            st.markdown(
                f"""<div class="iwx-card"><div>Receiving area: <b>{p['receiving_area']}</b></div>
                <div>Staging wait: {p['staging_start']:%H:%M}–{p['staging_end']:%H:%M}Z</div>
                <div>Move to bin: {p['move_start']:%H:%M}–{p['move_end']:%H:%M}Z</div>
                <div>Robot putaway bin: <b>{p['bin_location']}</b> · {p['putaway_robot']}</div>
                <div>Pallet pick time: {p['pallet_pick_time']:%d %b %H:%M}Z</div>
                <div>Release lane: {p['release_lane']} (for this container number)</div>
                <div>Consolidation group: {p['consolidation_group'] or 'next wave'}</div>
                <div style="color:#667085">Reasoning: {p['reasoning']}</div></div>""",
                unsafe_allow_html=True,
            )

    st.markdown("#### Outbound consolidation — bound for vessel")
    if not outbound:
        st.info("No consolidation groups yet. Run the planner first.")
    else:
        st.dataframe(
            pd.DataFrame([{k: v for k, v in r.items() if k not in ("out", "bayplan")} for r in outbound]),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Vessel ETD": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                "ETA loading area": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                "Stow cell": st.column_config.TextColumn(width="medium"),
            },
        )
        sel_o = st.selectbox(
            "Inspect outbound container (vessel it is bound for)",
            [r["Container"] for r in outbound],
            key="outbound_sel",
        )
        ob_rec = next(x for x in outbound if x["Container"] == sel_o)
        o = ob_rec["out"]
        ov = vessel_by_id.get(o["bound_vessel_id"], {})
        if ov.get("status") == "docked":
            vstat = f"<b>ALONGSIDE</b> at berth {ov.get('berth_id')} — waiting for loading"
        else:
            vstat = (
                f"<b>EN ROUTE</b> to Tuas — {ov.get('distance_nm')} nm out at "
                f"{ov.get('speed_knots')} kn"
            )
        loading_end = (o["vessel_etd"] - timedelta(hours=2)).strftime("%d %b %H:%M")
        st.markdown(
            f"""<div class="iwx-card"><h4>🚢 {o['container_id']} · {o.get('size_type') or '40HC'} → {o['destination']} · {_status_badge(o['status'])}</h4>
            <div>Equipment: {o.get('size_type') or '40HC'} · GP (40ft-family)</div>
            <div>Bound vessel: <b>{o['bound_vessel_name']}</b> ({o['bound_vessel_id']}) · berth {o.get('berth_id') or '—'} (highlighted on the map)</div>
            <div>Vessel status: {vstat}</div>
            <div>Vessel ETA at berth: {ov.get('eta', o['vessel_etd']):%d %b %H:%M}Z · Vessel leaves port (ETD): {o['vessel_etd']:%d %b %H:%M}Z</div>
            <div>Loading completes by: {loading_end}Z (ETD − 2h) — container at quay by {o['eta_loading_area']:%d %b %H:%M}Z</div>
            <div>Loading cell on vessel: <b>{o['stow_position']}</b></div>
            <div>Stuffing / pallet-pick window: {o['stuffing_start']:%d %b %H:%M}Z → {o['stuffing_end']:%d %b %H:%M}Z</div>
            <div>Loading lane released: {o['loading_lane']} at {o['lane_release_time']:%d %b %H:%M}Z</div>
            <div>ETA to arrive at loading area: <b>{o['eta_loading_area']:%d %b %H:%M}Z</b></div>
            <div>Sources: {', '.join(o['source_container_ids'])}</div>
            <div style="color:#667085">Reasoning: {o['reasoning']}</div></div>""",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""<div class="iwx-card"><h4>🗺️ Loading cell on vessel — bay plan</h4>
            <div>{ob_rec['Stow']}</div>
            {ob_rec['bayplan']}</div>""",
            unsafe_allow_html=True,
        )

# ============================== PSCH PLAN ======================================
with tab_plan:
    st.markdown("### PSCH Plan — agent-prepared receiving, putaway & consolidation")
    p = st.columns(5)
    p[0].metric("Arrival rate (next 6h)", f"{kpis['arrival_rate']} ctn/h")
    p[1].metric("Receiving areas opened", f"{kpis['receiving_areas_opened']} / 4")
    p[2].metric("Bin utilisation", f"{kpis['bin_util']}%")
    p[3].metric("Avg sea→PSCH pipeline", f"{kpis['avg_pipeline_h']} h")
    p[4].metric("Outbound groups", kpis["outbound_total"])

    st.markdown("#### Receiving & robot putaway plan (per inbound container)")
    if incoming:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Container": r["Container"],
                        "Status": r["Status"],
                        "Receipt ETA": r["Receipt ETA"],
                        "Receiving area": r["Receiving area"],
                        "Staging": f"{r['plan']['staging_start']:%H:%M}–{r['plan']['staging_end']:%H:%M}Z",
                        "Move to bin": f"{r['plan']['move_start']:%H:%M}–{r['plan']['move_end']:%H:%M}Z",
                        "Bin / Robot": f"{r['plan']['bin_location']} · {r['plan']['putaway_robot']}",
                        "Pallet pick": r["plan"]["pallet_pick_time"],
                        "Release lane": r["plan"]["release_lane"],
                        "Consol. group": r["Group"],
                    }
                    for r in incoming
                ]
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Receipt ETA": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                "Pallet pick": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            },
        )

    st.markdown("#### Outbound consolidation schedule")
    if outbound:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Outbound ctn": o["Container"],
                        "Dest": o["Dest"],
                        "Sources": o["Sources"],
                        "Bound vessel": o["Bound vessel"],
                        "Berth": o["Berth"],
                        "Vessel ETD": o["Vessel ETD"],
                        "Stow cell": o["Stow cell"],
                        "Stuffing window": o["Stuffing window"],
                        "Lane release": o["Lane release"],
                        "ETA loading area": o["ETA loading area"],
                        "Status": o["Status"],
                    }
                    for o in outbound
                ]
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Vessel ETD": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                "ETA loading area": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
            },
        )

# ============================== PSCH SPACE =====================================
with tab_space:
    st.markdown(
        "### PSCH Space — racking, bins & staging lanes (AMBIENT + COLD ROOM)"
    )
    shipments = store.get_shipments(DB_PATH)
    psch = build_psch_space(plans, shipments, outbounds)
    stats = psch["stats"]
    occ = kpis.get("room_occupancy", {})
    m = st.columns(7)
    m[0].metric("Rack bins used", f"{stats['bins_used']} / {stats['bins_total']} ({stats['bin_util']}%)")
    m[1].metric("AMBIENT occupancy", f"{occ.get('ambient', '—')}%")
    m[2].metric("COLD ROOM occupancy", f"{occ.get('cold_room', '—')}%")
    m[3].metric("Pallets planned", stats["pallets_planned"])
    m[4].metric("Pallets in storage", stats["pallets_in_storage"])
    m[5].metric("Receiving lanes", f"{stats['lanes_rcv_used']} / {stats['lanes_rcv_total']}")
    m[6].metric("Releasing lanes", f"{stats['lanes_rel_used']} / {stats['releasing_lanes']}")

    st.caption(
        "Conventions: bins named **AISLE-LEVEL-BAY** (e.g. 1-12-2A = Aisle 1, Level 12, "
        "Bay 2A — the box letter is written directly on the bay number) as in a "
        "distribution centre; aisles numbered 1-24 (ambient 1-21, cold room 22-24; "
        "aisle 21 segregated for dangerous goods); every aisle has 12 levels (height), "
        "each level has 3 bays and every bay has boxes A/B/C. **Slotting agent**: the "
        "height is optimised from when the cargo will be released — soon-to-release "
        "cargo at floor level, slower movers higher. Yellow bin = reserved before "
        "arrival · green = cargo arrived · grey = empty."
    )

    aisle_opts = [
        {
            "room": r["id"],
            "aisle": a["id"],
            "label": f'Aisle {a["id"]} ({r["label"]}) — {a["used"]}/{a["cap"]} bins · {a["pct"]}%',
        }
        for r in psch["rooms"]
        for a in r["aisles"]
    ]
    sel_label = st.selectbox(
        "Select a rack to inspect", [o["label"] for o in aisle_opts], key="psch_aisle"
    )
    sel_aisle = next(o["aisle"] for o in aisle_opts if o["label"] == sel_label)

    cid_opts = ["—"] + [r["Container"] for r in incoming]
    sel_cid_label = st.selectbox(
        "Select an incoming container (highlights its rack/bin below)",
        cid_opts,
        key="psch_cid",
    )
    sel_cid = None if sel_cid_label == "—" else sel_cid_label

    ob_opts = ["—"] + [o["container_id"] for o in outbounds]
    sel_ob_label = st.selectbox(
        "Consolidation container collecting from these lanes", ob_opts, key="psch_ob"
    )
    sel_ob = None if sel_ob_label == "—" else sel_ob_label
    rooms_html = room_blocks_html(
        psch, selected_aisle=sel_aisle, selected_cid=sel_cid, selected_outbound_cid=sel_ob
    )

    col_main, col_right = st.columns([1.5, 1])
    with col_main:
        st.markdown("#### INBOUND")
        ca, cb = st.columns(2)
        with ca:
            st.markdown("**Inbound Receiving Lanes**")
            st.markdown(lanes_html(psch), unsafe_allow_html=True)
        with cb:
            st.markdown("**Incoming Containers**")
            if not incoming:
                st.info("No MCC containers in the pipeline. Run the planner first.")
            else:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Container": r["Container"],
                                "Size": r["Size"],
                                "Type": r["Type"],
                                "Status": r["Status"],
                                "Vessel": r["Vessel"],
                                "Receipt ETA": r["Receipt ETA"],
                                "Hours": r["Hours"],
                                "Receiving area": r["Receiving area"],
                                "Bin": r["Bin"],
                                "Group": r["Group"],
                            }
                            for r in incoming
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Receipt ETA": st.column_config.DatetimeColumn(format="DD MMM HH:mm"),
                        "Hours": st.column_config.NumberColumn(format="%.1f h"),
                    },
                )

        st.markdown("#### OUTBOUND")
        collapsed = st.toggle(
            "Collapse rooms to compact summary", value=False, key="psch_collapse"
        )
        if collapsed:
            st.markdown(facility_collapsed_html(psch), unsafe_allow_html=True)
        else:
            st.markdown("**Ambient Room**")
            st.markdown(rooms_html["ambient"], unsafe_allow_html=True)
            st.markdown("**Cold Room**")
            st.markdown(rooms_html["cold_room"], unsafe_allow_html=True)

        st.markdown("**Releasing Lanes**")
        st.caption(
            "26 narrow vertical blocks parked side by side (like dock doors along a "
            "warehouse wall), one physical lane each, numbered 1…26 left to right at "
            "the PSCH dispatch area. Each MCC consolidation container is allocated one "
            "lane, or a contiguous group of adjacent lanes (colour-coded), to stage the "
            "pallets waiting to be loaded into that same container (one lane holds up "
            "to 8 pallets). The **red outline** marks the selected container's allocated "
            "lanes; pick a container above to see the cargo staged on its lanes. "
            "Grey = free lane. Picking a container also yellow-highlights the aisles "
            "holding its staged pallets in the rooms above."
        )
        st.markdown(releasing_lanes_html(psch, selected_cid=sel_ob), unsafe_allow_html=True)
        if sel_ob:
            ob = next(o for o in outbounds if o["container_id"] == sel_ob)
            start, end = ob.get("staging_lane_start"), ob.get("staging_lane_end")
            lanes_txt = str(start) if start == end else f"{start}–{end}"
            st.markdown(
                f"**{ob['container_id']}** — {len(ob['source_shipment_ids'])} pallets staged on "
                f"**{lanes_txt}** for collection · bound for {ob['bound_vessel_name']} "
                f"({ob['bound_vessel_id']}). Staged cargoes: {', '.join(ob['source_container_ids'])}."
            )

    with col_right:
        st.markdown("**Ship Tracker — Container Detail**")
        if not sel_cid:
            st.info("Select an incoming container above to open its ship-tracker detail.")
        else:
            r = next(x for x in incoming if x["Container"] == sel_cid)
            p = r["plan"]
            v = vessel_by_id.get(p["carrying_vessel_id"], {})
            docked = v.get("status") == "docked"
            st.markdown(f"**{r['Container']}** — {_status_badge(r['Status'])}")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown(
                    f"""<div class="iwx-card"><h4>🚢 Carried by (vessel tracker)</h4>
                    <div><b>{r['Vessel']}</b> ({r['Voyage']})</div>
                    <div>Berth at Tuas: {r['Berth']} ({'alongside' if docked else 'planned'})</div>
                    <div>Distance from Tuas: {('alongside' if docked else f'{p['vessel_distance_nm']} nm')}</div>
                    <div>Speed over ground: {'moored' if docked else f'{p['vessel_speed_knots']} kn'}</div>
                    <div>Vessel ETA at berth: {p['sea_arrival']:%d %b %H:%M}Z</div>
                    <div>Stowage cell on vessel: <b>{p['stow_position'] or '—'}</b></div></div>""",
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(
                    f"""<div class="iwx-card"><h4>🗺️ Journey to PSCH</h4>
                    <div>Status: {_status_badge(r['Status'])}</div>
                    <div>ETA at PSCH doorstep: <b>{p['psch_receipt_eta']:%d %b %H:%M}Z</b> ({r['Hours']} h)</div>
                    <div>Receiving area: <b>{p['receiving_area']}</b></div>
                    <div>Robot putaway bin: <b>{p['bin_location']}</b> · {p['putaway_robot']}</div>
                    <div>Pallet pick: {p['pallet_pick_time']:%d %b %H:%M}Z</div>
                    <div>Release lane: {p['release_lane']} · Group: {p['consolidation_group'] or 'next wave'}</div></div>""",
                    unsafe_allow_html=True,
                )
        st.markdown("**Agent Reasoning**")
        if not sel_cid:
            st.caption("Select a container to see the agent reasoning for its plan.")
        else:
            r = next(x for x in incoming if x["Container"] == sel_cid)
            st.markdown(f"{r['plan']['reasoning']}")

    st.markdown(
        "**Aisle Details** — every bin in the selected aisle (AISLE-LEVEL-BAY, yellow = reserved · green = arrived · grey = empty)"
    )
    st.markdown(
        rack_summary_html(psch, sel_aisle) + bin_grid_html(psch, sel_aisle, selected_cid=sel_cid),
        unsafe_allow_html=True,
    )

# ============================== CONTROL TOWER ==================================
with tab_tower:
    st.markdown("### Control tower KPIs — MCC cargo pipeline")
    k = st.columns(6)
    k[0].metric("MCC containers", kpis["mcc_containers"])
    k[1].metric("En route (sea + road)", kpis["en_route_total"])
    k[2].metric("Arrived at PSCH", kpis["arrived_at_psch"])
    k[3].metric("Avg remaining ETA", f"{kpis['avg_remaining_h']} h")
    k[4].metric("Outbound loaded", f"{kpis['loaded_outbound']} / {kpis['outbound_total']}")
    k[5].metric("Bin utilisation", f"{kpis['bin_util']}%")

    jl, or_ = st.columns(2)
    with jl:
        st.markdown("**Inbound journey stages**")
        st.dataframe(
            pd.DataFrame(
                [{"Stage": s, "Containers": n} for s, n in kpis["journey_counts"].items()]
            ),
            use_container_width=True,
            hide_index=True,
        )
    with or_:
        st.markdown("**Outbound consolidation status**")
        st.dataframe(
            pd.DataFrame(
                [{"Status": s, "Containers": n} for s, n in kpis["outbound_counts"].items()]
            ),
            use_container_width=True,
            hide_index=True,
        )

# ============================== EXECUTION TRACE ================================
with tab_trace:
    st.markdown("### Execution trace")
    trace = store.get_trace(path=DB_PATH)
    if not trace:
        st.info(
            "No trace events yet. Run the MCC planner or click a berth on the map "
            "to record tool calls, decisions and inspections."
        )
    else:
        st.caption(f"{len(trace)} most recent events (newest first)")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Time": e["ts"].strftime("%H:%M:%S"),
                        "Actor": e["actor"],
                        "Event": e["event"],
                        "Detail": json.dumps(e["detail"])[:220],
                    }
                    for e in trace
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        if st.button("Clear trace"):
            store.clear_trace(DB_PATH)
            st.rerun()
