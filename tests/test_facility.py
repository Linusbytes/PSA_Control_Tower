"""Tests for the PSCH facility model and the space-utilisation view.

Covers the distribution-centre bin-naming convention (AISLE-LEVEL-BAY), the
ambient / cold-room split, cold-room and hazmat routing by the planner, the
dwell-based slotting rule (height optimisation), the staging lanes, and the
shared HTML builders.
"""
import re

from data import store
from data.facility import (
    aisle_of_bin,
    bay_of_bin,
    bin_id_of,
    bin_room,
    box_of_bin,
    build_psch_space,
    iter_bins,
    level_of_bin,
    room_capacity,
    room_of_aisle,
    total_capacity,
)
from data.simulator import generate


def _planned(tmp_path, seed=42, n=60):
    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=seed, n_containers=n), db)
    from agents.mcc_planner import plan

    plan(db)
    return db


def test_bin_names_follow_dc_aisle_level_bay_convention():
    # 24 numbered aisles across the facility: ambient 1-21, cold room 22-24.
    ambient = iter_bins("ambient")
    assert ambient[0] == "1-01-1A"
    assert "21-12-3C" in ambient  # last ambient aisle, top level, last bay, box C
    assert len(ambient) == room_capacity("ambient") == 21 * 12 * 3 * 3 == 2268

    cold = iter_bins("cold_room")
    assert cold[0] == "22-01-1A"
    assert len(cold) == room_capacity("cold_room") == 3 * 12 * 3 * 3 == 324
    assert total_capacity() == len(ambient) + len(cold) == 2592

    # AISLE-LEVEL-BAY naming: e.g. 1-12-2A = Aisle 1, Level 12, Bay 2A (the
    # box letter is written directly on the bay number).
    assert aisle_of_bin("1-12-2A") == "1"
    assert level_of_bin("1-12-2A") == 12
    assert bay_of_bin("1-12-2A") == 2
    assert box_of_bin("1-12-2A") == "A"
    assert aisle_of_bin("22-03-1B") == "22"
    assert bin_room("1-12-2A") == "ambient"
    assert bin_room("22-03-1B") == "cold_room"
    assert room_of_aisle("22") == "cold_room"
    assert room_of_aisle("21") == "ambient"
    assert bin_id_of("Bin 1-12-2A") == "1-12-2A"

    # Every level holds bays 1-3, each with exactly the boxes A/B/C.
    for room in ("ambient", "cold_room"):
        for b in iter_bins(room):
            assert b[-1] in "ABC"
        assert {level_of_bin(b) for b in iter_bins(room)} == set(range(1, 13))
        assert {bay_of_bin(b) for b in iter_bins(room)} == {1, 2, 3}


def test_planner_routes_reefer_to_cold_room_and_hazmat_to_aisle_d(tmp_path):
    db = _planned(tmp_path)
    containers = {c["container_id"]: c for c in store.get_containers(db)}
    # Bin flows (mcc / lcl) deconsolidate into rack bins; whole-container
    # flows (fcl / topup / transload) are staged whole and carry no bin.
    plans = [p for p in store.get_mcc_plans(db) if p["bin_location"].startswith("Bin ")]
    assert plans
    for p in plans:
        special = containers[p["container_id"]]["special_handling"]
        bid = bin_id_of(p["bin_location"])
        if "reefer" in special:
            assert bin_room(bid) == "cold_room", p["bin_location"]
        elif "hazmat" in special:
            assert aisle_of_bin(bid) == "21", p["bin_location"]
        else:
            assert bin_room(bid) == "ambient" and aisle_of_bin(bid) != "21", p["bin_location"]

    # The scenario deliberately carries a healthy reefer share so the cold
    # room is visibly populated in the facility view.
    cold_bins = {
        p["bin_location"]
        for p in plans
        if bin_room(bin_id_of(p["bin_location"])) == "cold_room"
    }
    assert len(cold_bins) >= 4


