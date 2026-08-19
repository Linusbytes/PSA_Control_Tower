"""Seeded synthetic data generator for the MCC (multi-country consolidation) flow.

The world is one coherent cargo story: MCC cargo arrives at Tuas inside inbound
containers (some still at sea on inbound vessels, some already discharged and on
their way to PSCH), is deconsolidated at the PSA Supply Chain Hub, stored in
bins, and later re-consolidated into outbound containers that must make a
specific vessel's loading plan.

Every vessel carries a full **bay plan** (industry Bay-Row-Tier stowage): a set
of bays, each a grid of stacks (rows 1-16) and tiers (02-18 below deck, 82-92
above deck, hatch between). MCC containers are the cells marked as MCC cargo, so
their stowage cell is real and bay-parity correct (20ft in odd bays, 40ft in
even bays), and the UI can render the exact bay plan with the container's cell
highlighted.

The simulator produces the *world state*; the MCC planner agent
(agents/mcc_planner.py) derives journey times and writes the receiving/putaway/
consolidation plan.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import DRAYAGE_TOTAL, N_CONTAINERS, SEED, SIM_NOW
from models.schemas import (
    Booking,
    CargoFlag,
    CargoType,
    Container,
    CustomsStatus,
    DockSlotStatus,
    DrayageStatus,
    ServiceType,
    Shipment,
    SlaProfile,
    StorageZone,
    Vessel,
    VesselStatus,
    YardStatus,
)

CONTAINER_OWNERS = ["MSCU", "CMAU", "MAEU", "OOLU", "TCLU", "EMCU", "HLXU", "NYKU", "SEAU", "HLCU"]
SIZES = ["20FT", "40FT", "40HC"]  # 20ft or 40ft only; ~98% of the fleet is 40ft
SPECIAL_FLAGS = ["reefer", "hazmat", "oversized"]
YARD_BLOCKS = ["A", "B", "C", "D", "E", "F"]
# Discharge ports in the vessel's rotation, matching the reference
# preliminary stowage plan's legend (Singapore, Colombo, Piraeus, Rotterdam,
# Hamburg, Antwerp).
DESTINATIONS = ["Singapore", "Colombo", "Piraeus", "Rotterdam", "Hamburg", "Antwerp"]
BERTHS = [f"B{i}" for i in range(1, 16)]  # 15 berth markers drawn on the aerial map

# --- PSCH service flows -------------------------------------------------------
# Every inbound container that enters PSCH is tagged with the service flow it
# serves, so the port<->hub story is one list of containers with different
# downstream purposes:
#   mcc      marine deconsolidation, re-consolidated onto vessels (existing)
#   lcl      LCL deconsolidation, delivered by land (local SG or regional)
#   fcl      full-container-load, released whole by land
#   topup    container re-consolidation (topping up) then released
#   transload container-to-container transfer
FLOWS = ["mcc", "lcl", "fcl", "topup", "transload"]
# Local Singapore delivery areas (Central / Regional distribution).
LOCAL_DESTINATIONS = [
    "Changi", "Jurong East", "Woodlands", "Pasir Panjang", "Tanjong Pagar",
    "Paya Lebar", "Serangoon", "Tampines", "Bukit Timah", "Tuas Link",
]
# Land destinations beyond Singapore (by road via the Tuas / Woodlands
# causeway links, per the brief's central & regional distribution).
REGIONAL_DESTINATIONS = [
    "Johor Bahru", "Kuala Lumpur", "Port Klang", "Melaka", "Penang", "Kuantan",
]
TRUCK_LETTERS = ["T01", "T02", "T03", "T04", "T05", "T06", "T07", "T08"]

# --- Bay plan geometry (industry stowage notation) ------------------------------
# Stacks (rows) run 1..16 across the vessel; they are displayed even-side-first
# (16..02) then odd-side (01..15), mirroring how terminals draw bay plans.
STACKS = list(range(1, 17))
STACKS_DISPLAY = list(range(16, 0, -2)) + list(range(1, 17, 2))
# Tiers below deck (hold): 02..18; above deck: 82..92. The hatch sits between.
TIERS_BELOW = list(range(2, 20, 2))
TIERS_ABOVE = list(range(82, 94, 2))
N_BINS_40FT = 42  # even (40ft) bays per vessel -> ~98% of containers are 40ft
N_BINS_20FT = 1   # one odd (20ft) bay per vessel (20ft boxes below deck)

# Sea->PSCH stage durations (minutes) mirror config.py; the planner uses the
# same constants so the world and the plan agree.
_EST_RECEIPT_MIN = 390  # unload 180 + yard transfer 45 + dwell 120 + road 45


@dataclass
class StowCell:
    """One occupied cell in a vessel's bay plan."""

    vessel_id: str
    bay: int
    bay_label: str  # e.g. "33(34)" for a 40ft bay pair, "35" for a 20ft bay
    stack: int
    tier: int
    container_id: str
    size: str
    destination: str
    weight_t: float
    is_mcc: bool = False
    cargo_type: str = "GP"  # RF (reefer) | DG (dangerous) | OOG (out of gauge) | GP


