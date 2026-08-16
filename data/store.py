"""SQLite data layer shared by the simulator, the MCC planner, and the dashboard.

Chosen over in-memory JSON so the planner process, the HTTP server, and the
Streamlit process all observe the same state (no infra to run).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH
from models.schemas import (
    CargoFlag,
    MccPlan,
    OutboundContainer,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    status TEXT NOT NULL,
    discharge_timestamp TEXT,
    yard_location TEXT,
    size_type TEXT NOT NULL,
    cargo_flag TEXT NOT NULL,
    customs_status TEXT NOT NULL,
    consignee_id TEXT,
    special_handling TEXT NOT NULL DEFAULT '[]',
    vessel_cutoff TEXT,
    stow_position TEXT,
    stow_bay INTEGER,
    stow_row INTEGER,
    stow_tier INTEGER
);
CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    linked_container_id TEXT NOT NULL REFERENCES containers(container_id),
    service_type TEXT NOT NULL,
    shipper_ids TEXT NOT NULL DEFAULT '[]',
    required_by TEXT NOT NULL,
    storage_zone TEXT NOT NULL,
    dock_slot_status TEXT NOT NULL,
    processing_queue_position INTEGER NOT NULL DEFAULT 0,
    destination TEXT
);
CREATE TABLE IF NOT EXISTS yard_status (
    block TEXT PRIMARY KEY,
    zone TEXT NOT NULL,
    utilization_pct REAL NOT NULL,
    gate_lanes_available INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vessels (
    voyage_id TEXT PRIMARY KEY,
    vessel_name TEXT NOT NULL,
    status TEXT NOT NULL,
    berth_id TEXT,
    eta TEXT,
    etd TEXT,
    moves_planned INTEGER,
    destination TEXT,
    distance_nm REAL,
    speed_knots REAL
);
CREATE TABLE IF NOT EXISTS drayage (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_trucks INTEGER NOT NULL,
    available_trucks INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sla_profiles (
    consignee_id TEXT PRIMARY KEY,
    priority_tier INTEGER NOT NULL,
    sla_hours INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    shipper_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    cargo_type TEXT NOT NULL,
    volume_cbm REAL NOT NULL,
    ready_time TEXT NOT NULL,
    service_type TEXT NOT NULL,
    consignee_id TEXT,
    source_container_id TEXT,
    outbound_container_id TEXT
);
CREATE TABLE IF NOT EXISTS mcc_plans (
    container_id TEXT PRIMARY KEY,
    carrying_vessel_id TEXT NOT NULL,
    carrying_vessel_name TEXT NOT NULL,
    vessel_destination TEXT,
    vessel_distance_nm REAL,
    vessel_speed_knots REAL,
    stow_position TEXT,
    sea_arrival TEXT NOT NULL,
    unload_end TEXT NOT NULL,
    depot_arrive TEXT NOT NULL,
    road_depart TEXT NOT NULL,
    psch_receipt_eta TEXT NOT NULL,
    receiving_area TEXT NOT NULL,
    staging_start TEXT NOT NULL,
    staging_end TEXT NOT NULL,
    move_start TEXT NOT NULL,
    move_end TEXT NOT NULL,
    bin_location TEXT NOT NULL,
    putaway_robot TEXT NOT NULL,
    pallet_pick_time TEXT NOT NULL,
    release_lane TEXT NOT NULL,
    consolidation_group TEXT,
    reasoning TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outbound_containers (
    container_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    size_type TEXT NOT NULL DEFAULT '40HC',
    source_container_ids TEXT NOT NULL DEFAULT '[]',
    source_shipment_ids TEXT NOT NULL DEFAULT '[]',
    bound_vessel_id TEXT NOT NULL,
    bound_vessel_name TEXT NOT NULL,
    vessel_etd TEXT,
    stow_position TEXT,
    stow_bay INTEGER,
    stow_row INTEGER,
    stow_tier INTEGER,
    stuffing_start TEXT NOT NULL,
    stuffing_end TEXT NOT NULL,
    lane_release_time TEXT NOT NULL,
    loading_lane TEXT NOT NULL,
    road_depart TEXT NOT NULL,
    eta_loading_area TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged',
    staging_lane_start INTEGER,
    staging_lane_end INTEGER,
    reasoning TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vessel_stowage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vessel_id TEXT NOT NULL,
    bay INTEGER NOT NULL,
    bay_label TEXT NOT NULL,
    stack INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    container_id TEXT NOT NULL,
    size TEXT NOT NULL,
    destination TEXT NOT NULL,
    weight_t REAL NOT NULL,
    is_mcc INTEGER NOT NULL DEFAULT 0,
    cargo_type TEXT NOT NULL DEFAULT 'GP'
);
CREATE TABLE IF NOT EXISTS trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@contextmanager
def connect(path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path | str = DB_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def load_scenario(scenario: Any, path: Path | str = DB_PATH) -> None:
    init_db(path)
    with connect(path) as conn:
        for table in (
            "mcc_plans",
            "outbound_containers",
            "vessel_stowage",
            "trace",
            "shipments",
            "bookings",
            "containers",
            "yard_status",
            "drayage",
            "sla_profiles",
            "vessels",
        ):
            conn.execute(f"DELETE FROM {table}")

        for c in scenario.containers:
            conn.execute(
                """INSERT INTO containers
                   (container_id, voyage_id, status, discharge_timestamp, yard_location,
                    size_type, cargo_flag, customs_status, consignee_id, special_handling,
                    vessel_cutoff, stow_position, stow_bay, stow_row, stow_tier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c.container_id,
                    c.voyage_id,
                    c.status,
                    _iso(c.discharge_timestamp),
                    c.yard_location,
                    c.size_type,
                    c.cargo_flag.value,
                    c.customs_status.value,
                    c.consignee_id,
                    json.dumps(c.special_handling),
                    _iso(c.vessel_cutoff),
                    c.stow_position,
                    c.stow_bay,
                    c.stow_row,
                    c.stow_tier,
                ),
            )

        for b in scenario.bookings:
            conn.execute(
                """INSERT INTO bookings
                   (booking_id, linked_container_id, service_type, shipper_ids, required_by,
                    storage_zone, dock_slot_status, processing_queue_position, destination)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    b.booking_id,
                    b.linked_container_id,
                    b.service_type.value,
                    json.dumps(b.shipper_ids),
                    _iso(b.required_by),
                    b.storage_zone.value,
                    b.dock_slot_status.value,
                    b.processing_queue_position,
                    b.destination,
                ),
            )

        for y in scenario.yard:
            conn.execute(
                """INSERT INTO yard_status (block, zone, utilization_pct, gate_lanes_available)
                   VALUES (?, ?, ?, ?)""",
                (y.block, y.zone, y.utilization_pct, y.gate_lanes_available),
            )

        if scenario.drayage is not None:
            conn.execute(
                "INSERT INTO drayage (id, total_trucks, available_trucks) VALUES (1, ?, ?)",
                (scenario.drayage.total_trucks, scenario.drayage.available_trucks),
            )

        for s in scenario.slas:
            conn.execute(
                "INSERT INTO sla_profiles (consignee_id, priority_tier, sla_hours) VALUES (?, ?, ?)",
                (s.consignee_id, s.priority_tier, s.sla_hours),
            )

        for s in scenario.shipments:
            conn.execute(
                """INSERT INTO shipments
                   (shipment_id, shipper_id, destination, cargo_type, volume_cbm,
                    ready_time, service_type, consignee_id, source_container_id,
                    outbound_container_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    s.shipment_id,
                    s.shipper_id,
                    s.destination,
                    s.cargo_type.value,
                    s.volume_cbm,
                    _iso(s.ready_time),
                    s.service_type.value,
                    s.consignee_id,
                    s.source_container_id,
                    s.outbound_container_id,
                ),
            )

        for v in scenario.vessels:
            conn.execute(
                """INSERT INTO vessels
                   (voyage_id, vessel_name, status, berth_id, eta, etd, moves_planned,
                    destination, distance_nm, speed_knots)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    v.voyage_id,
                    v.vessel_name,
                    v.status.value,
                    v.berth_id,
                    _iso(v.eta),
                    _iso(v.etd),
                    v.moves_planned,
                    v.destination,
                    v.distance_nm,
                    v.speed_knots,
                ),
            )

        for cell in scenario.stowage:
            conn.execute(
                """INSERT INTO vessel_stowage
                   (vessel_id, bay, bay_label, stack, tier, container_id, size,
                    destination, weight_t, is_mcc, cargo_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cell.vessel_id,
                    cell.bay,
                    cell.bay_label,
                    cell.stack,
                    cell.tier,
                    cell.container_id,
                    cell.size,
                    cell.destination,
                    cell.weight_t,
                    int(cell.is_mcc),
                    cell.cargo_type,
                ),
            )


