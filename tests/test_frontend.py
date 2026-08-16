import json

from data import store
from data.seed import seed
from server import BERTHS, build_state


def test_state_is_json_serialisable():
    seed()  # ensure the store is populated at DB_PATH
    state = build_state()
    assert isinstance(state, dict)
    assert "sim_now" in state
    assert "incoming" in state
    assert "outbound" in state
    assert "berths" in state
    assert "trace" in state
    assert "kpis" in state
    assert "psch" in state
    json.dumps(state)  # must not raise (datetimes already serialised)


def test_incoming_containers_have_ship_tracker_fields():
    seed()
    state = build_state()
    assert len(state["incoming"]) >= 6
    for entry in state["incoming"]:
        assert entry["container_id"]
        assert entry["status"] in (
            "En Route (Sea)", "Unloaded", "Depot", "En Route (Road)", "Arrived",
        )
        assert entry["vessel_name"] and entry["vessel_id"]
        assert entry["sea_arrival"] and entry["psch_receipt_eta"]
        assert "stow" in entry and entry["stow"]
        assert entry["receiving_area"] and entry["bin_location"]
        assert entry["hours_until"] is not None


def test_incoming_containers_carry_bay_plan_data():
    seed()
    state = build_state()
    for entry in state["incoming"]:
        assert entry["stow_bay"] is not None
        assert entry["stow_row"] and entry["stow_tier"]
        assert entry["bay_label"]
        assert len(entry["bay_cells"]) > 0
        # The bay plan grid HTML is present and highlights the container.
        html = entry["bayplan_html"]
        assert "bp-cell" in html and "bp-sel" in html
        assert entry["container_id"] in html
        # The selected cell's coordinates exist among the bay cells.
        coords = {(c["stack"], c["tier"]) for c in entry["bay_cells"]}
        assert (entry["stow_row"], entry["stow_tier"]) in coords
        # Every cell carries its industry cargo type (RF/DG/OOG/GP).
        assert all(c["type"] in ("RF", "DG", "OOG", "GP") for c in entry["bay_cells"])


def test_state_includes_psch_space_view():
    seed()
    state = build_state()
    psch = state["psch"]
    # Two rooms, 24 numbered aisles, 10 receiving lanes, 26 releasing lanes.
    assert {r["id"] for r in psch["rooms"]} == {"ambient", "cold_room"}
    assert sum(len(r["aisles"]) for r in psch["rooms"]) == 24
    assert psch["hazmat_aisle"] == "21"
    assert len(psch["lanes"]["receiving"]) == 10
    assert len(psch["lanes"]["releasing"]) == 26
    assert psch["stats"]["releasing_lanes"] == 26
    assert psch["stats"]["lanes_rcv_total"] == 10
    # 24 aisles x 12 levels x 3 bays x boxes A/B/C.
    assert psch["stats"]["bins_total"] == 24 * 12 * 3 * 3 == 2592
    # Every incoming MCC container has a planned rack bin in the facility.
    planned_bins = {e["bin_location"] for e in state["incoming"]}
    assert psch["stats"]["bins_used"] == len(planned_bins)
    for e in state["incoming"]:
        bid = e["bin_location"].removeprefix("Bin ")
        assert bid in psch["bins"]


def test_all_journey_stages_present_in_scenario():
    seed()
    state = build_state()
    stages = {e["status"] for e in state["incoming"]}
    # The scenario is designed to show the whole journey at once.
    assert stages >= {"En Route (Sea)", "Arrived"}
    assert "Unloaded" in stages or "Depot" in stages or "En Route (Road)" in stages


def test_container_sizes_are_20_or_40_only():
    seed()
    state = build_state()
    allowed = {"20FT", "40FT", "40HC"}
    for entry in state["incoming"]:
        assert entry["size"] in allowed
    for o in state["outbound"]:
        assert o["size"] in allowed


def test_bay_plan_shows_rational_white_gaps():
    from analysis.bayplan import build_bay_plan

    entry = {
        "container_id": "TESTU1234567",
        "bay_label": "9(10)",
        "bay_cells": [
            {
                "stack": 10, "tier": 86, "container_id": "TESTU1234567",
                "size": "40FT", "destination": "Singapore", "weight_t": 12.3, "is_mcc": True,
            },
            {
                "stack": 8, "tier": 84, "container_id": "TESTU7654321",
                "size": "40HC", "destination": "Hamburg", "weight_t": 15.0, "is_mcc": False,
            },
        ],
    }
    html = build_bay_plan(entry, clickable=True)
    # Uncovered slots are drawn as white gaps (boxes already discharged at
    # earlier ports), never as hatch-cover panels.
    assert "bp-empty" in html
    assert "bp-hatch" not in html


