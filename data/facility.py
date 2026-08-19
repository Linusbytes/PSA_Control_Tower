"""PSCH facility model: the physical space of the PSA Supply Chain Hub.

PSCH is a two-room container freight station:

* **AMBIENT** — dry MCC cargo, aisles 1-21 (aisle 21 segregated for dangerous
  goods), 12 levels x 3 bays x boxes A/B/C.
* **COLD ROOM** — reefer cargo, aisles 22-24, 12 levels x 3 bays x boxes A/B/C.

Racks and bins follow conventional distribution-centre naming: aisles are
**numbered from 1**, and every bin is ``AISLE-LEVEL-BAY`` (e.g. ``1-12-2A`` =
Aisle 1, Level 12, Bay 2A) with the box letter (A/B/C) written directly on the
bay number. Each aisle has a fixed number of levels (12, the height in the
rack) and each level has a fixed number of bays (3), every bay holding boxes
A/B/C. Each bin is one pallet location in a rack.

The MCC planner agent reserves bins ahead of the cargo's arrival (robot
putaway plan), and a **slotting rule** picks the rack level (height) from how
long the cargo is predicted to dwell before release: cargo picked soon is
stored at the floor levels for fast retrieval, slower movers higher up. A bin
is *reserved* while the container is en route and *occupied* once the
container has arrived at PSCH.

Staging lanes sit at the inbound and outbound areas of PSCH: **receiving lanes**
(numbered plainly 1..10, one per receiving area RA-1..RA-10) list the inbound
container numbers unloaded/staged in each lane, and **releasing lanes**
(numbered 1..26, parked side by side like dock doors) stage the pallets of each
consolidation container before it is collected.
"""
from __future__ import annotations

import random
import re
from datetime import datetime

from config import (
    PSCH_AMBIENT_AISLES,
    PSCH_BAYS_PER_LEVEL,
    PSCH_BOXES,
    PSCH_COLD_AISLES,
    PSCH_HAZMAT_AISLE,
    PSCH_LEVELS_PER_AISLE,
    RECEIVING_LANES,
    RELEASING_LANES,
    SEED,
    SIM_NOW,
)

# Room definitions: label, temperature band, aisle list, levels/aisle, bays/level.
ROOMS = {
    "ambient": {
        "label": "AMBIENT",
        "temp": "+15 °C … +25 °C",
        "note": f"dry MCC cargo · aisle {PSCH_HAZMAT_AISLE} segregated (dangerous goods)",
        "aisles": list(PSCH_AMBIENT_AISLES),
        "levels": PSCH_LEVELS_PER_AISLE,
        "bays": PSCH_BAYS_PER_LEVEL,
        "boxes": list(PSCH_BOXES),
    },
    "cold_room": {
        "label": "COLD ROOM",
        "temp": "0 °C … −18 °C",
        "note": "reefer cargo · chilled / frozen bands",
        "aisles": list(PSCH_COLD_AISLES),
        "levels": PSCH_LEVELS_PER_AISLE,
        "bays": PSCH_BAYS_PER_LEVEL,
        "boxes": list(PSCH_BOXES),
    },
}

HAZMAT_AISLE = PSCH_HAZMAT_AISLE

# "1-12-2A" -> aisle "1", level 12, bay 2, box "A".
_BIN_RE = re.compile(r"^(\d+)-(\d{2})-(\d+)([A-C])$")


def aisle_of_bin(bin_id: str) -> str:
    """Aisle (rack) id of a bin, e.g. ``1-12-2A`` -> ``1``, ``22-03-1B`` -> ``22``."""
    m = _BIN_RE.match(bin_id or "")
    return m.group(1) if m else (bin_id or "").split("-", 1)[0]


def level_of_bin(bin_id: str) -> int:
    """Level (height) of a bin, e.g. ``1-12-2A`` -> 12."""
    m = _BIN_RE.match(bin_id or "")
    return int(m.group(2)) if m else 0


def bay_of_bin(bin_id: str) -> int:
    """Bay number of a bin, e.g. ``1-12-2A`` -> 2."""
    m = _BIN_RE.match(bin_id or "")
    return int(m.group(3)) if m else 0


def box_of_bin(bin_id: str) -> str:
    """Box letter of a bin, e.g. ``1-12-2A`` -> ``A``."""
    m = _BIN_RE.match(bin_id or "")
    return m.group(4) if m else ""