def has_data(path: Path | str = DB_PATH) -> bool:
    if not Path(path).exists():
        return False
    try:
        with connect(path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM containers").fetchone()
        return row["n"] > 0
    except sqlite3.Error:
        return False


def get_containers(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM containers").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["discharge_timestamp"] = _dt(r["discharge_timestamp"])
        d["vessel_cutoff"] = _dt(r["vessel_cutoff"])
        d["special_handling"] = json.loads(r["special_handling"])
        out.append(d)
    return out


def get_bookings(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM bookings").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["required_by"] = _dt(r["required_by"])
        d["shipper_ids"] = json.loads(r["shipper_ids"])
        out.append(d)
    return out


def get_yard_status(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM yard_status ORDER BY block").fetchall()
    return [dict(r) for r in rows]


def get_drayage(path: Path | str = DB_PATH) -> dict:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT total_trucks, available_trucks FROM drayage WHERE id = 1"
        ).fetchone()
    if row is None:
        return {"total_trucks": 0, "available_trucks": 0}
    d = dict(row)
    d["utilization_pct"] = (
        round(100 * (d["total_trucks"] - d["available_trucks"]) / d["total_trucks"], 1)
        if d["total_trucks"] > 0
        else 0.0
    )
    return d


def get_slas(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM sla_profiles ORDER BY priority_tier").fetchall()
    return [dict(r) for r in rows]


def get_vessels(path: Path | str = DB_PATH) -> list[dict]:
    """Return every vessel and its berth assignment / tracking state."""
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM vessels").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["eta"] = _dt(d["eta"])
        d["etd"] = _dt(d["etd"])
        out.append(d)
    return out


def get_shipments(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM shipments").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ready_time"] = _dt(r["ready_time"])
        out.append(d)
    return out


def get_vessel_stowage(path: Path | str = DB_PATH) -> list[dict]:
    """Every occupied cell of every vessel's bay plan (industry Bay-Row-Tier)."""
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM vessel_stowage ORDER BY vessel_id, bay, tier DESC, stack"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_mcc"] = bool(r["is_mcc"])
        out.append(d)
    return out


# --- MCC planner tables ---------------------------------------------------------


def save_mcc_plans(plans: list[MccPlan], path: Path | str = DB_PATH) -> None:
    """Replace the current MCC plan batch with a fresh one from the agent."""
    with connect(path) as conn:
        conn.execute("DELETE FROM mcc_plans")
        for p in plans:
            conn.execute(
                """INSERT INTO mcc_plans
                   (container_id, carrying_vessel_id, carrying_vessel_name,
                    vessel_destination, vessel_distance_nm, vessel_speed_knots,
                    stow_position, sea_arrival, unload_end, depot_arrive,
                    road_depart, psch_receipt_eta, receiving_area, staging_start,
                    staging_end, move_start, move_end, bin_location, putaway_robot,
                    pallet_pick_time, release_lane, consolidation_group, reasoning)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.container_id,
                    p.carrying_vessel_id,
                    p.carrying_vessel_name,
                    p.vessel_destination,
                    p.vessel_distance_nm,
                    p.vessel_speed_knots,
                    p.stow_position,
                    _iso(p.sea_arrival),
                    _iso(p.unload_end),
                    _iso(p.depot_arrive),
                    _iso(p.road_depart),
                    _iso(p.psch_receipt_eta),
                    p.receiving_area,
                    _iso(p.staging_start),
                    _iso(p.staging_end),
                    _iso(p.move_start),
                    _iso(p.move_end),
                    p.bin_location,
                    p.putaway_robot,
                    _iso(p.pallet_pick_time),
                    p.release_lane,
                    p.consolidation_group,
                    p.reasoning,
                ),
            )


def get_mcc_plans(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM mcc_plans").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for col in (
            "sea_arrival", "unload_end", "depot_arrive", "road_depart",
            "psch_receipt_eta", "staging_start", "staging_end", "move_start",
            "move_end", "pallet_pick_time",
        ):
            d[col] = _dt(d[col])
        out.append(d)
    return out


def save_outbound_containers(
    outbound: list[OutboundContainer], path: Path | str = DB_PATH
) -> None:
    """Replace the current outbound container plan batch."""
    with connect(path) as conn:
        conn.execute("DELETE FROM outbound_containers")
        for o in outbound:
            conn.execute(
                """INSERT INTO outbound_containers
                   (container_id, destination, size_type, source_container_ids,
                    source_shipment_ids, bound_vessel_id, bound_vessel_name,
                    vessel_etd, stow_position, stow_bay, stow_row, stow_tier,
                    stuffing_start, stuffing_end, lane_release_time, loading_lane,
                    road_depart, eta_loading_area, status, staging_lane_start,
                    staging_lane_end, reasoning)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    o.container_id,
                    o.destination,
                    o.size_type,
                    json.dumps(o.source_container_ids),
                    json.dumps(o.source_shipment_ids),
                    o.bound_vessel_id,
                    o.bound_vessel_name,
                    _iso(o.vessel_etd),
                    o.stow_position,
                    o.stow_bay,
                    o.stow_row,
                    o.stow_tier,
                    _iso(o.stuffing_start),
                    _iso(o.stuffing_end),
                    _iso(o.lane_release_time),
                    o.loading_lane,
                    _iso(o.road_depart),
                    _iso(o.eta_loading_area),
                    o.status.value,
                    o.staging_lane_start,
                    o.staging_lane_end,
                    o.reasoning,
                ),
            )


def get_outbound_containers(path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute("SELECT * FROM outbound_containers").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for col in (
            "vessel_etd", "stuffing_start", "stuffing_end", "lane_release_time",
            "road_depart", "eta_loading_area",
        ):
            d[col] = _dt(d[col])
        d["source_container_ids"] = json.loads(r["source_container_ids"])
        d["source_shipment_ids"] = json.loads(r["source_shipment_ids"])
        out.append(d)
    return out


# --- Execution trace ------------------------------------------------------------


def record_event(
    actor: str, event: str, detail: dict | None = None, path: Path | str = DB_PATH
) -> None:
    """Append one entry to the execution trace (brief requirement #6)."""
    init_db(path)
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO trace (ts, actor, event, detail) VALUES (?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor,
                event,
                json.dumps(detail or {}, default=str),
            ),
        )


def get_trace(limit: int = 200, path: Path | str = DB_PATH) -> list[dict]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM trace ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ts"] = _dt(r["ts"])
        d["detail"] = json.loads(r["detail"])
        out.append(d)
    return out


def clear_trace(path: Path | str = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM trace")
