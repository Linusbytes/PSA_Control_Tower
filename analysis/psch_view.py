"""PSCH facility HTML builders (shared by the classic and Streamlit UIs).

Renders the PSCH space-utilisation visualisation from the state produced by
``data.facility.build_psch_space``: the two rooms (AMBIENT / COLD ROOM) each
laid out Receiving -> Storage -> Dispatch with a one-way flow, every rack
(aisle) as a clickable cell, the rack detail as an AISLE-LEVEL-BAY bin grid,
and the receiving / releasing staging lanes at the inbound and outbound areas.

The classic UI (server.py) renders the same structure client-side in JS and
shares ``FACILITY_CSS``; the Streamlit UI (dashboard/app.py) calls the HTML
builders directly.
"""
from __future__ import annotations

from data.facility import HAZMAT_AISLE, ROOMS, aisle_of_bin, room_of_aisle

STATUS_SHORT = {
    "En Route (Sea)": "SEA",
    "Unloaded": "UNLD",
    "Depot": "DEP",
    "En Route (Road)": "ROAD",
    "Arrived": "ARR",
    "staged": "STG",
    "released": "REL",
    "in_transit": "TRANSIT",
    "loaded": "LOADED",
}

FACILITY_CSS = """
/* ============ PSCH Space — facility visualisation (shared) ============ */
/* The facility keeps a fixed minimum width and scrolls inside its own box on
   narrow panes, so the lanes columns never squeeze the rooms column to zero
   and nothing overlaps a neighbouring column. */
.psch-facility { width:100%; overflow-x:auto; }
.psch-cols { table-layout:fixed; border-spacing:0; width:100%; }
.psch-cols.psch-fac-cols { min-width:640px; }
.psch-cols > tbody > tr > td, .psch-cols > tr > td { vertical-align:top; }
.psch-rooms-col { min-width:400px; }
.psch-lanes-col { width:120px; padding-right:6px; }
.psch-lanes-col.last { padding-right:0; padding-left:6px; }
.psch-lanes-head { background:#000080; color:#FFFFFF; font-weight:bold; font-size:9px;
                   text-align:center; padding:2px; border:1px solid #404040; margin-bottom:3px; }
.psch-lanes-head.out { background:#1F4E79; }
.psch-room { border:2px solid #404040; background:#C0C0C0; margin-bottom:8px; }
.psch-room-head { background:#000080; color:#FFFFFF; font-weight:bold; padding:3px 6px; font-size:11px; }
.psch-room-head.cold { background:#1F4E79; }
.psch-room-body { border-collapse:collapse; width:100%; table-layout:fixed; }
.psch-zone { padding:4px; min-height:120px; vertical-align:top; }
.psch-zone-recv { background:#CFE3F2; width:104px; border-right:2px dashed #808080; }
.psch-zone-store { background:#E4E4E4; }
.psch-zone-disp { background:#CFE3F2; width:104px; border-left:2px solid #404040; }
.psch-zone-title { font-weight:bold; font-size:9px; text-align:center; margin-bottom:4px; }
.psch-zone-note { font-size:8px; color:#404040; text-align:center; margin-top:3px; }
.psch-rack-grid { display:flex; flex-wrap:wrap; gap:6px; justify-content:center; padding:4px; }
.psch-rack { border:2px solid #808080; background:#FFFFFF; padding:4px 8px; text-align:center;
             min-width:76px; cursor:pointer; }
.psch-rack:hover { background:#FFFFCC; }
.psch-rack.sel { border-color:#FF0000; outline:2px solid #FF0000; }
.psch-rack.hl { outline:3px solid #FF00FF; }
.psch-rack.hl-out { outline:3px solid #FFCC00; background:#FFF9D9; }
.psch-inline.hl-out { background:#FFF9D9; }
.psch-rack-id { font-weight:bold; font-size:11px; }
.psch-rack-meta { font-size:8px; color:#404040; }
.psch-rack-tag { font-size:7px; color:#FFFFFF; background:#B07000; padding:0 2px; margin-left:2px; }
.psch-occ-bar { height:5px; background:#E0E0E0; border:1px solid #808080; margin-top:3px; }
.psch-occ-bar i { display:block; height:100%; background:#000080; }
.psch-rack.cold .psch-occ-bar i { background:#1F6FB2; }
.psch-rack.hazmat .psch-occ-bar i { background:#B07000; }
.psch-flow { border-top:2px solid #808080; background:#C0C0C0; font-size:9px; padding:2px 6px;
             text-align:center; color:#404040; }
.psch-bin-grid { border-collapse:collapse; }
.psch-bin-grid th { background:#D0D0D0; font-size:8px; border:1px solid #808080; padding:1px 2px; }
.psch-bin-grid td { border:1px solid #808080; min-width:54px; height:36px; text-align:center;
                    font-size:8px; vertical-align:top; padding:1px 2px; }
.psch-bin-grid td.psch-bin-empty { background:#F0F0F0; color:#A8A8A8; }
.psch-bin-grid td.psch-bin-reserved { background:#FFF3C4; cursor:pointer; }
.psch-bin-grid td.psch-bin-occupied { background:#DDFFDD; cursor:pointer; }
.psch-bin-grid td.sel { outline:3px solid #FF0000; }
.psch-bin-grid td.hl { outline:3px solid #FF00FF; }
.psch-bin-grid td.psch-b-box { background:#C0C0C0; font-size:8px; font-weight:bold; width:22px; }
.psch-grid-wrap { overflow-x:auto; }
.psch-collapsed { padding:2px; font-size:9px; line-height:1.5; }
.psch-collapsed-room { margin-bottom:4px; }
.psch-collapsed-lanes { color:#404040; margin-top:4px; border-top:1px dashed #808080; padding-top:3px; }
/* PSCH Space is organised into INBOUND (receiving lanes + incoming containers)
   and OUTBOUND (ambient room, cold room, releasing lanes) sections; the right
   column holds the ship tracker and the agent reasoning. */
.psch-sec-head { background:#000080; color:#FFFFFF; font-weight:bold; font-size:10px;
                 padding:3px 6px; margin:8px 0 3px 0; }
.psch-sec-head.out { background:#1F4E79; margin-top:12px; }
.psch-rel-legend { font-size:8px; color:#404040; margin-top:2px; }
/* Physical releasing lanes (outbound staging): 26 narrow vertical blocks
   parked side by side in one row, like dock doors along a warehouse wall
   (the wall is longer than the pane, so the row scrolls horizontally). Each
   block is one numbered lane; a consolidation container occupies one lane or
   a contiguous group of adjacent lanes, colour-coded per group. Red outline
   (sel) = the selected container's allocated lanes; the inspector shows the
   cargo staged on them. */
.psch-rel-grid { display:flex; flex-wrap:nowrap; gap:3px; padding:2px;
                 align-items:flex-start; overflow-x:auto; }
.psch-rel-lane { width:46px; height:112px; border:2px solid #606060; border-radius:3px;
                 background:#FFFFFF; padding:3px 2px; cursor:pointer; box-sizing:border-box;
                 overflow:hidden; text-align:center; flex:0 0 auto; }
.psch-rel-lane.empty { background:#EDEDED; color:#909090; cursor:default; }
.psch-rel-lane.sel { outline:3px solid #FF0000; }
.psch-rel-lane.hl { outline:3px solid #FF00FF; }
.psch-rel-lane .psch-rel-id { font-weight:bold; font-size:9px; }
.psch-rel-lane .psch-rel-group { font-size:7px; font-weight:bold; line-height:1.2;
                                 word-wrap:break-word; word-break:break-all; }
.psch-rel-lane .psch-rel-pallets { font-size:7px; color:#404040; }
.psch-rel-lane.rel-c1 { background:#DDEBFF; border-color:#2F6FB2; }
.psch-rel-lane.rel-c2 { background:#E6F2D9; border-color:#4E7D2E; }
.psch-rel-lane.rel-c3 { background:#FDEBD9; border-color:#B9702F; }
.psch-rel-lane.rel-c4 { background:#F2DCE5; border-color:#9E3E63; }
.psch-rel-lane.rel-c5 { background:#EAE0F5; border-color:#6A3FA0; }
.psch-rel-lane.rel-c6 { background:#D9EDE8; border-color:#2E7D6B; }
.psch-bin-grid th.psch-b-bay { background:#B8B8B8; border-bottom:none; }
.psch-bin-grid .psch-bin-id { font-size:7px; color:#404040; }
.psch-bin-grid .psch-bin-cid { font-size:8px; font-weight:bold; word-break:break-all; }
.psch-bin-grid .psch-bin-meta { font-size:7px; color:#404040; }
.psch-lane-chip { border:1px solid #404040; background:#FFFFFF; margin-bottom:3px; padding:2px 3px; font-size:8px; }
.psch-lane-id { font-weight:bold; font-size:9px; }
.psch-lane-count { float:right; color:#404040; }
.psch-lane-cids { color:#404040; word-wrap:break-word; font-size:7px; }
.psch-lane-cids b { color:#000000; }
.psch-inline { font-size:8px; color:#404040; word-wrap:break-word; }
.psch-inline b { color:#000000; }
"""


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _status_short(status: str | None) -> str:
    return STATUS_SHORT.get(status or "", status or "")


