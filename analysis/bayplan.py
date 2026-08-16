"""Industry-style vessel Bay Plan renderer.

Draws the bay plan of a carrying vessel exactly as a terminal would: columns
are stacks (even rows on the port side, odd on the starboard, counted outward
from the centreline), rows are tiers (02-18 below deck, 82-92 above deck with a
hatch line between), and every occupied cell shows the size code, container
number and weight, colour-coded by destination. The container being tracked is
highlighted with a red border and (in the classic UI) is clickable.

The plan is drawn as the ship's cross-section: above the hatch the full breadth
of the vessel is available, while the hold below tapers toward the keel like a
cone — the lower the tier, the fewer outboard stacks exist inside the hull, so
the plan narrows to the centreline. The highlighted container's cell is never
hidden by the hull outline, so its Bay-Row-Tier coordinates are always visible
in the caption and on the diagram itself.

See PORT_PROCESS_FLOW.md §1.3 and the AMT Mercator sample bay plan for the
domain conventions this follows.
"""
from __future__ import annotations

# Stack (row) display order: even side 16..02 then odd side 01..15.
STACKS_DISPLAY = list(range(16, 0, -2)) + list(range(1, 17, 2))
# Tier rows, top to bottom: above deck 92..82, hatch, below deck 18..02.
TIERS_ABOVE = list(range(92, 81, -2))
TIERS_BELOW = list(range(18, 1, -2))

# Hull cross-section: below the deck the hull narrows toward the keel, so each
# lower tier carries fewer stacks (the outboard rows drop out first). This is
# what gives the bay plan its cone / hull shape. Above deck the full 16-stack
# breadth is always available.
HULL_STACKS = {
    18: 16, 16: 14, 14: 12, 12: 10, 10: 8,
    8: 6, 6: 4, 4: 2, 2: 2,
}

# Cell fill colour by discharge port (the reference preliminary stowage
# plan's legend: Singapore, Colombo, Piraeus, Rotterdam, Hamburg, Antwerp).
DEST_COLORS = {
    "Singapore": "#F08080",  # light red / salmon
    "Colombo": "#C71585",    # magenta
    "Piraeus": "#6B8E8E",    # muted blue-green / slate
    "Rotterdam": "#87CEEB",  # light blue
    "Hamburg": "#4CAF50",    # bright green
    "Antwerp": "#F2C230",    # yellow
}
FALLBACK_COLOR = "#9AA0A6"


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tier_stacks(tier: int, force: int | None = None) -> list[int]:
    """Stacks drawn for a tier, following the hull cross-section.

    Above deck the full breadth exists; below deck the hull tapers to fewer
    stacks on the lower tiers. `force` (the tracked cell's stack) is always
    kept so the highlighted container is never hidden behind the hull outline.
    """
    if tier >= 82:
        keep = set(STACKS_DISPLAY)
    else:
        width = HULL_STACKS.get(tier, 2)
        trim = (len(STACKS_DISPLAY) - width) // 2
        keep = set(STACKS_DISPLAY[trim:len(STACKS_DISPLAY) - trim])
    if force is not None:
        keep.add(force)
    return [s for s in STACKS_DISPLAY if s in keep]