@dataclass
class Scenario:
    """Everything the data layer needs to populate one simulated world."""

    containers: list[Container] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)
    yard: list[YardStatus] = field(default_factory=list)
    drayage: DrayageStatus | None = None
    slas: list[SlaProfile] = field(default_factory=list)
    shipments: list[Shipment] = field(default_factory=list)
    vessels: list[Vessel] = field(default_factory=list)
    stowage: list[StowCell] = field(default_factory=list)


def _unique_id(rng: random.Random, seen: set[str], prefix: str, digits: int) -> str:
    while True:
        value = f"{prefix}{rng.randint(10 ** (digits - 1), 10 ** digits - 1)}"
        if value not in seen:
            seen.add(value)
            return value


def _yard_location(rng: random.Random) -> str:
    block = rng.choice(YARD_BLOCKS)
    return f"Block {block}, Row {rng.randint(1, 30)}, Tier {rng.randint(1, 6)}"


def _special_handling(rng: random.Random) -> list[str]:
    flags: list[str] = []
    if rng.random() < 0.12:
        flags.append("reefer")
    if rng.random() < 0.08:
        flags.append("hazmat")
    if rng.random() < 0.05:
        flags.append("oversized")
    return flags


def _bay_label(bay: int) -> str:
    """40ft bays live in even numbers and span the preceding odd bay (33(34))."""
    return f"{bay - 1}({bay})" if bay % 2 == 0 else f"{bay}"


def _stow_string(bay: int, stack: int, tier: int) -> str:
    return f"Bay {_bay_label(bay)} · Row {stack:02d} · Tier {tier:02d}"


def _size_for_bay(rng: random.Random, bay: int) -> str:
    """20ft containers stow in odd bays; 40ft-family boxes in even bays."""
    if bay % 2 == 0:
        return rng.choice(["40FT", "40HC"])
    return "20FT"


def _vessel_fleet(sim_now: datetime) -> list[Vessel]:
    """The marine-side picture: 6 docked + 2 inbound vessels, 24/7 flow.

    ETAs/ETDs are deliberately spread across the whole day (including the
    small hours) so the scenario shows the continuous port<->PSCH<->port
    rhythm: some cargo already consolidated and loaded (Singapore group),
    some mid-journey at sea or on the road (Hamburg / Antwerp), some still
    far out (Piraeus). No shift structure — a vessel docks or sails at any
    hour.
    """
    return [
        Vessel(
            voyage_id="MAERSK-EG-24E",
            vessel_name="MAERSK EGYPT",
            status=VesselStatus.DOCKED,
            berth_id="B1",
            eta=sim_now - timedelta(hours=22),
            etd=sim_now + timedelta(hours=4),
            moves_planned=3400,
            destination="Singapore",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="ONE-TR-24E",
            vessel_name="ONE TRIUMPH",
            status=VesselStatus.DOCKED,
            berth_id="B2",
            eta=sim_now - timedelta(hours=6),
            etd=sim_now + timedelta(hours=22),
            moves_planned=2100,
            destination="Colombo",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="CMACGM-JS-24W",
            vessel_name="CMA CGM JACQUES SAADE",
            status=VesselStatus.DOCKED,
            berth_id="B3",
            eta=sim_now - timedelta(hours=5),
            etd=sim_now + timedelta(hours=30),
            moves_planned=2800,
            destination="Piraeus",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="EVER-AL-25E",
            vessel_name="EVER ALOT",
            status=VesselStatus.DOCKED,
            berth_id="B4",
            eta=sim_now - timedelta(hours=2, minutes=30),
            etd=sim_now + timedelta(hours=40),
            moves_planned=1900,
            destination="Rotterdam",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="HLC-NA-24W",
            vessel_name="HAPAG-LLOYD NAMIBIA",
            status=VesselStatus.DOCKED,
            berth_id="B7",
            eta=sim_now - timedelta(hours=14),
            etd=sim_now + timedelta(hours=34),
            moves_planned=1600,
            destination="Hamburg",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="SEA-TA-25W",
            vessel_name="SEALAND TAHITI",
            status=VesselStatus.DOCKED,
            berth_id="B8",
            eta=sim_now - timedelta(hours=3),
            etd=sim_now + timedelta(hours=40),
            moves_planned=1200,
            destination="Antwerp",
            distance_nm=0.0,
            speed_knots=0.0,
        ),
        Vessel(
            voyage_id="MSC-OS-24W",
            vessel_name="MSC OSCAR",
            status=VesselStatus.INBOUND,
            berth_id="B5",
            eta=sim_now + timedelta(hours=6),
            etd=sim_now + timedelta(hours=26),
            moves_planned=2600,
            destination="Hamburg",  # en-route vessel: its MCC cargo re-sails on it
            distance_nm=102.0,  # ~6h at 17 kn
            speed_knots=17.0,
        ),
        Vessel(
            voyage_id="OOCL-SE-25E",
            vessel_name="OOCL SEOUL",
            status=VesselStatus.INBOUND,
            berth_id="B6",
            eta=sim_now + timedelta(hours=14),
            etd=sim_now + timedelta(hours=34),
            moves_planned=2400,
            destination="Antwerp",  # en-route vessel: its MCC cargo re-sails on it
            distance_nm=280.0,  # ~14h at 20 kn
            speed_knots=20.0,
        ),
    ]