def bin_room(bin_id: str) -> str:
    """Room a bin belongs to: cold-room aisles are 22-24, everything else ambient."""
    return "cold_room" if aisle_of_bin(bin_id) in ROOMS["cold_room"]["aisles"] else "ambient"


def room_of_aisle(aisle: str) -> str:
    return "cold_room" if (aisle or "") in ROOMS["cold_room"]["aisles"] else "ambient"


def iter_bins(room: str) -> list[str]:
    """Every bin id in a room, ordered aisle -> level -> bay -> box (DC convention)."""
    cfg = ROOMS[room]
    return [
        f"{aisle}-{level:02d}-{bay}{box}"
        for aisle in cfg["aisles"]
        for level in range(1, cfg["levels"] + 1)
        for bay in range(1, cfg["bays"] + 1)
        for box in cfg["boxes"]
    ]


# The facility geometry is static, so the (expensive) bin list derivation is
# cached once per room / per aisle and reused by every state poll. With 24
# aisles x 12 levels x 3 bays x 3 boxes this turns repeated O(n) scans into
# O(1) lookups — important because build_psch_space() runs every 8s poll.
_ROOM_BINS: dict[str, list[str]] = {}
_AISLE_BINS: dict[tuple[str, str], list[str]] = {}


def _cached_room_bins(room: str) -> list[str]:
    if room not in _ROOM_BINS:
        _ROOM_BINS[room] = iter_bins(room)
    return _ROOM_BINS[room]


def _cached_aisle_bins(room: str, aisle: str) -> list[str]:
    key = (room, aisle)
    if key not in _AISLE_BINS:
        _AISLE_BINS[key] = [b for b in _cached_room_bins(room) if aisle_of_bin(b) == aisle]
    return _AISLE_BINS[key]


def aisle_bins(room: str, aisle: str) -> list[str]:
    return _cached_aisle_bins(room, aisle)


def room_capacity(room: str) -> int:
    return len(_cached_room_bins(room))


def total_capacity() -> int:
    return sum(room_capacity(room) for room in ROOMS)


def bin_id_of(bin_location: str | None) -> str:
    """Strip the display prefix ('Bin 1-12-2A' -> '1-12-2A')."""
    return (bin_location or "").removeprefix("Bin ")


def is_bin(bin_id: str | None) -> bool:
    """True when the id is a real AISLE-LEVEL-BAY rack bin (vs a staging slot).

    Whole-container flows (FCL / Top Up / Transload) are staged in yard slots
    and bays rather than deconsolidated into rack bins, so their plan's
    ``bin_location`` is a descriptive label that must never be counted as a
    rack bin in the facility view.
    """
    return bool(_BIN_RE.match(bin_id or ""))


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# --- Dwell-stock floor (prior-wave carryover) --------------------------------
# A real container freight station is never empty: cargo from previous waves
# sits in the racks while the current wave's containers arrive, get put away
# and are picked. Without that floor the facility view would show a nearly
# empty warehouse (the complaint this code fixes), so a deterministic
# "dwell-stock" layer fills every aisle into a realistic utilisation band:
#   ambient aisles     35-60% (a few quiet aisles ~10%)
#   hazmat aisle 21    30-50% (DG is always present; segregated)
#   cold room          30-60% (reefer stock)
# The floor is derived from (SEED, bin id) so it is identical on every poll
# and after every wave regeneration, and it tops the aisle up to the band
# AFTER the current wave's own plan bins are counted, so the total per-aisle
# utilisation sits inside the band for the whole wave.
STOCK_BAND = (0.35, 0.60)
STOCK_QUIET_BAND = (0.08, 0.20)
STOCK_QUIET_PROB = 0.15
HAZMAT_BAND = (0.30, 0.50)
COLD_BAND = (0.30, 0.60)


def _aisle_stock_target(aisle: str, cap: int, wave_used: int) -> int:
    """How many dwell-stock bins an aisle should carry, given its wave usage."""
    rng = random.Random(f"{SEED}:aisle-band:{aisle}")
    if aisle == HAZMAT_AISLE:
        lo, hi = HAZMAT_BAND
    elif aisle in ROOMS["cold_room"]["aisles"]:
        lo, hi = COLD_BAND
    elif rng.random() < STOCK_QUIET_PROB:
        lo, hi = STOCK_QUIET_BAND
    else:
        lo, hi = STOCK_BAND
    util = rng.uniform(lo, hi)
    return max(0, round(cap * util) - wave_used)