def test_psch_space_state_shape_and_lanes(tmp_path):
    db = _planned(tmp_path)
    plans = store.get_mcc_plans(db)
    shipments = store.get_shipments(db)
    outbounds = store.get_outbound_containers(db)
    psch = build_psch_space(plans, shipments, outbounds)

    assert [r["id"] for r in psch["rooms"]] == ["ambient", "cold_room"]
    ambient, cold = psch["rooms"]
    assert {a["id"] for a in ambient["aisles"]} == {str(i) for i in range(1, 22)}
    assert {a["id"] for a in cold["aisles"]} == {"22", "23", "24"}
    assert psch["hazmat_aisle"] == "21"
    assert all(a["used"] <= a["cap"] for r in psch["rooms"] for a in r["aisles"])
    assert all(a["levels"] == 12 and a["bays"] == 3 and a["boxes"] == 3 for r in psch["rooms"] for a in r["aisles"])

    bin_plans = [p for p in plans if p["bin_location"].startswith("Bin ")]
    # The facility carries a deterministic prior-wave dwell-stock floor on top
    # of the current wave's plan bins, so utilisation sits in a realistic band.
    assert psch["stats"]["bins_used"] >= len(bin_plans)
    assert psch["stats"]["stock_bins"] > 0
    assert psch["stats"]["bins_total"] == total_capacity()
    assert 25 <= psch["stats"]["bin_util"] <= 70  # busy warehouse, not 3%
    for p in bin_plans:
        assert bin_id_of(p["bin_location"]) in psch["bins"]
    # Dwell stock never double-books a wave bin and never overflows an aisle.
    for r in psch["rooms"]:
        for a in r["aisles"]:
            assert a["used"] <= a["cap"]
            assert a["used"] == len(a["bins"]) + a["stock"]
            assert a["pct"] <= 70
    # Whole-container flows stage in slots/bays, never rack bins.
    for p in plans:
        if not p["bin_location"].startswith("Bin "):
            assert bin_id_of(p["bin_location"]) not in psch["bins"]

    # Staging lanes: receiving lanes mirror the receiving areas (RA-n -> lane n,
    # numbered plainly 1..10). Membership is LIVE against the sim clock: a
    # container occupies its lane from arrival at PSCH until its putaway move
    # completes (cargo moved to its bin), so the lanes fill and empty as the
    # wave progresses — and the UI flashes each lane when a container arrives
    # or leaves. The releasing lanes are the 40 physical lanes at the dispatch
    # area, a cycling staging buffer: a lane may serve successive groups whose
    # stuffing windows do not overlap, never two at the same time.
    assert len(psch["lanes"]["receiving"]) == 10
    assert len(psch["lanes"]["releasing"]) == 40
    all_rcv = [c for lane in psch["lanes"]["receiving"] for c in lane["containers"]]
    assert len(all_rcv) <= len(plans)
    assert len(all_rcv) > 0  # at the seed instant some containers have arrived
    # Every listed container belongs to the matching receiving area.
    for i, lane in enumerate(psch["lanes"]["receiving"], start=1):
        for cid in lane["containers"]:
            p = next(p for p in plans if p["container_id"] == cid)
            assert str(p.get("receiving_area", "")).startswith(f"RA-{i}")
    assert any(lane["group"] for lane in psch["lanes"]["releasing"])
    assert psch["stats"]["releasing_lanes"] == 40

    # Bins of arrived containers are marked occupied; others reserved.
    assert any(b["arrived"] for b in psch["bins"].values())
    assert any(not b["arrived"] for b in psch["bins"].values())


def test_slotting_optimises_height_by_release_time(tmp_path):
    """The slotting agent stores soon-to-release cargo at floor level.

    The rack level is a pure function of the predicted dwell (pallet pick time
    minus receipt), so every plan's level must match the slotting rule.
    """
    from agents.mcc_planner import _dwell_level

    db = _planned(tmp_path)
    plans = [p for p in store.get_mcc_plans(db) if p["bin_location"].startswith("Bin ")]
    assert plans
    for p in plans:
        dwell_h = (p["pallet_pick_time"] - p["psch_receipt_eta"]).total_seconds() / 3600
        expected = _dwell_level(max(0.0, dwell_h))
        assert level_of_bin(bin_id_of(p["bin_location"])) == expected, p["bin_location"]

    # Cargo released immediately (dwell ~0, e.g. next-wave) sits at floor level.
    soon = [p for p in plans if p["pallet_pick_time"] <= p["psch_receipt_eta"]]
    assert soon
    assert all(level_of_bin(bin_id_of(p["bin_location"])) == 1 for p in soon)
    # And at least some cargo is slotted above the floor.
    assert any(level_of_bin(bin_id_of(p["bin_location"])) > 1 for p in plans)