def _rack_html(
    aisle: dict,
    room_id: str,
    selected_aisle: str | None,
    hl_aisle: str | None,
    hl_out_aisles: set[str] | None = None,
) -> str:
    cls = "psch-rack"
    if room_id == "cold_room":
        cls += " cold"
    if room_id == "ambient" and aisle["id"] == HAZMAT_AISLE:
        # The segregated hazmat aisle of the ambient room.
        cls += " hazmat"
    if aisle["id"] == selected_aisle:
        cls += " sel"
    if hl_aisle and aisle["id"] == hl_aisle:
        cls += " hl"
    if hl_out_aisles and aisle["id"] in hl_out_aisles:
        cls += " hl-out"
    tag = ""
    if room_id == "ambient" and aisle["id"] == HAZMAT_AISLE:
        tag = '<span class="psch-rack-tag">DG</span>'
    elif room_id == "cold_room":
        tag = '<span class="psch-rack-tag">❄</span>'
    bar_w = max(2, int(aisle["pct"])) if aisle["pct"] else 0
    return (
        f'<div class="{cls}" data-aisle="{_esc(aisle["id"])}" '
        f'title="Aisle {_esc(aisle["id"])} · {aisle["used"]}/{aisle["cap"]} bins · {aisle["pct"]}% used">'
        f'<div class="psch-rack-id">Aisle {_esc(aisle["id"])}{tag}</div>'
        f'<div class="psch-rack-meta">{aisle["used"]}/{aisle["cap"]} bins · {aisle["pct"]}%</div>'
        f'<div class="psch-occ-bar"><i style="width:{bar_w}%"></i></div></div>'
    )


