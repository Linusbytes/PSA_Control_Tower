from collections import Counter

from config import SIM_NOW
from models.schemas import CargoFlag, VesselStatus
from data.simulator import generate


def test_generator_is_deterministic():
    a = generate(seed=42, n_containers=60)
    b = generate(seed=42, n_containers=60)
    assert [c.container_id for c in a.containers] == [c.container_id for c in b.containers]
    assert [b.booking_id for b in a.bookings] == [b.booking_id for b in b.bookings]
    assert [v.voyage_id for v in a.vessels] == [v.voyage_id for v in b.vessels]


def test_counts_and_mcc_linkage():
    s = generate(seed=42, n_containers=60)
    assert len(s.containers) == 60

    mcc = [c for c in s.containers if c.cargo_flag == CargoFlag.DECONSOLIDATION_REQUIRED]
    assert len(s.bookings) == len(mcc) > 0
    assert {b.linked_container_id for b in s.bookings} == {c.container_id for c in mcc}


def test_every_mcc_container_has_voyage_stow_and_pallets():
    s = generate(seed=42, n_containers=60)
    mcc = [c for c in s.containers if c.cargo_flag == CargoFlag.DECONSOLIDATION_REQUIRED]
    for c in mcc:
        assert c.voyage_id
        assert c.stow_position and "Bay" in c.stow_position and "Row" in c.stow_position and "Tier" in c.stow_position
        # Structured stowage: bay-parity correct (20ft in odd bays, 40ft in even).
        assert c.stow_bay is not None and c.stow_row and c.stow_tier
        if c.stow_bay % 2 == 0:
            assert c.size_type != "20FT"
        else:
            assert c.size_type == "20FT"
    pallet_sources = {p.source_container_id for p in s.shipments}
    assert pallet_sources == {c.container_id for c in mcc}


def test_stowage_is_persisted_and_consistent(tmp_path):
    from data import store

    s = generate(seed=42, n_containers=60)
    assert len(s.stowage) > 1000  # full bay plans across the fleet
    assert any(c.is_mcc for c in s.stowage)

    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(s, db)
    rows = store.get_vessel_stowage(db)
    assert len(rows) == len(s.stowage)
    by_key = {(r["vessel_id"], r["bay"], r["stack"], r["tier"]): r for r in rows}

    for c in s.containers:
        if c.cargo_flag != CargoFlag.DECONSOLIDATION_REQUIRED:
            continue
        cell = by_key.get((c.voyage_id, c.stow_bay, c.stow_row, c.stow_tier))
        assert cell is not None
        assert cell["container_id"] == c.container_id
        assert cell["size"] == c.size_type
        assert cell["is_mcc"] is True


def test_only_20ft_and_40ft_sizes_and_98pct_are_40ft():
    s = generate(seed=42, n_containers=60)
    # No 45-footers anywhere: every container in the world is 20ft or 40ft.
    for c in s.containers:
        assert c.size_type in ("20FT", "40FT", "40HC")
    sizes = Counter(c.size for c in s.stowage)
    assert "45HC" not in sizes
    n_40 = sizes["40FT"] + sizes["40HC"]
    # ~98% of the berth-plan containers are 40-footers.
    assert n_40 / sum(sizes.values()) >= 0.97
    # Plain import/transshipment boxes follow the same sizing rule (the
    # population is small at n=60, so allow a little slack for one 20ft box).
    non_mcc = [c for c in s.containers if c.cargo_flag != CargoFlag.DECONSOLIDATION_REQUIRED]
    assert non_mcc
    assert sum(1 for c in non_mcc if c.size_type != "20FT") / len(non_mcc) >= 0.8