def build_bay_plan(
    entry: dict, clickable: bool = False, target: tuple[int, int] | None = None
) -> str:
    """Render a bay plan grid for one container's vessel stowage cell.

    `entry` needs: container_id (the highlighted cell), bay_label, and
    bay_cells (list of {stack, tier, container_id, size, destination,
    weight_t, is_mcc}). `target` optionally marks a (stack, tier) cell as the
    loading target (an outbound container about to be loaded there), drawn
    with a dashed outline. Returns an HTML fragment.
    """
    selected = entry.get("container_id")
    by_key = {(c["stack"], c["tier"]): c for c in entry.get("bay_cells") or []}
    bay_label = _esc(entry.get("bay_label") or entry.get("stow_bay") or "")

    # The tracked cell: the selected container, or the loading target.
    sel_cell = next(
        (c for c in (entry.get("bay_cells") or []) if c.get("container_id") == selected),
        None,
    )
    sel_stack = sel_cell["stack"] if sel_cell else None
    sel_tier = sel_cell["tier"] if sel_cell else None
    tar_stack, tar_tier = target if target else (None, None)

    def cell(stack: int, tier: int, extra_cls: str = "") -> str:
        is_target = target is not None and (stack, tier) == target
        c = by_key.get((stack, tier))
        if c is None:
            if is_target:
                return '<td class="bp-cell bp-target' + extra_cls + '" title="Loading target">'\
                       '<span class="bp-target-tag">LOAD</span></td>'
            # Uncovered slots are drawn as white gaps: a stack is always filled
            # from the bottom of its band up, so the top tiers are empty where
            # boxes were already discharged at earlier ports.
            return f'<td class="bp-cell bp-empty{extra_cls}"></td>'
        is_sel = c["container_id"] == selected
        color = DEST_COLORS.get(c.get("destination"), FALLBACK_COLOR)
        onclick = ""
        cursor = ""
        if clickable and c.get("is_mcc"):
            onclick = f" onclick=\\\"selectIncoming('{c['container_id']}')\\\""
            cursor = ' style="cursor:pointer"'
        title = (
            f"{c['container_id']} · {c['size']} · {c['weight_t']}t · "
            f"{c.get('destination')} · Row {c['stack']} Tier {c['tier']}"
        )
        if is_target:
            title = f"LOADING TARGET — {title}"
        # Specials (RF/DG/OOG) are marked on the size line, as terminals do.
        ctype = c.get("type") or c.get("cargo_type") or ""
        size_line = _esc(c["size"][:2])
        if ctype in ("RF", "DG", "OOG"):
            size_line += " " + _esc(ctype)
        tag = '<span class="bp-target-tag">LOAD</span>' if is_target else ""
        return (
            f'<td class="bp-cell{" bp-sel" if is_sel else ""}{" bp-target" if is_target else ""}{extra_cls}"'
            f' style="background:{color}"'
            f"{cursor}{onclick} title=\"{title}\">"
            f'<div class="bp-size">{size_line}</div>'
            f'<div class="bp-id">{_esc(c["container_id"])}</div>'
            f'<div class="bp-w">{c["weight_t"]} {tag}</div></td>'
        )

    def tier_row(label: int) -> str:
        # Never hide the cell being tracked: force it into the tier's outline.
        force = None
        if sel_tier == label:
            force = sel_stack
        if tar_tier == label:
            force = tar_stack
        stacks = _tier_stacks(label, force)
        n = len(STACKS_DISPLAY)
        # Centre the hull: pad the row with void cells so each stack lines up
        # under its own column header, leaving the taper as a symmetric cone.
        left = STACKS_DISPLAY.index(stacks[0])
        right = n - left - len(stacks)
        pads_l = f'<td class="bp-void" colspan="{left}"></td>' if left else ""
        pads_r = f'<td class="bp-void" colspan="{right}"></td>' if right else ""
        cells = ""
        for i, stack in enumerate(stacks):
            extra = ""
            if label < 82:  # hold: draw the hull wall on the outboard edges
                if i == 0:
                    extra += " bp-wall-l"
                if i == len(stacks) - 1:
                    extra += " bp-wall-r"
            cells += cell(stack, label, extra)
        return (
            f'<tr><td class="bp-tier">{label:02d}</td>{pads_l}{cells}{pads_r}'
            f'<td class="bp-tier bp-tier-r">{label:02d}</td></tr>'
        )

    rows = "".join(tier_row(t) for t in TIERS_ABOVE)
    rows += (
        '<tr><td class="bp-deck" colspan="17">'
        '<span>HATCH — deck line</span></td></tr>'
    )
    rows += "".join(tier_row(t) for t in TIERS_BELOW)

    stack_header = "".join(
        f'<td class="bp-stack">{s:02d}</td>' for s in STACKS_DISPLAY
    )
    legend = "".join(
        f'<span class="bp-legend"><i style="background:{DEST_COLORS[d]}"></i>{d}</span>'
        for d in sorted({c.get("destination") for c in (entry.get("bay_cells") or []) if c.get("destination")})
    )
    colors = '<span class="bp-legend"><i style="background:#FFFFFF"></i>empty (discharged)</span>'
    note = (
        '<div style="font-size:8px;color:#606060;margin-top:2px">'
        'Preliminary stowage plan · each bay carries its own port mix — deck '
        'band and hold band · white above the hatch = deck boxes already '
        'discharged at earlier ports · holds stay full (later-port cargo) · '
        'heavy low · RF = reefer at power stacks · DG = dangerous, segregated · '
        'OOG topmost · hull narrows to the keel below the hatch line</div>'
    )

    # Caption carries the diagram's coordinates in the correct terms
    # (Bay · Row · Tier), as a terminal would quote a cell.
    caption = f"Bay Plan — {bay_label}"
    if sel_cell:
        caption = (
            f"Bay Plan — Bay {bay_label} · Row {sel_cell['stack']:02d} · "
            f"Tier {sel_cell['tier']:02d} · {_esc(sel_cell['container_id'])}"
        )
    elif target:
        caption = (
            f"Bay Plan — Bay {bay_label} · Row {tar_stack:02d} · "
            f"Tier {tar_tier:02d} · loading target"
        )

    return (
        '<div class="bayplan" style="overflow-x:auto">'
        f'<table class="bp-table">'
        f'<caption>{caption}</caption>'
        f'<tr><td class="bp-tier"></td>{stack_header}<td class="bp-tier"></td></tr>'
        f'{rows}'
        f'</table>'
        f'<div class="bp-legend-wrap">{legend}{colors}</div>'
        f"{note}"
        "</div>"
    )
