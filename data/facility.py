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


def aisle_bins(room: str, aisle: str) -> list[str]:
    return [b for b in iter_bins(room) if aisle_of_bin(b) == aisle]


def room_capacity(room: str) -> int:
    return len(iter_bins(room))


def total_capacity() -> int:
    return sum(room_capacity(room) for room in ROOMS)


def bin_id_of(bin_location: str | None) -> str:
    """Strip the display prefix ('Bin 1-12-2A' -> '1-12-2A')."""
    return (bin_location or "").removeprefix("Bin ")


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    for p in plans:
        bid = bin_id_of(p.get("bin_location"))
        if not bid:
            continue
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

    # Rooms with per-aisle occupancy (used/capacity + the assigned bins).
    rooms = []
    for room_id, cfg in ROOMS.items():
        aisles = []
        used_total = 0
        for aisle in cfg["aisles"]:
            entries = [bins[b] for b in aisle_bins(room_id, aisle) if b in bins]
            cap = len(aisle_bins(room_id, aisle))
            used_total += len(entries)
            aisles.append(
                {
                    "id": aisle,
                    "levels": cfg["levels"],
                    "bays": cfg["bays"],
                    "boxes": len(cfg["boxes"]),
                    "used": len(entries),
                    "cap": cap,
                    "pct": round(100 * len(entries) / cap, 1) if cap else 0.0,
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
            }
        )

    # Staging lanes at the inbound / outbound areas of PSCH.
    # Receiving lanes map 1:1 to receiving areas (RA-n -> lane n); each lane
    # lists the containers unloaded/staged there (no physical layout drawn,
    # just the chips as in the classic view).
    receiving = []
    for i, lane in enumerate(RECEIVING_LANES, start=1):
        cids = [
            p["container_id"]
            for p in plans
            if str(p.get("receiving_area", "")).startswith(f"RA-{i}")
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