def test_bay_plans_have_preliminary_plan_occupancy():
    s = generate(seed=42, n_containers=60)
    for vessel in s.vessels:
        cells = [c for c in s.stowage if c.vessel_id == vessel.voyage_id]
        assert cells, "every vessel must carry a bay plan"
        per_bay = Counter(c.bay for c in cells)
        # A preliminary plan is partly worked: some bays nearly full (the
        # midship block, a little emptier where boxes were discharged at
        # earlier ports), some partial, some sparse.
        assert any(v >= 200 for v in per_bay.values())  # a nearly full 40ft bay
        assert min(per_bay.values()) <= 16  # sparse bays exist at the ends
        assert max(per_bay.values()) < 16 * 15  # never totally full
        # Occupied cells are unique per (bay, stack, tier).
        keys = {(c.bay, c.stack, c.tier) for c in cells}
        assert len(keys) == len(cells)
        # A nearly full 40ft bay still holds all 16 stacks.
        full_bay = next(b for b, v in per_bay.items() if v >= 200)
        assert {c.stack for c in cells if c.bay == full_bay} == set(range(1, 17))


def test_no_container_floats_above_a_gap():
    s = generate(seed=42, n_containers=60)
    # Band tiers, bottom -> top.
    deck_band = sorted(range(82, 94, 2))
    hold_band = sorted(range(2, 20, 2))
    for vessel in s.vessels:
        bays = {c.bay for c in s.stowage if c.vessel_id == vessel.voyage_id}
        for bay in bays:
            for stack in range(1, 17):
                got_deck = sorted(
                    c.tier for c in s.stowage
                    if c.vessel_id == vessel.voyage_id and c.bay == bay
                    and c.stack == stack and c.tier >= 82
                )
                got_hold = sorted(
                    c.tier for c in s.stowage
                    if c.vessel_id == vessel.voyage_id and c.bay == bay
                    and c.stack == stack and c.tier < 82
                )
                # A stack is filled from the bottom of its band upward: the
                # top may be empty (already discharged at earlier ports), but
                # a container never floats above an empty slot.
                assert got_deck == deck_band[: len(got_deck)]
                assert got_hold == hold_band[: len(got_hold)]


def test_holds_stay_full_and_only_deck_is_discharged():
    s = generate(seed=42, n_containers=60)
    hold_band = sorted(range(2, 20, 2))  # 02..18, bottom -> top
    deck_band = sorted(range(82, 94, 2))  # 82..92
    full_bay_seen = False
    deck_discharged_seen = False
    for vessel in s.vessels:
        cells = [c for c in s.stowage if c.vessel_id == vessel.voyage_id]
        per_bay = Counter(c.bay for c in cells)
        for bay in sorted(per_bay):
            bay_cells = [c for c in cells if c.bay == bay]
            if per_bay[bay] >= 200:  # a nearly-full midship 40ft bay
                full_bay_seen = True
                # Holds stay full: every stack carries its complete 02..18 band.
                for stack in range(1, 17):
                    hold = sorted(
                        c.tier for c in bay_cells if c.stack == stack and c.tier < 82
                    )
                    assert hold == hold_band
            # Whitespace lives only above the hatch (deck band discharged).
            for stack in range(1, 17):
                deck = sorted(
                    c.tier for c in bay_cells if c.stack == stack and c.tier >= 82
                )
                if deck and deck != deck_band:
                    deck_discharged_seen = True
    assert full_bay_seen
    assert deck_discharged_seen  # at least one deck stack shows discharged gaps