def dwell_stock(wave_bin_ids: set[str], seed: int = SEED) -> dict[str, dict]:
    """Deterministic prior-wave dwell-stock floor, one pallet per stock bin.

    ``wave_bin_ids`` are the rack bins already claimed by the current wave's
    plans (a bin is never double-booked). Returns ``{bin_id: stock_bin}`` where
    each stock bin mirrors the wave-bin shape with ``stock=True`` and a
    ``DWL-<bin>`` pallet-group id, so the classic UI grid renders it as
    occupied without it ever appearing in the inbound-container list.
    """
    stock: dict[str, dict] = {}
    for room_id, cfg in ROOMS.items():
        for aisle in cfg["aisles"]:
            aisle_ids = _cached_aisle_bins(room_id, aisle)
            wave_used = sum(1 for b in aisle_ids if b in wave_bin_ids)
            target = _aisle_stock_target(aisle, len(aisle_ids), wave_used)
            free = [b for b in aisle_ids if b not in wave_bin_ids]
            rng = random.Random(f"{SEED}:stock:{aisle}")
            picks = rng.sample(free, min(target, len(free)))
            for bid in picks:
                stock[bid] = {
                    "id": bid,
                    "container_id": f"DWL-{bid}",
                    "status": "Arrived",
                    "arrived": True,
                    "receipt_eta": None,
                    "pallet_pick_time": None,
                    "consolidation_group": None,
                    "pallets": 1,
                    "stock": True,
                }
    return stock