# Tier order from the bottom of the hold (02) to the top of the deck (92),
# used for the weight gradient: heavy boxes go deep, light boxes high.
TIER_ORDER = TIERS_BELOW + TIERS_ABOVE
TIER_POS = {t: i for i, t in enumerate(TIER_ORDER)}


def _stow_cargo_type(stack: int, tier: int) -> str:
    """Special-cargo placement per port safety rules:

    - Reefers cluster at stack 06, the vessel's reefer power-socket stacks.
    - Dangerous goods are segregated at the outermost stack 16.
    - Out-of-gauge units sit topmost (tier 92), loaded last / discharged first.
    """
    if stack == 6:
        return "RF"
    if stack == 16:
        return "DG"
    if tier == 92:
        return "OOG"
    return "GP"


def _bay_plan_modes(pos: float) -> tuple[str, str]:
    """(deck_mode, hold_mode) for a bay at position `pos` across the vessel.

    Mirrors the reference preliminary stowage plan: the midship block is fully
    worked, the shoulders carry partial bands, and the ends are sparse, so the
    rendered plan shows a realistic mix of full, partial and hatch-covered
    bays with the heavy work amidships.
    """
    if pos < 0.16:
        return ("HALF", "HALF")
    if pos < 0.32:
        return ("EMPTY", "SPARSE")
    if pos < 0.68:
        return ("FULL", "FULL")
    if pos < 0.80:
        return ("SPARSE", "EMPTY")
    return ("EMPTY", "SPARSE")


