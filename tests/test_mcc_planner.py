from config import SIM_NOW
from data import store
from data.simulator import generate
from agents import mcc_planner


def _seeded(tmp_path, seed=11, n=60):
    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=seed, n_containers=n), db)
    return db


def test_planner_covers_every_mcc_container(tmp_path):
    db = _seeded(tmp_path)
    containers = store.get_containers(db)
    mcc = [c for c in containers if c["cargo_flag"] == "deconsolidation_required"]
    mcc_planner.plan(db)

    plans = store.get_mcc_plans(db)
    assert len(plans) == len(mcc)
    assert {p["container_id"] for p in plans} == {c["container_id"] for c in mcc}


def test_journey_timeline_is_consistent(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    for p in store.get_mcc_plans(db):
        # Journey timeline is time-consistent for every flow.
        assert p["sea_arrival"] < p["unload_end"] <= p["depot_arrive"] < p["road_depart"] < p["psch_receipt_eta"]
        assert p["staging_start"] == p["psch_receipt_eta"]
        assert p["staging_start"] < p["staging_end"] <= p["move_start"] < p["move_end"]
        assert p["receiving_area"].startswith("RA-")
    # Bin flows (mcc / lcl) deconsolidate into rack bins; whole-container
    # flows (fcl / topup / transload) are staged whole, so only bin flows
    # carry a Bin/stacker/lane-number plan.
    for p in store.get_mcc_plans(db):
        if p["flow"] in ("mcc", "lcl"):
            assert p["bin_location"].startswith("Bin ")
            assert p["putaway_robot"].startswith("Stacker ")
            # Releasing lanes are plain numbers, possibly a span "5–7".
            assert p["release_lane"][0].isdigit()
        else:
            assert not p["bin_location"].startswith("Bin ")


def test_journey_status_derivation():
    # All stages reachable from the plan times, checked against SIM_NOW.
    plan = {
        "sea_arrival": SIM_NOW + __import__("datetime").timedelta(hours=2),
        "depot_arrive": SIM_NOW + __import__("datetime").timedelta(hours=6),
        "road_depart": SIM_NOW + __import__("datetime").timedelta(hours=8),
        "psch_receipt_eta": SIM_NOW + __import__("datetime").timedelta(hours=9),
    }
    assert mcc_planner.journey_status(plan) == "En Route (Sea)"

    plan["sea_arrival"] = SIM_NOW - __import__("datetime").timedelta(hours=1)
    assert mcc_planner.journey_status(plan) == "Unloaded"

    plan["depot_arrive"] = SIM_NOW - __import__("datetime").timedelta(hours=1)
    assert mcc_planner.journey_status(plan) == "Depot"

    plan["road_depart"] = SIM_NOW - __import__("datetime").timedelta(hours=1)
    assert mcc_planner.journey_status(plan) == "En Route (Road)"

    plan["psch_receipt_eta"] = SIM_NOW - __import__("datetime").timedelta(minutes=30)
    assert mcc_planner.journey_status(plan) == "Arrived"

    assert mcc_planner.journey_status(None) == "En Route (Sea)"


def test_receiving_plan_responds_to_volume_rate(tmp_path):
    # A larger scenario (more arriving containers in the next 6h) opens more
    # receiving areas than a tiny one.
    small = _seeded(tmp_path, seed=5, n=20)
    big = _seeded(tmp_path, seed=6, n=120)
    mcc_planner.plan(small)
    mcc_planner.plan(big)
    areas_small = {p["receiving_area"].split(" · ")[0] for p in store.get_mcc_plans(small)}
    areas_big = {p["receiving_area"].split(" · ")[0] for p in store.get_mcc_plans(big)}
    assert len(areas_big) >= len(areas_small)


def test_outbound_groups_are_coherent(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    plans = {p["container_id"]: p for p in store.get_mcc_plans(db)}
    outbounds = store.get_outbound_containers(db)

    assert len(outbounds) >= 2
    for o in outbounds:
        assert len(o["source_container_ids"]) >= 2
        assert len({plans[cid]["vessel_destination"] for cid in o["source_container_ids"]}) == 1
        assert o["destination"] == plans[o["source_container_ids"][0]]["vessel_destination"]
        # The bound vessel heads to the same destination; it may already be
        # alongside at the berth (waiting for loading) or still arriving.
        vessels = {v["voyage_id"]: v for v in store.get_vessels(db)}
        assert vessels[o["bound_vessel_id"]]["destination"] == o["destination"]
        assert vessels[o["bound_vessel_id"]]["status"] in ("docked", "inbound")
        # Stuffing happens before the vessel sails; lane released at stuffing end.
        assert o["stuffing_start"] < o["stuffing_end"] <= o["lane_release_time"] < o["road_depart"] < o["eta_loading_area"]
        assert o["eta_loading_area"] <= o["vessel_etd"]
        # Loading cell is a real, bay-parity-correct cell of the bound vessel's
        # bay plan, written in Bay-Row-Tier notation.
        assert o["stow_bay"] and o["stow_row"] and o["stow_tier"]
        assert "Bay " in o["stow_position"] and "Row " in o["stow_position"] and "Tier " in o["stow_position"]
        stow_key = {
            (s["vessel_id"], s["bay"], s["stack"], s["tier"]) for s in store.get_vessel_stowage(db)
        }
        assert (o["bound_vessel_id"], o["stow_bay"], o["stow_row"], o["stow_tier"]) in stow_key

    # The demo shows both loading pictures: a vessel alongside waiting for
    # loading and one still arriving en route.
    bound_statuses = {
        vessels[o["bound_vessel_id"]]["status"] for o in outbounds
    }
    assert "docked" in bound_statuses and "inbound" in bound_statuses

    # Pallet picks are consistent: never before the container arrives, and
    # inside the stuffing window of its group.
    by_group: dict[str, list] = {}
    for cid, p in plans.items():
        if p["consolidation_group"]:
            by_group.setdefault(p["consolidation_group"], []).append(p)
    for ob in outbounds:
        group = by_group.get(ob["container_id"], [])
        if not group:
            continue
        for p in group:
            assert p["pallet_pick_time"] >= p["psch_receipt_eta"]
            assert ob["stuffing_start"] <= p["pallet_pick_time"] <= ob["stuffing_end"]


def test_outbound_status_derivation():
    o = {
        "lane_release_time": SIM_NOW + __import__("datetime").timedelta(hours=3),
        "road_depart": SIM_NOW + __import__("datetime").timedelta(hours=4),
        "eta_loading_area": SIM_NOW + __import__("datetime").timedelta(hours=5),
    }
    assert mcc_planner.outbound_status(o) == "staged"
    o["lane_release_time"] = SIM_NOW - __import__("datetime").timedelta(hours=1)
    assert mcc_planner.outbound_status(o) == "released"
    o["road_depart"] = SIM_NOW - __import__("datetime").timedelta(hours=1)
    assert mcc_planner.outbound_status(o) == "in_transit"
    o["eta_loading_area"] = SIM_NOW - __import__("datetime").timedelta(minutes=30)
    assert mcc_planner.outbound_status(o) == "loaded"


def test_planner_is_deterministic(tmp_path):
    db1 = _seeded(tmp_path, seed=42)
    db2 = _seeded(tmp_path, seed=42)
    mcc_planner.plan(db1)
    mcc_planner.plan(db2)
    p1 = store.get_mcc_plans(db1)
    p2 = store.get_mcc_plans(db2)
    assert [(p["container_id"], p["psch_receipt_eta"].isoformat(), p["bin_location"])
            for p in p1] == [(p["container_id"], p["psch_receipt_eta"].isoformat(), p["bin_location"])
                             for p in p2]
    o1 = [(o["container_id"], o["eta_loading_area"].isoformat()) for o in store.get_outbound_containers(db1)]
    o2 = [(o["container_id"], o["eta_loading_area"].isoformat()) for o in store.get_outbound_containers(db2)]
    assert o1 == o2