def build_psch_space(
    plans: list[dict],
    shipments: list[dict],
    outbounds: list[dict],
    sim_now: datetime = SIM_NOW,
) -> dict:
    """Assemble the PSCH space-utilisation view state.

    From the agent's plans we know exactly which bin each MCC container is
    planned into (and when it arrives / when its pallets are picked), so the
    facility can be drawn with every rack, every bin and every staging lane,
    colour-coded by the container's journey status.
    """
    from agents.mcc_planner import journey_status, outbound_status  # lazy: avoid import cycle

    pallets_by_container: dict[str, int] = {}
    for s in shipments:
        cid = s.get("source_container_id")
        if cid:
            pallets_by_container[cid] = pallets_by_container.get(cid, 0) + 1

    bins: dict[str, dict] = {}
    assigned: dict[str, str] = {}
    wave_bin_ids: set[str] = set()
    for p in plans:
        bid = bin_id_of(p.get("bin_location"))
        if not is_bin(bid):
            continue  # whole-container staging slots are not rack bins
        status = journey_status(p, sim_now)
        bins[bid] = {
            "id": bid,
            "container_id": p["container_id"],
            "status": status,
            "arrived": status == "Arrived",
            "receipt_eta": p["psch_receipt_eta"].isoformat() if p.get("psch_receipt_eta") else None,
            "pallet_pick_time": p["pallet_pick_time"].isoformat() if p.get("pallet_pick_time") else None,
            "consolidation_group": p.get("consolidation_group"),
            "pallets": pallets_by_container.get(p["container_id"], 0),
        }
        assigned[p["container_id"]] = bid
        wave_bin_ids.add(bid)

    # Prior-wave dwell stock: fills every aisle into a realistic utilisation
    # band on top of the current wave's plan bins (never double-booking a bin).
    stock = dwell_stock(wave_bin_ids)
    bins.update(stock)

    # Rooms with per-aisle occupancy (used/capacity + the assigned bins).
    rooms = []
    for room_id, cfg in ROOMS.items():
        aisles = []
        used_total = 0
        stock_total = 0
        for aisle in cfg["aisles"]:
            aisle_ids = aisle_bins(room_id, aisle)
            cap = len(aisle_ids)
            # Wave bins only in the per-aisle list (the dwell stock is drawn
            # from the shared bins dict, so it never floods the receiving /
            # dispatch summaries with background stock).
            entries = [bins[b] for b in aisle_ids if b in bins and not bins[b].get("stock")]
            stock_n = sum(1 for b in aisle_ids if b in stock)
            used = len(entries) + stock_n
            used_total += used
            stock_total += stock_n
            aisles.append(
                {
                    "id": aisle,
                    "levels": cfg["levels"],
                    "bays": cfg["bays"],
                    "boxes": len(cfg["boxes"]),
                    "used": used,
                    "cap": cap,
                    "pct": round(100 * used / cap, 1) if cap else 0.0,
                    "stock": stock_n,
                    "bins": entries,
                }
            )
        cap = room_capacity(room_id)
        rooms.append(
            {
                "id": room_id,
                "label": cfg["label"],
                "temp": cfg["temp"],
                "note": cfg["note"],
                "aisles": aisles,
                "used": used_total,
                "cap": cap,
                "pct": round(100 * used_total / cap, 1) if cap else 0.0,
                "stock": stock_total,
            }
        )

    # Staging lanes at the inbound / outbound areas of PSCH.
    # Receiving lanes map 1:1 to receiving areas (RA-n -> lane n); each lane
    # lists the containers currently being unloaded/staged there. Membership is
    # LIVE against the sim clock: a container occupies its lane from the moment
    # it arrives at PSCH until its putaway move completes (cargo moved to its
    # bin), so the lanes fill and empty as the wave progresses — and the UI can
    # flash each lane when a container arrives or leaves.
    receiving = []
    for i, lane in enumerate(RECEIVING_LANES, start=1):
        cids = [
            p["container_id"]
            for p in plans
            if str(p.get("receiving_area", "")).startswith(f"RA-{i}")
            and p.get("psch_receipt_eta") is not None
            and sim_now >= p["psch_receipt_eta"]
            and (p.get("move_end") is None or sim_now < p["move_end"])
        ]
        receiving.append({"lane": lane, "containers": cids})

    # Releasing lanes: the 26 physical lanes at the PSCH dispatch area. Each
    # consolidation container is allocated one lane or a contiguous group of
    # adjacent lanes (staging_lane_start..end) where its pallets wait to be
    # loaded into that same container; every lane belongs to at most one group.
    releasing = []
    for i, lane in enumerate(RELEASING_LANES, start=1):
        ob = next(
            (
                o
                for o in outbounds
                if (o.get("staging_lane_start") or 0) <= i <= (o.get("staging_lane_end") or 0)
            ),
            None,
        )
        if ob is not None:
            releasing.append(
                {
                    "lane": lane,
                    "index": i,
                    "group": ob["container_id"],
                    "destination": ob.get("destination"),
                    "status": ob.get("status"),
                    "sources": ob.get("source_container_ids", []),
                    "pallets": len(ob.get("source_shipment_ids") or []),
                }
            )
        else:
            releasing.append(
                {
                    "lane": lane,
                    "index": i,
                    "group": None,
                    "destination": None,
                    "status": None,
                    "sources": [],
                    "pallets": 0,
                }
            )

    releasing_groups = []
    for o in outbounds:
        start, end = o.get("staging_lane_start"), o.get("staging_lane_end")
        if start and end:
            releasing_groups.append(
                {
                    "container_id": o["container_id"],
                    "destination": o.get("destination"),
                    "status": o.get("status"),
                    "lane_start": start,
                    "lane_end": end,
                    "lanes": [str(j) for j in range(start, end + 1)],
                    "pallets": len(o.get("source_shipment_ids") or []),
                    "sources": o.get("source_container_ids", []),
                }
            )

    pallets_planned = sum(pallets_by_container.values())
    return {
        "rooms": rooms,
        "lanes": {"receiving": receiving, "releasing": releasing},
        "releasing_groups": releasing_groups,
        "bins": bins,
        "assigned": assigned,
        "hazmat_aisle": HAZMAT_AISLE,
        "stats": {
            "bins_total": total_capacity(),
            "bins_used": len(bins),
            "stock_bins": len(stock),
            "bin_util": round(100 * len(bins) / total_capacity(), 1) if total_capacity() else 0.0,
            "pallets_planned": pallets_planned,
            "pallets_in_storage": sum(
                b["pallets"] for b in bins.values() if b["arrived"]
            ),
            "lanes_rcv_used": sum(1 for lane in receiving if lane["containers"]),
            "lanes_rcv_total": len(receiving),
            "lanes_rel_used": sum(1 for lane in releasing if lane["group"]),
            "releasing_lanes": len(releasing),
            "releasing_groups": len(releasing_groups),
        },
    }
