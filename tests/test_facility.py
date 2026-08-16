"""Tests for the PSCH facility model and the space-utilisation view.

Covers the distribution-centre bin-naming convention (AISLE-LEVEL-BAY), the
ambient / cold-room split, cold-room and hazmat routing by the planner, the
dwell-based slotting rule (height optimisation), the staging lanes, and the
shared HTML builders.
"""
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
    plans = store.get_mcc_plans(db)
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

    assert psch["stats"]["bins_used"] == len(plans)
    assert psch["stats"]["bins_total"] == total_capacity()
    for p in plans:
        assert bin_id_of(p["bin_location"]) in psch["bins"]

    # Staging lanes: receiving lanes mirror the receiving areas (RA-n -> lane n,
    # numbered plainly 1..10) and every plan appears in exactly one receiving
    # lane. The releasing lanes are the 26 physical lanes at the dispatch area,
    # each staged by at most one consolidation container.
    assert len(psch["lanes"]["receiving"]) == 10
    assert len(psch["lanes"]["releasing"]) == 26
    all_rcv = [c for lane in psch["lanes"]["receiving"] for c in lane["containers"]]
    assert len(all_rcv) == len(plans)
    assert any(lane["group"] for lane in psch["lanes"]["releasing"])
    assert psch["stats"]["releasing_lanes"] == 26

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
    plans = store.get_mcc_plans(db)
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
    assert k["bins_used"] == len(plans)
    assert k["bins_total"] == total_capacity()
    assert k["lanes_releasing_used"] >= 1


def test_releasing_lanes_allocated_contiguously(tmp_path):
    """The agent allocates one lane (or a contiguous span) per consolidation group.

    Every staged group owns a range inside lanes 1..26, spans are contiguous
    (a group never jumps lanes), and no lane is shared between two groups.
    """
    db = _planned(tmp_path)
    outbounds = store.get_outbound_containers(db)
    psch = build_psch_space(
        store.get_mcc_plans(db), store.get_shipments(db), outbounds
    )

    groups = psch["releasing_groups"]
    assert groups
    assert len(groups) == len([o for o in outbounds if o.get("staging_lane_start")])
    used = []
    for g in groups:
        assert 1 <= g["lane_start"] <= g["lane_end"] <= 26
        assert len(g["lanes"]) == g["lane_end"] - g["lane_start"] + 1
        assert g["container_id"] and g["sources"]
        used.extend(g["lanes"])
    # Every staged lane in the visual carries exactly that group, and groups
    # never share a lane (contiguous non-overlapping allocation).
    assert len(used) == len(set(used))
    for lane in psch["lanes"]["releasing"]:
        if lane["group"]:
            group = next(g for g in groups if g["container_id"] == lane["group"])
            assert lane["lane"] in group["lanes"]


def test_psch_view_html_builders(tmp_path):
    from analysis.psch_view import (
        bin_grid_html,
        facility_collapsed_html,
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
    assert "L12" not in grid and "1-01-1A" in grid
    assert rack_summary_html(psch, "1").startswith("<div")
    assert "RECEIVING LANES" in lanes_html(psch)
    collapsed = facility_collapsed_html(psch)
    assert "psch-collapsed" in collapsed and "AMBIENT" in collapsed