def test_kpis_include_room_occupancy(tmp_path):
    from analysis.kpis import compute_kpis

    db = _planned(tmp_path)
    containers = store.get_containers(db)
    plans = store.get_mcc_plans(db)
    outbounds = store.get_outbound_containers(db)
    k = compute_kpis(
        containers, plans, outbounds, store.get_yard_status(db), store.get_drayage(db)
    )
    assert set(k["room_occupancy"]) == {"ambient", "cold_room"}
    assert 0 <= k["bin_util"] <= 100
    bin_plans = [p for p in plans if p["bin_location"].startswith("Bin ")]
    # Same dwell-stock floor as the facility view: KPI and Storage page agree.
    assert k["bins_used"] >= len(bin_plans)
    assert k["bins_total"] == total_capacity()
    assert k["lanes_releasing_used"] >= 1


def test_releasing_lanes_allocated_contiguously(tmp_path):
    """The agent allocates one lane (or a contiguous span) per consolidation group.

    Every staged group owns a contiguous range inside lanes 1..40 (a group never
    jumps lanes), and the dispatch area is a CYCLING STAGING BUFFER: two groups
    may reuse the same lane only when their stuffing windows do not overlap,
    exactly like a real dispatch area whose bays are freed once a box is sealed.
    """
    db = _planned(tmp_path)
    outbounds = store.get_outbound_containers(db)
    psch = build_psch_space(
        store.get_mcc_plans(db), store.get_shipments(db), outbounds
    )

    groups = psch["releasing_groups"]
    assert groups
    assert len(groups) == len([o for o in outbounds if o.get("staging_lane_start")])
    for g in groups:
        assert 1 <= g["lane_start"] <= g["lane_end"] <= 40
        assert len(g["lanes"]) == g["lane_end"] - g["lane_start"] + 1
        assert g["container_id"] and g["sources"]
    for lane in psch["lanes"]["releasing"]:
        if lane["group"]:
            group = next(g for g in groups if g["container_id"] == lane["group"])
            assert lane["lane"] in group["lanes"]
    # A lane shared by two groups must never serve overlapping stuffing windows.
    staged = [o for o in outbounds if o.get("staging_lane_start")]
    for i, a in enumerate(staged):
        for b in staged[i + 1 :]:
            a_lanes = set(range(a["staging_lane_start"], a["staging_lane_end"] + 1))
            b_lanes = set(range(b["staging_lane_start"], b["staging_lane_end"] + 1))
            if not (a_lanes & b_lanes):
                continue
            lo = max(a["stuffing_start"], b["stuffing_start"])
            hi = min(a["stuffing_end"], b["stuffing_end"])
            assert lo >= hi, (a["container_id"], b["container_id"])


def test_wave_bins_spread_across_all_aisles(tmp_path):
    """Bin assignment interleaves across every aisle of the zone.

    The old cursor walked each (zone, level) pool sequentially, so the first
    aisles filled while the rest sat empty. The shuffled pool spreads the
    wave's putaways across all aisles, which is what a busy rack looks like.
    """
    db = _planned(tmp_path, seed=42, n=300)
    plans = [p for p in store.get_mcc_plans(db) if p["bin_location"].startswith("Bin ")]
    by_aisle = {}
    for p in plans:
        a = aisle_of_bin(bin_id_of(p["bin_location"]))
        by_aisle[a] = by_aisle.get(a, 0) + 1
    # Wave bins land in a healthy majority of the facility's 24 aisles.
    assert len(by_aisle) >= 18, f"only {len(by_aisle)} aisles used: {by_aisle}"