def _band_cells(
    mode: str, stacks: list[int], tiers: list[int], rng: random.Random,
    discharge: bool = True,
) -> list[tuple[int, int]]:
    """Which (stack, tier) cells a band mode occupies.

    Cells are always filled from the bottom of the band upward. The DECK band
    may be partially discharged: the top of a deck stack can be empty because
    those boxes were already lifted off at earlier ports (never above a gap).
    The HOLD band is always filled to its planned height — holds carry
    later-port cargo that has not been touched yet. FULL fills the whole band;
    HALF fills a centred block of stacks to about half height; SPARSE fills a
    single column; EMPTY leaves the band as white space.
    """
    if mode == "EMPTY":
        return []
    if mode == "FULL":
        stacks_in = stacks
        height = len(tiers)
    elif mode == "HALF":
        stacks_in = stacks[len(stacks) // 4: 3 * len(stacks) // 4] or stacks
        height = max(2, round(len(tiers) * 0.6))
    else:  # SPARSE
        stacks_in = [stacks[len(stacks) // 2 - 1]]  # a centreline-adjacent stack
        height = max(2, round(len(tiers) * 0.7))
    cells: list[tuple[int, int]] = []
    for stack in stacks_in:
        if discharge:
            # The topmost deck boxes may already have been discharged at
            # earlier foreign ports, so the stack is filled from the bottom up.
            drop = rng.choices([0, 1, 2], weights=[0.6, 0.3, 0.1])[0] if mode == "FULL" \
                else rng.choices([0, 1], weights=[0.7, 0.3])[0]
            keep = max(1, height - drop)
        else:
            keep = height  # holds stay full: later-port cargo not yet touched
        # The band tier lists run bottom -> top, so the first `keep` tiers are
        # the bottom of the stack: fill from there up, never above a gap.
        cells += [(stack, t) for t in tiers[:keep]]
    return cells


def _build_bay_plans(
    rng: random.Random,
    seen_ids: set[str],
    vessels: list[Vessel],
    mcc_counts: dict[str, int],
) -> list[StowCell]:
    """Generate each vessel's bay plan following port stowage standards.

    Arrangement principles (as a terminal planner would apply them):
    - Preliminary-plan loading: bays are worked like a real preliminary
      stowage plan — the midship block fully loaded, the shoulders partial,
      the ends sparse. Boxes already discharged at earlier ports leave
      rational white gaps at the top of the DECK stacks (above the hatch,
      nothing floats above a gap); the HOLD band stays fully loaded because
      it carries later-port cargo that has not been touched yet.
    - Two-tone bays: each bay carries its own port mix, one destination on
      the deck band (tiers 82-92) and another in the hold (02-18), the way
      the reference plan colour-codes cargo by discharge port.
    - Weight: heavy containers at the bottom of the hold, light ones on top
      of the deck, so the vessel's centre of gravity stays low.
    - Specials: reefers at the power-socket stacks, DG segregated, OOG topmost.
    - Parity: 40ft-family in even bays, 20ft in odd bays; ~98% of the fleet
      is 40ft, so each vessel carries ~30 40ft bays and one 20ft bay
      (20ft boxes below deck; above-deck hatch covers are drawn as panels).
    """
    even_bays = list(range(2, 92, 2))  # 45 even (40ft) bays to sample from
    odd_bays = list(range(1, 93, 2))    # 46 odd (20ft) bays to sample from
    stowage: list[StowCell] = []
    for vessel in vessels:
        bays = sorted(
            rng.sample(even_bays, N_BINS_40FT) + rng.sample(odd_bays, N_BINS_20FT)
        )
        # Bay ports cycle through the rotation so the plan shows the classic
        # patchwork of destination colours; the vessel's own next port is
        # guaranteed to sit on some deck bands (where its MCC cargo is stowed).
        offset = DESTINATIONS.index(vessel.destination or DESTINATIONS[0])
        cells: list[StowCell] = []
        for i, bay in enumerate(bays):
            label = _bay_label(bay)
            even = bay % 2 == 0
            # Odd (20ft) bays carry boxes below deck; above deck the hatch
            # covers are drawn as panels, so no deck cells are created.
            deck_tiers = TIERS_ABOVE if even else []
            hold_tiers = TIERS_BELOW
            pos = i / max(1, len(bays) - 1)
            deck_mode, hold_mode = _bay_plan_modes(pos)
            if not even:
                deck_mode = "EMPTY"
                if hold_mode == "EMPTY":
                    hold_mode = "SPARSE"  # keep the 20ft bay alive
            # Two-tone bay: one destination per band, like the reference plan.
            deck_dest = DESTINATIONS[(i + offset) % len(DESTINATIONS)]
            hold_dest = DESTINATIONS[(i + offset + 3) % len(DESTINATIONS)]
            for stack, tier in _band_cells(deck_mode, STACKS, deck_tiers, rng, discharge=True) + \
                    _band_cells(hold_mode, STACKS, hold_tiers, rng, discharge=False):
                size = _size_for_bay(rng, bay)
                # Heavy low, light high: weight falls as the tier rises.
                weight = round(max(5.0, 27.0 - 1.1 * TIER_POS[tier] + rng.uniform(-1.5, 1.5)), 1)
                owner = rng.choice(CONTAINER_OWNERS)
                cells.append(
                    StowCell(
                        vessel_id=vessel.voyage_id,
                        bay=bay,
                        bay_label=label,
                        stack=stack,
                        tier=tier,
                        container_id=_unique_id(rng, seen_ids, owner, 7),
                        size=size,
                        destination=deck_dest if tier >= 82 else hold_dest,
                        weight_t=weight,
                        is_mcc=False,
                        cargo_type=_stow_cargo_type(stack, tier),
                    )
                )
        # Mark the MCC cells (cargo discharged at Tuas): they sit in the
        # accessible deck tiers of bays whose deck band carries the vessel's
        # own next-port destination, so the MCC container and its cell share
        # the final-destination colour.
        deck_indices = [
            i for i, c in enumerate(cells)
            if c.tier >= 82 and c.destination == (vessel.destination or "")
        ]
        target = min(mcc_counts.get(vessel.voyage_id, 0), len(deck_indices))
        if target:
            picks = rng.sample(deck_indices, target)

            # Give the vessel a healthy share of reefer MCC cells (~1/3) and at
            # least one dangerous-goods cell, so PSCH's cold room and the
            # segregated hazmat aisle both show real utilisation in the
            # facility view. Containers inherit their special handling from the
            # cell, so reefers always stay at the power-socket stack and DG at
            # the segregated outermost stack.
            def _promote(ctype: str, want: int, keep: tuple[str, ...] = ()) -> None:
                avail = [
                    i for i in deck_indices
                    if cells[i].cargo_type == ctype and i not in picks
                ]
                have = sum(1 for i in picks if cells[i].cargo_type == ctype)
                swaps = [
                    i for i in range(len(picks))
                    if cells[i].cargo_type not in (ctype,) + keep
                ]
                for _ in range(min(want - have, len(avail), len(swaps))):
                    picks[swaps.pop()] = avail.pop()

            _promote("RF", max(1, round(target * 0.35)))
            _promote("DG", 1, keep=("RF",))

            # Safety net: keep at least one reefer cell even on tiny vessels.
            reefer_deck = [i for i in deck_indices if cells[i].cargo_type == "RF"]
            if reefer_deck and not any(cells[i].cargo_type == "RF" for i in picks):
                picks[rng.randrange(target)] = rng.choice(reefer_deck)
            for idx in picks:
                cells[idx].is_mcc = True
        stowage.extend(cells)
    return stowage


def _flow_counts(rng: random.Random, n: int) -> dict[str, int]:
    """Exact per-flow container counts summing to n (MCC/LCL/FCL/Top Up/Transload)."""
    weights = {"mcc": 0.36, "lcl": 0.26, "fcl": 0.16, "topup": 0.14, "transload": 0.08}
    counts = {f: round(n * w) for f, w in weights.items()}
    diff = n - sum(counts.values())
    keys = list(counts)
    for i in range(abs(diff)):
        counts[keys[i % len(keys)]] += 1 if diff > 0 else -1
    return {f: max(0, c) for f, c in counts.items()}


def _land_destination(rng: random.Random) -> str:
    """A land release target: local Singapore delivery or regional (by road)."""
    return rng.choice(LOCAL_DESTINATIONS + REGIONAL_DESTINATIONS)


def generate(
    seed: int = SEED,
    n_containers: int = N_CONTAINERS,
    sim_now: datetime = SIM_NOW,
) -> Scenario:
    """Generate a deterministic scenario. Same seed -> same world.

    ~85% of the population is PSCH-bound cargo across the five service flows
    (MCC / LCL / FCL / Top Up / Transload), every container taken from a real
    bay-plan cell on its carrying vessel (so stowage plans and berth
    highlights work for the whole list); the rest are plain import /
    transshipment boxes that dwell in the yard. Arrivals are spread across the
    whole 24h cycle — no shift structure. Every PSCH container gets exactly
    one booking and a set of pallet shipments.
    """
    rng = random.Random(seed)
    seen_ids: set[str] = set()

    vessels = _vessel_fleet(sim_now)
    vessel_by_id = {v.voyage_id: v for v in vessels}

    n_psch = max(24, round(n_containers * 0.85))
    flow_counts = _flow_counts(rng, n_psch)

    # Distribute PSCH containers across vessels (spread evenly; the journey-
    # stage story still comes from each vessel's ETA, docked vs inbound).
    picks = rng.choices(vessels, weights=[3, 3, 3, 3, 2, 2, 2, 2], k=n_psch)
    mcc_counts = Counter(v.voyage_id for v in picks)

    stowage = _build_bay_plans(rng, seen_ids, vessels, mcc_counts)
    cells = [c for c in stowage if c.is_mcc]
    n_actual = min(len(cells), n_psch)

    # Exact flow labels, shuffled, zipped onto the cells in order.
    flow_labels: list[str] = []
    for flow, count in flow_counts.items():
        flow_labels += [flow] * count
    rng.shuffle(flow_labels)
    flow_labels = flow_labels[:n_actual]

    consignee_ids = [f"CUST-{4000 + i}" for i in range(12)]
    shipper_ids = [f"SHP-{100 + i}" for i in range(40)]

    # --- PSCH containers come from the bay-plan cells -------------------------
    containers: list[Container] = []
    for cell, flow in zip(cells, flow_labels):
        vessel = vessel_by_id[cell.vessel_id]
        containers.append(
            Container(
                container_id=cell.container_id,
                voyage_id=cell.vessel_id,
                status="discharged",
                discharge_timestamp=sim_now - timedelta(hours=rng.randint(2, 72)),
                yard_location=_yard_location(rng),
                size_type=cell.size,
                cargo_flag=CargoFlag.DECONSOLIDATION_REQUIRED,
                customs_status=rng.choices(
                    [CustomsStatus.CLEARED, CustomsStatus.PENDING, CustomsStatus.HELD],
                    weights=[0.85, 0.1, 0.05],
                )[0],
                consignee_id=rng.choice(consignee_ids),
                # Special handling matches the stowage cell: the container is a
                # reefer/DG/OOG unit because that is where the planner stowed it.
                special_handling=(
                    ["reefer"] if cell.cargo_type == "RF"
                    else ["hazmat"] if cell.cargo_type == "DG"
                    else ["oversized"] if cell.cargo_type == "OOG"
                    else []
                ),
                flow=flow,
                destination=(
                    _land_destination(rng)
                    if flow in ("lcl", "fcl", "topup")
                    else (vessel.destination if flow == "mcc" else None)
                ),
                vessel_cutoff=None,
                stow_position=_stow_string(cell.bay, cell.stack, cell.tier),
                stow_bay=cell.bay,
                stow_row=cell.stack,
                stow_tier=cell.tier,
            )
        )

    # Safety net: the MCC picks above already guarantee a reefer, but if the
    # world were regenerated with unusual counts, force one anyway (and keep
    # its bay-plan cell coherent as an RF cell too).
    mcc_containers = [c for c in containers if c.cargo_flag == CargoFlag.DECONSOLIDATION_REQUIRED]
    if mcc_containers and not any("reefer" in c.special_handling for c in mcc_containers):
        mcc_containers[-1].special_handling = ["reefer"]
        for cell in stowage:
            if cell.container_id == mcc_containers[-1].container_id:
                cell.cargo_type = "RF"

    # --- Plain import / transshipment boxes (yard dwell only) -----------------
    for _ in range(max(0, n_containers - n_actual)):
        owner = rng.choice(CONTAINER_OWNERS)
        container_id = _unique_id(rng, seen_ids, owner, 7)
        if rng.random() < 0.6:
            cargo_flag = CargoFlag.IMPORT
            customs = rng.choices(
                [CustomsStatus.CLEARED, CustomsStatus.PENDING, CustomsStatus.HELD],
                weights=[0.6, 0.3, 0.1],
            )[0]
        else:
            cargo_flag = CargoFlag.TRANSSHIPMENT
            customs = rng.choices(
                [CustomsStatus.CLEARED, CustomsStatus.PENDING],
                weights=[0.8, 0.2],
            )[0]
        containers.append(
            Container(
                container_id=container_id,
                voyage_id=rng.choice(vessels).voyage_id,
                status="discharged",
                discharge_timestamp=sim_now - timedelta(hours=rng.randint(1, 96)),
                yard_location=_yard_location(rng),
                size_type=("20FT" if rng.random() < 0.02 else rng.choice(["40FT", "40HC"])),
                cargo_flag=cargo_flag,
                customs_status=customs,
                consignee_id=rng.choice(consignee_ids),
                special_handling=_special_handling(rng),
                flow="yard",
            )
        )

    service_by_flow = {
        "mcc": ServiceType.LCL_DECONSOLIDATION,
        "lcl": ServiceType.LCL_DECONSOLIDATION,
        "fcl": ServiceType.FCL_RELEASE,
        "topup": ServiceType.TOP_UP,
        "transload": ServiceType.TRANSLOADING,
    }
    bookings: list[Booking] = []
    for c in containers:
        if c.cargo_flag != CargoFlag.DECONSOLIDATION_REQUIRED:
            continue
        vessel = vessel_by_id[c.voyage_id]
        receipt_est = (vessel.eta or sim_now) + timedelta(minutes=_EST_RECEIPT_MIN)
        if "reefer" in c.special_handling:
            storage_zone = StorageZone.COLD_ROOM
        elif "hazmat" in c.special_handling:
            storage_zone = StorageZone.HAZMAT
        else:
            storage_zone = StorageZone.AMBIENT
        bookings.append(
            Booking(
                booking_id=_unique_id(rng, seen_ids, "PSCH-CFS-", 5),
                linked_container_id=c.container_id,
                service_type=service_by_flow[c.flow],
                shipper_ids=rng.sample(shipper_ids, k=rng.randint(1, 3)),
                required_by=receipt_est + timedelta(hours=12),
                storage_zone=storage_zone,
                dock_slot_status=rng.choices(
                    [DockSlotStatus.UNASSIGNED, DockSlotStatus.ASSIGNED, DockSlotStatus.OCCUPIED],
                    weights=[0.5, 0.3, 0.2],
                )[0],
                processing_queue_position=rng.randint(0, 8),
                destination=(
                    c.destination
                    if c.destination
                    else (vessel.destination if c.flow == "mcc" else None)
                ),
            )
        )

    yard = [
        YardStatus(
            block=block,
            zone="export" if i % 2 else "import",
            utilization_pct=round(rng.uniform(35, 95), 1),
            gate_lanes_available=rng.randint(2, 8),
        )
        for i, block in enumerate(YARD_BLOCKS)
    ]

    available_trucks = rng.randint(3, max(3, DRAYAGE_TOTAL - 1))
    drayage = DrayageStatus(total_trucks=DRAYAGE_TOTAL, available_trucks=available_trucks)

    slas = [
        SlaProfile(
            consignee_id=consignee_id,
            priority_tier=1 if idx < 3 else (2 if idx < 7 else 3),
            sla_hours={1: 4, 2: 8, 3: 24}[1 if idx < 3 else (2 if idx < 7 else 3)],
        )
        for idx, consignee_id in enumerate(consignee_ids)
    ]

    # Pallet shipments: each PSCH container carries 2-4 palletised cargo units
    # (deconsolidated for mcc/lcl, the container's own cargo for fcl/topup,
    # the cargo being transferred for transload).
    shipments: list[Shipment] = []
    for c in containers:
        if c.cargo_flag != CargoFlag.DECONSOLIDATION_REQUIRED:
            continue
        vessel = vessel_by_id[c.voyage_id]
        receipt_est = (vessel.eta or sim_now) + timedelta(minutes=_EST_RECEIPT_MIN)
        booking = next(b for b in bookings if b.linked_container_id == c.container_id)
        # A 40ft deconsolidation container typically breaks down into several
        # palletised units (2-6 here); the bin it is put away to holds that
        # whole block until the pallets are picked for consolidation.
        n_pieces = rng.randint(2, 6)
        total_volume = rng.uniform(18, 45)  # cbm of palletised cargo
        weights = [rng.random() for _ in range(n_pieces)]
        weight_sum = sum(weights)
        for piece in weights:
            volume = round(max(1.0, total_volume * piece / weight_sum), 1)
            if rng.random() < 0.15:
                cargo_type = CargoType.REEFER
            elif rng.random() < 0.08:
                cargo_type = CargoType.HAZMAT
            else:
                cargo_type = CargoType.AMBIENT
            shipments.append(
                Shipment(
                    shipment_id=_unique_id(rng, seen_ids, "SHIP-", 6),
                    shipper_id=rng.choice(booking.shipper_ids),
                    destination=c.destination or vessel.destination,
                    cargo_type=cargo_type,
                    volume_cbm=volume,
                    ready_time=receipt_est + timedelta(minutes=30),
                    service_type=(
                        ServiceType.MCC_CONSOLIDATION
                        if c.flow == "mcc"
                        else booking.service_type
                    ),
                    consignee_id=rng.choice(consignee_ids),
                    source_container_id=c.container_id,
                )
            )

    return Scenario(
        containers=containers,
        bookings=bookings,
        yard=yard,
        drayage=drayage,
        slas=slas,
        shipments=shipments,
        vessels=vessels,
        stowage=stowage,
    )