def _container_line(b: dict, hl_cid: str | None, hl_out_cids: set[str] | None = None) -> str:
    if b.get("container_id") == hl_cid:
        cls = " hl"
    elif hl_out_cids and b.get("container_id") in hl_out_cids:
        cls = " hl-out"  # cargo of the selected outbound container
    else:
        cls = ""
    return (
        f'<div class="psch-inline{cls}"><b>{_esc(b["container_id"])}</b> '
        f'({_status_short(b.get("status"))})</div>'
    )


def _zone_rooms_html(
    room: dict, side: str, hl_cid: str | None, hl_out_cids: set[str] | None = None
) -> str:
    """Compact summary of the containers flowing into/out of a room's zone."""
    entries = [b for a in room["aisles"] for b in a["bins"]]
    if side == "receiving":
        lines = [_container_line(b, hl_cid, hl_out_cids) for b in entries[:8]]
        if len(entries) > 8:
            lines.append(f'<div class="psch-inline">+{len(entries) - 8} more</div>')
        return "".join(lines) or '<div class="psch-inline">no cargo assigned yet</div>'
    # dispatch side: pallets picked to consolidation groups
    pallets = sum(b.get("pallets", 0) for b in entries)
    groups = sorted({b.get("consolidation_group") for b in entries if b.get("consolidation_group")})
    out = f'<div class="psch-inline"><b>{pallets} pallets</b> to be picked</div>'
    if groups:
        shown = ", ".join(_esc(g) for g in groups[:4])
        if len(groups) > 4:
            shown += f" +{len(groups) - 4} more"
        out += f'<div class="psch-inline">groups: {shown}</div>'
    else:
        out += '<div class="psch-inline">groups: next wave</div>'
    return out