def test_aisle_utilisation_band_with_dwell_stock(tmp_path):
    """Every aisle sits in a realistic utilisation band (30-70%, some ~10%)."""
    db = _planned(tmp_path)
    psch = build_psch_space(
        store.get_mcc_plans(db), store.get_shipments(db), store.get_outbound_containers(db)
    )
    for room in psch["rooms"]:
        for a in room["aisles"]:
            assert 8 <= a["pct"] <= 70, (room["id"], a["id"], a["pct"])
    # The band is a real band: several aisles comfortably inside 30-70%, and
    # a few quiet ones allowed near 10%.
    busy = [a for r in psch["rooms"] for a in r["aisles"] if a["pct"] >= 30]
    assert len(busy) >= 14


def test_dwell_stock_is_deterministic_and_never_double_books(tmp_path):
    from data.facility import dwell_stock

    db = _planned(tmp_path)
    plans = store.get_mcc_plans(db)
    wave = {
        bin_id_of(p["bin_location"])
        for p in plans
        if p["bin_location"].startswith("Bin ")
    }
    a = dwell_stock(wave)
    b = dwell_stock(wave)
    assert a == b  # identical on every poll / regeneration
    assert not (set(a) & wave)  # never double-books a wave bin
    assert all(v["stock"] and v["arrived"] and v["pallets"] >= 1 for v in a.values())


def test_consolidation_chunks_per_destination(tmp_path):
    """A vessel call takes several consolidation boxes, not one giant group."""
    from config import MCC_GROUP_SIZE

    db = _planned(tmp_path, seed=42, n=300)
    outbounds = store.get_outbound_containers(db)
    assert len(outbounds) >= 8
    # Every consolidation box groups at most MCC_GROUP_SIZE source containers
    # and at least one (chunking keeps the staging footprint realistic).
    for o in outbounds:
        assert 1 <= len(o["source_container_ids"]) <= MCC_GROUP_SIZE
    # The same destination produces several boxes (per-vessel chunking).
    from collections import Counter

    per_dest = Counter(o["destination"] for o in outbounds)
    assert any(c >= 2 for c in per_dest.values())


def test_psch_view_html_builders(tmp_path):
    from analysis.psch_view import (
        bin_grid_html,
        facility_html,
        lanes_html,
        rack_summary_html,
        releasing_lanes_html,
    )

    db = _planned(tmp_path)
    psch = build_psch_space(
        store.get_mcc_plans(db), store.get_shipments(db), store.get_outbound_containers(db)
    )

    html = facility_html(psch)
    assert "psch-room" in html
    # The room name lives in the box header; the inner bar carries temp + occupancy.
    assert "+15 °C … +25 °C" in html and "0 °C … −18 °C" in html
    assert "Aisle 1" in html and "Aisle 22" in html
    assert "RECEIVING LANES" in html
    assert "INBOUND ⟶" in html

    rcv = lanes_html(psch)
    assert ">1<" in rcv and ">10<" in rcv  # receiving lanes numbered plainly 1..10

    rel = releasing_lanes_html(psch)
    # Lanes are named plainly 1..26 (no REL- prefix) in a single side-by-side row.
    assert "psch-rel-lane" in rel and ">1<" in rel and ">26<" in rel
    assert "REL-" not in rel and "free" in rel

    # Selecting an outbound container yellow-highlights the aisles holding its
    # staged pallets on the facility.
    grp = psch["releasing_groups"][0]
    fac_out = facility_html(psch, selected_outbound_cid=grp["container_id"])
    assert "hl-out" in fac_out
    assert facility_html(psch).count("hl-out") == 0

    grid = bin_grid_html(psch, "1")
    # Levels are plain numbers (no "L" prefix): header "Level", rows 12..1.
    assert "Bay 1" in grid and ">Level</th>" in grid and ">12</td>" in grid
    assert "L12" not in grid
    # The rack's bins render with the AISLE-LEVEL-BAY naming in the cell
    # titles (occupied bins show the container id instead, so match the
    # id pattern rather than one specific bin).
    assert re.search(r'title="1-\d{2}-\d[ABC]', grid)
    assert rack_summary_html(psch, "1").startswith("<div")
    assert "RECEIVING LANES" in lanes_html(psch)