def _full_bay_entry(tracked=(8, 86), container="TRCK0000001", label="9(10)"):
    """A bay plan with every stack x tier occupied, like the simulator's."""
    from analysis.bayplan import STACKS_DISPLAY, TIERS_ABOVE, TIERS_BELOW

    cells = []
    n = 0
    for tier in TIERS_BELOW + TIERS_ABOVE:
        for stack in STACKS_DISPLAY:
            n += 1
            cells.append(
                {
                    "stack": stack, "tier": tier,
                    "container_id": f"TESTU{n:07d}", "size": "40FT",
                    "destination": "Singapore", "weight_t": 12.3,
                    "is_mcc": False, "type": "GP",
                }
            )
    for c in cells:
        if (c["stack"], c["tier"]) == tracked:
            c["container_id"] = container
    return {"container_id": container, "bay_label": label, "bay_cells": cells}


def test_bay_plan_is_a_hull_cross_section():
    from analysis.bayplan import TIERS_ABOVE, TIERS_BELOW, build_bay_plan

    html = build_bay_plan(_full_bay_entry(tracked=(8, 86)), clickable=True)
    rows = html.split("<tr>")
    cells_per_tier = {}
    for row in rows:
        for tier in TIERS_ABOVE + TIERS_BELOW:
            if f'<td class="bp-tier">{tier:02d}</td>' in row:
                cells_per_tier[tier] = row.count('class="bp-cell')
    # Above deck: full breadth (16 stacks) on every tier.
    assert cells_per_tier[92] == 16 and cells_per_tier[82] == 16
    # Below deck: the hull tapers like a cone toward the keel.
    assert cells_per_tier[18] == 16
    assert cells_per_tier[16] == 14
    assert cells_per_tier[14] == 12
    assert cells_per_tier[12] == 10
    assert cells_per_tier[10] == 8
    assert cells_per_tier[8] == 6
    assert cells_per_tier[6] == 4
    assert cells_per_tier[4] == 2
    assert cells_per_tier[2] == 2
    # The taper is drawn as centred void space, and the caption carries the
    # cell's coordinates in correct Bay-Row-Tier terms.
    assert "bp-void" in html
    assert "Bay 9(10) · Row 08 · Tier 86" in html


def test_bay_plan_never_hides_the_tracked_cell():
    from analysis.bayplan import build_bay_plan

    # Tracked cell sits at the outermost stack on a low hold tier — well
    # outside the tapered hull outline — but must never be hidden.
    html = build_bay_plan(_full_bay_entry(tracked=(16, 4), container="SELU0000001"))
    assert "SELU0000001" in html
    assert "bp-sel" in html
    assert "Row 16 · Tier 04" in html


def test_outbound_containers_are_bound_to_a_vessel_berth():
    seed()
    state = build_state()
    assert len(state["outbound"]) >= 2
    for o in state["outbound"]:
        assert o["bound_vessel_id"] and o["bound_vessel_name"]
        assert o["berth_id"] in {b["id"] for b in state["berths"]}
        assert o["status"] in ("staged", "released", "in_transit", "loaded")
        assert o["eta_loading_area"]
    # At least one group is already loaded (the demo's "Loaded" status).
    assert any(o["status"] == "loaded" for o in state["outbound"])


def test_berths_and_vessels_have_shape():
    seed()
    state = build_state()
    assert len(state["berths"]) == len(BERTHS) == 15
    for b in state["berths"]:
        assert {"id", "pier", "x", "y", "w", "h", "vessel"} <= set(b)
        assert 0 <= b["x"] <= 100 and 0 <= b["y"] <= 100
    assert any(b["vessel"] is None for b in state["berths"])
    assert any(b["vessel"] is not None for b in state["berths"])


def test_static_map_asset_is_served():
    assert __import__("server").MAP_FILE.is_file()


def test_planner_runs_on_fresh_store(tmp_path):
    from data.simulator import generate

    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=3, n_containers=40), db)

    plans = store.get_mcc_plans(db)
    outbounds = store.get_outbound_containers(db)
    assert plans == [] and outbounds == []  # the agent has not run yet

    from agents.mcc_planner import plan

    plan(db)
    plans = store.get_mcc_plans(db)
    outbounds = store.get_outbound_containers(db)
    assert plans and outbounds
    assert len(plans) >= 2
    # Every plan is time-consistent: unload after sea arrival, receipt last.
    for p in plans:
        assert p["sea_arrival"] < p["depot_arrive"] < p["road_depart"] < p["psch_receipt_eta"]