def room_html(room: dict, selected_aisle: str | None = None, hl_aisle: str | None = None,
              hl_cid: str | None = None, hl_out_aisles: set[str] | None = None,
              hl_out_cids: set[str] | None = None) -> str:
    """One room block: header, Receiving | Storage (rack grid) | Dispatch, flow arrow."""
    cold = " cold" if room["id"] == "cold_room" else ""
    # The box header ("Ambient Room"/"Cold Room") already names the room, so the
    # inner bar carries only temperature + occupancy — no duplicate header.
    head = (
        f'<div class="psch-room-head{cold}">'
        f'{_esc(room["temp"])} · {room["used"]}/{room["cap"]} bins '
        f'({room["pct"]}% occupied)</div>'
    )
    racks = "".join(
        _rack_html(a, room["id"], selected_aisle, hl_aisle, hl_out_aisles)
        for a in room["aisles"]
    )
    store_zone = (
        '<div class="psch-zone-title">STORAGE — RACKING</div>'
        f'<div class="psch-rack-grid">{racks}</div>'
        '<div class="psch-zone-note">racks named AISLE-LEVEL-BAY · click a rack to view its bins</div>'
    )
    recv_zone = (
        '<div class="psch-zone-title">RECEIVING</div>'
        + _zone_rooms_html(room, "receiving", hl_cid, hl_out_cids)
    )
    disp_zone = (
        '<div class="psch-zone-title">DISPATCH</div>'
        + _zone_rooms_html(room, "dispatch", hl_cid, hl_out_cids)
    )
    flow = (
        '<div class="psch-flow">INBOUND ⟶ RECEIVING ⟶ PUTAWAY ⟶ STORAGE ⟶ PICK ⟶ DISPATCH ⟶ OUTBOUND</div>'
    )
    return (
        f'<div class="psch-room">{head}'
        '<table class="psch-room-body"><tr>'
        f'<td class="psch-zone psch-zone-recv">{recv_zone}</td>'
        f'<td class="psch-zone psch-zone-store">{store_zone}</td>'
        f'<td class="psch-zone psch-zone-disp">{disp_zone}</td>'
        "</tr></table>"
        f"{flow}</div>"
    )


def _lane_chip_html(lane: dict, out: bool) -> str:
    ids = lane.get("containers") or []
    out_ids = lane.get("outbound") or []
    shown = ids[:6]
    lines = ", ".join(f"<b>{_esc(c)}</b>" for c in shown)
    if len(ids) > 6:
        lines += f" +{len(ids) - 6} more"
    for c in out_ids:
        lines += f" {_esc(c)}<i>⬆</i>"
    count = len(ids) + len(out_ids)
    return (
        f'<div class="psch-lane-chip" title="Lane {_esc(lane["lane"])} · {count} container(s)">'
        f'<span class="psch-lane-id">{_esc(lane["lane"])}</span>'
        f'<span class="psch-lane-count">{count}</span>'
        f'<div class="psch-lane-cids">{lines or "—"}</div></div>'
    )


def lanes_html(psch: dict) -> str:
    """Receiving staging lanes at the inbound area.

    The releasing lanes are shown as physical blocks in their own section above
    the facility (see ``releasing_lanes_html``), so this keeps the inbound side
    only.
    """
    rcv = "".join(_lane_chip_html(l, out=False) for l in psch["lanes"]["receiving"])
    return (
        '<table class="psch-cols"><tr>'
        '<td class="psch-lanes-col">'
        '<div class="psch-lanes-head">INBOUND<br>RECEIVING LANES</div>' + rcv + "</td>"
        "</tr></table>"
    )


_REL_COLORS = ["rel-c1", "rel-c2", "rel-c3", "rel-c4", "rel-c5", "rel-c6"]


def releasing_lanes_html(psch: dict, selected_cid: str | None = None) -> str:
    """The physical releasing lanes (numbered 1..26) as narrow vertical blocks.

    Each block is one numbered lane, parked side by side in one row like dock
    doors along a warehouse wall. A consolidation container is allocated one
    lane or a contiguous group of adjacent lanes (``psch['releasing_groups']``);
    blocks are colour-coded per container and show how many pallets are staged
    there waiting to be loaded into that same container. The lanes of the
    selected container are highlighted with ``sel``.
    """
    groups = psch.get("releasing_groups", [])
    color_by_group = {
        g["container_id"]: _REL_COLORS[i % len(_REL_COLORS)]
        for i, g in enumerate(groups)
    }
    blocks = []
    for lane in psch["lanes"]["releasing"]:
        gid = lane.get("group")
        cls = "psch-rel-lane"
        if not gid:
            cls += " empty"
        else:
            cls += " " + color_by_group.get(gid, "rel-c1")
            if gid == selected_cid:
                cls += " sel"
        title = (
            f'{lane["lane"]} · {gid} · {lane["pallets"]} pallets staged'
            if gid
            else f'{lane["lane"]} · free'
        )
        if gid:
            inner = (
                f'<div class="psch-rel-id">{_esc(lane["lane"])}</div>'
                f'<div class="psch-rel-group">{_esc(gid)}</div>'
                f'<div class="psch-rel-pallets">{lane["pallets"]} pal</div>'
            )
        else:
            inner = (
                f'<div class="psch-rel-id">{_esc(lane["lane"])}</div>'
                '<div class="psch-rel-group">free</div>'
                '<div class="psch-rel-pallets">—</div>'
            )
        blocks.append(
            f'<div class="{cls}" data-rel="{_esc(lane["lane"])}" '
            f'data-group="{_esc(gid or "")}" title="{_esc(title)}">{inner}</div>'
        )
    return '<div class="psch-rel-grid">' + "".join(blocks) + "</div>"