def test_stowage_is_two_tone_per_bay_and_follows_weight_rules():
    s = generate(seed=42, n_containers=60)
    vessel_by_id = {v.voyage_id: v for v in s.vessels}
    by_key = {(c.vessel_id, c.bay, c.stack, c.tier): c for c in s.stowage}

    mcc = [c for c in s.containers if c.cargo_flag == CargoFlag.DECONSOLIDATION_REQUIRED]
    assert mcc
    for c in mcc:
        cell = by_key[(c.voyage_id, c.stow_bay, c.stow_row, c.stow_tier)]
        # MCC cargo is discharged at Tuas -> stowed in the accessible deck
        # tiers, and carries the vessel's next-port destination colour.
        assert cell.tier >= 82
        assert cell.destination == vessel_by_id[c.voyage_id].destination
        # The container's special handling matches its stowage cell.
        expected = (
            ["reefer"] if cell.cargo_type == "RF"
            else ["hazmat"] if cell.cargo_type == "DG"
            else ["oversized"] if cell.cargo_type == "OOG"
            else []
        )
        assert c.special_handling == expected

    # Preliminary-plan banding: within every bay the deck band and the hold
    # band each carry a single destination, and they differ (two-tone bays).
    for vessel in s.vessels:
        for bay in {c.bay for c in s.stowage if c.vessel_id == vessel.voyage_id}:
            deck = {
                c.destination for c in s.stowage
                if c.vessel_id == vessel.voyage_id and c.bay == bay and c.tier >= 82
            }
            hold = {
                c.destination for c in s.stowage
                if c.vessel_id == vessel.voyage_id and c.bay == bay and c.tier < 82
            }
            if deck:
                assert len(deck) == 1, f"deck band of bay {bay} is not single-tone"
            if hold:
                assert len(hold) == 1, f"hold band of bay {bay} is not single-tone"
            if deck and hold:
                assert deck != hold, f"bay {bay} should be two-tone"

    # Specials are stowed by rule, never at random.
    for cell in s.stowage:
        if cell.cargo_type == "RF":
            assert cell.stack == 6  # reefers at the power-socket stack
        if cell.cargo_type == "DG":
            assert cell.stack == 16  # dangerous goods segregated outermost
        if cell.cargo_type == "OOG":
            assert cell.tier == 92  # out of gauge topmost

    # Weight: heavy at the bottom of the hold, light on top of the deck.
    def avg(tier: int) -> float:
        ws = [c.weight_t for c in s.stowage if c.tier == tier]
        return sum(ws) / len(ws)

    prev = None
    for tier in [2, 4, 6, 8, 10, 12, 14, 16, 18, 82, 84, 86, 88, 90, 92]:
        a = avg(tier)
        if prev is not None:
            assert a <= prev + 0.5  # never meaningfully heavier higher up
        prev = a
    assert avg(2) > avg(92)


def test_bay_labels_follow_industry_convention():
    s = generate(seed=7, n_containers=60)
    labels = {c.bay_label for c in s.stowage}
    for label in labels:
        # 20ft bays are single odd numbers; 40ft bays are pairs "odd(even)".
        if "(" in label:
            odd, even = label[:-1].split("(")
            assert int(even) % 2 == 0 and int(odd) == int(even) - 1
        else:
            assert int(label) % 2 == 1


def test_fleet_covers_journey_stages_and_berth_plan():
    s = generate(seed=42, n_containers=60)
    statuses = [v.status for v in s.vessels]
    assert VesselStatus.DOCKED in statuses and VesselStatus.INBOUND in statuses
    assert len({v.berth_id for v in s.vessels}) == len(s.vessels)  # unique berths

    docked = [v for v in s.vessels if v.status == VesselStatus.DOCKED]
    inbound = [v for v in s.vessels if v.status == VesselStatus.INBOUND]
    # Spread across the journey: some vessels arrived long ago, one mid-journey,
    # one recent, and the inbound ones are still at sea with tracking data.
    past = sorted(v.eta for v in docked)
    assert all(v.eta <= SIM_NOW for v in docked)
    assert past[0] < SIM_NOW - timedelta_hours(10)
    for v in inbound:
        assert v.eta > SIM_NOW
        assert v.distance_nm and v.distance_nm > 0
        assert v.speed_knots and v.speed_knots > 0
    for v in docked:
        assert v.distance_nm == 0.0


def timedelta_hours(h: int):
    from datetime import timedelta

    return timedelta(hours=h)


def test_no_duplicate_ids():
    s = generate(seed=7, n_containers=60)
    ids = [c.container_id for c in s.containers] + [b.booking_id for b in s.bookings]
    ids += [p.shipment_id for p in s.shipments]
    assert len(ids) == len(set(ids))