def _facility_highlights(psch: dict, selected_cid: str | None,
                         selected_outbound_cid: str | None):
    """Highlight state shared by the facility builders.

    ``selected_cid`` (incoming) -> magenta highlight on that container's aisle;
    ``selected_outbound_cid`` -> yellow highlight on the aisles holding the
    pallets staged for that consolidation container (its source cargo can sit
    across several aisles) plus its source containers in the receiving zones.
    """
    hl_aisle = None
    hl_out_aisles: set[str] | None = None
    hl_out_cids: set[str] | None = None
    if selected_cid and selected_cid in psch.get("assigned", {}):
        hl_aisle = aisle_of_bin(psch["assigned"][selected_cid])
    if selected_outbound_cid:
        sources = next(
            (g.get("sources", []) for g in psch.get("releasing_groups", [])
             if g["container_id"] == selected_outbound_cid),
            [],
        )
        hl_out_cids = set(sources)
        aisles = {aisle_of_bin(psch["assigned"][c]) for c in sources if c in psch.get("assigned", {})}
        hl_out_aisles = aisles or None
    return hl_aisle, hl_out_aisles, hl_out_cids


def facility_html(psch: dict, selected_aisle: str | None = None,
                  selected_cid: str | None = None,
                  selected_outbound_cid: str | None = None) -> str:
    """The full facility visualisation: lanes flanking the two room blocks.

    ``selected_cid`` highlights one incoming container's aisle (magenta);
    ``selected_outbound_cid`` yellow-highlights the aisles holding the pallets
    staged for that consolidation container (its source cargo can sit across
    several aisles) and its source containers in the receiving zones.
    """
    hl_aisle, hl_out_aisles, hl_out_cids = _facility_highlights(
        psch, selected_cid, selected_outbound_cid
    )
    rooms = "".join(
        room_html(r, selected_aisle=selected_aisle, hl_aisle=hl_aisle, hl_cid=selected_cid,
                  hl_out_aisles=hl_out_aisles, hl_out_cids=hl_out_cids)
        for r in psch["rooms"]
    )
    rcv = "".join(_lane_chip_html(l, out=False) for l in psch["lanes"]["receiving"])
    return (
        '<div class="psch-facility"><table class="psch-cols psch-fac-cols"><tr>'
        '<td class="psch-lanes-col">'
        '<div class="psch-lanes-head">INBOUND<br>RECEIVING LANES</div>' + rcv + "</td>"
        f'<td class="psch-rooms-col">{rooms}</td>'
        "</tr></table></div>"
    )


def facility_collapsed_html(psch: dict) -> str:
    """Compact arrangement used when the facility view is collapsed.

    Keeps the two rooms + lane usage as one-line summaries so a narrow pane
    stays readable; the full facility (rooms + racks + lanes) is restored by
    switching back to ``facility_html``.
    """
    rooms = "".join(
        f'<div class="psch-collapsed-room"><b>{_esc(r["label"])}</b> · '
        f'{r["used"]}/{r["cap"]} bins ({r["pct"]}% occupied) · '
        f'{len(r["aisles"])} aisles</div>'
        for r in psch["rooms"]
    )
    s = psch["stats"]
    lanes = (
        f'Receiving lanes {s["lanes_rcv_used"]}/{s["lanes_rcv_total"]} in use · '
        f'Releasing lanes {s["lanes_rel_used"]}/4 in use · '
        f'{s["bins_used"]}/{s["bins_total"]} bins used ({s["bin_util"]}%)'
    )
    return (
        f'<div class="psch-collapsed">{rooms}'
        f'<div class="psch-collapsed-lanes">{lanes}</div></div>'
    )


def room_blocks_html(psch: dict, selected_aisle: str | None = None,
                     selected_cid: str | None = None,
                     selected_outbound_cid: str | None = None) -> dict[str, str]:
    """Per-room HTML blocks (keyed by room id) for the INBOUND/OUTBOUND layout.

    Same highlights as ``facility_html``, but each room comes back separately so
    a UI can render them as its own boxes (Ambient Room / Cold Room).
    """
    hl_aisle, hl_out_aisles, hl_out_cids = _facility_highlights(
        psch, selected_cid, selected_outbound_cid
    )
    return {
        r["id"]: '<div class="psch-facility">'
        + room_html(
            r,
            selected_aisle=selected_aisle,
            hl_aisle=hl_aisle,
            hl_cid=selected_cid,
            hl_out_aisles=hl_out_aisles,
            hl_out_cids=hl_out_cids,
        )
        + "</div>"
        for r in psch["rooms"]
    }


def bin_grid_html(psch: dict, aisle: str, selected_bin: str | None = None,
                  selected_cid: str | None = None) -> str:
    """The rack detail: a levels x bays(boxes) grid of bins for one aisle (rack).

    Rows are the rack levels (12 at the top down to 1 at floor level); columns
    are each bay's boxes (e.g. 1A 1B 1C 2A 2B 2C 3A 3B 3C), so every cell is
    one AISLE-LEVEL-BAY bin.
    """
    room = room_of_aisle(aisle)
    cfg = ROOMS[room]
    bins = psch.get("bins", {})
    levels, bays, boxes = cfg["levels"], cfg["bays"], cfg["boxes"]

    # Two header rows: bay group spanning its three box columns, then A/B/C.
    head = (
        '<tr><th class="psch-b-box" rowspan="2">Level</th>'
        + "".join(
            f'<th class="psch-b-bay" colspan="{len(boxes)}">Bay {b}</th>'
            for b in range(1, bays + 1)
        )
        + "</tr><tr>"
        + "".join(f"<th>{box}</th>" for _ in range(bays) for box in boxes)
        + "</tr>"
    )
    rows = []
    for lvl in range(levels, 0, -1):  # top level first, like a rack elevation
        cells = [f'<td class="psch-b-box">{lvl}</td>']
        for bay in range(1, bays + 1):
            for box in boxes:
                bid = f"{aisle}-{lvl:02d}-{bay}{box}"
                b = bins.get(bid)
                cell_cls = "psch-bin-empty"
                title = f"{bid} · empty"
                inner = f'<span class="psch-bin-id">{bay}{box}</span><br>—'
                if b:
                    cell_cls = "psch-bin-occupied" if b.get("arrived") else "psch-bin-reserved"
                    if bid == selected_bin:
                        cell_cls += " sel"
                    elif b.get("container_id") == selected_cid:
                        cell_cls += " hl"
                    title = (
                        f"{b['container_id']} · {b.get('status')} · {b.get('pallets', 0)} pallets · "
                        f"receipt {b.get('receipt_eta') or '—'} · pick {b.get('pallet_pick_time') or '—'}"
                    )
                    inner = (
                        f'<span class="psch-bin-id">{bay}{box}</span><br>'
                        f'<span class="psch-bin-cid">{_esc(b["container_id"])}</span><br>'
                        f'<span class="psch-bin-meta">{_status_short(b.get("status"))}</span>'
                    )
                cells.append(f'<td class="{cell_cls}" title="{_esc(title)}">{inner}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div class="psch-grid-wrap"><table class="psch-bin-grid">'
        f"{head}{''.join(rows)}</table></div>"
    )


def rack_summary_html(psch: dict, aisle: str) -> str:
    """Header block for a selected rack (used above the bin grid)."""
    room = room_of_aisle(aisle)
    cfg = ROOMS[room]
    for r in psch["rooms"]:
        if r["id"] == room:
            for a in r["aisles"]:
                if a["id"] == aisle:
                    bar_w = max(2, int(a["pct"])) if a["pct"] else 0
                    boxes = "".join(cfg["boxes"])
                    return (
                        f'<div style="margin-bottom:4px"><b>Aisle {_esc(aisle)}</b> '
                        f'({r["label"]} · {_esc(r["temp"])}) — '
                        f'{a["used"]}/{a["cap"]} bins · {a["pct"]}% occupied · '
                        f'<span style="color:#404040">levels 1–{cfg["levels"]} × bays 1–{cfg["bays"]} '
                        f'(boxes {boxes})</span>'
                        f'<div class="psch-occ-bar"><i style="width:{bar_w}%"></i></div>'
                        f'<div style="font-size:8px;color:#404040;margin-top:2px">slotting: '
                        f'cargo released soon sits at floor level, slow movers higher</div></div>'
                    )
    return f"<b>Aisle {_esc(aisle)}</b>"
