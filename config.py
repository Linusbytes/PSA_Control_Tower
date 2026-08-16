"""Central configuration for the PSA Port-PSCH MCC coordination prototype."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

try:  # .env support is optional at import time
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv not installed
    pass

ROOT = Path(__file__).resolve().parent

# --- Simulation clock -----------------------------------------------------------
# Synthetic data is anchored to this instant instead of the real wall clock, so
# the scenario is reproducible and the agent derives every ETA against the same
# reference point.
SIM_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

# --- Data -----------------------------------------------------------------------
DB_PATH = ROOT / "data" / "port.db"
SEED = int(os.getenv("SIM_SEED", "42"))
N_CONTAINERS = int(os.getenv("N_CONTAINERS", "60"))
DRAYAGE_TOTAL = int(os.getenv("DRAYAGE_TOTAL", "8"))

# --- MCC journey planning constants (minutes) -----------------------------------
# The agent derives the full port-to-PSCH timeline from a vessel's ETA using
# these fixed stage durations (see PORT_PROCESS_FLOW.md for the domain model).
UNLOAD_MIN = int(os.getenv("UNLOAD_MIN", "180"))          # quay discharge of one MCC container
YARD_TRANSFER_MIN = int(os.getenv("YARD_TRANSFER_MIN", "45"))   # quay -> depot yard
DEPOT_DWELL_MIN = int(os.getenv("DEPOT_DWELL_MIN", "120"))      # wait before road dispatch
ROAD_TRANSIT_MIN = int(os.getenv("ROAD_TRANSIT_MIN", "45"))     # port depot -> PSCH doorstep

# PSCH in-house processing (once the container is at the doorstep).
STAGING_MIN = int(os.getenv("STAGING_MIN", "20"))          # wait at the receiving staging area
MOVE_TO_BIN_MIN = int(os.getenv("MOVE_TO_BIN_MIN", "15"))   # receiving area -> bin (robot move)

# --- PSCH receiving capacity -----------------------------------------------------
# Receiving areas (RA-n, the doors) are opened based on the inbound arrival rate
# (containers/hr); the receiving lanes map 1:1 to them and are numbered plainly
# 1..10 (same convention as the releasing lanes).
RECEIVING_LANES_TOTAL = int(os.getenv("PSCH_RECEIVING_LANES", "10"))
RECEIVING_AREAS = [f"RA-{i}" for i in range(1, RECEIVING_LANES_TOTAL + 1)]
PUTAWAY_ROBOTS = 8

# --- PSCH facility layout (distribution-centre conventions) -----------------------
# PSCH is a two-room container freight station: AMBIENT (dry MCC cargo) and
# COLD ROOM (reefer cargo). Racks and bins are named the conventional
# distribution-centre way: AISLE-LEVEL-BAY, e.g. "1-12-2A" = Aisle 1, Level
# 12, Bay 2A -- the box letter (A/B/C) is written directly on the bay number.
# Aisles are numbered 1..24 across the facility: ambient 1-21, cold room 22-24
# (the last ambient aisle, 21, is segregated for dangerous goods). Every aisle
# has a fixed number of levels (12, the height in the rack) and every level has
# a fixed number of bays (3), each bay holding boxes A/B/C.
PSCH_AISLES_TOTAL = int(os.getenv("PSCH_AISLES_TOTAL", "24"))
PSCH_COLD_ROOM_AISLES = int(os.getenv("PSCH_COLD_ROOM_AISLES", "3"))
PSCH_AMBIENT_AISLES = [
    str(i) for i in range(1, PSCH_AISLES_TOTAL - PSCH_COLD_ROOM_AISLES + 1)
]
PSCH_COLD_AISLES = [
    str(i)
    for i in range(
        PSCH_AISLES_TOTAL - PSCH_COLD_ROOM_AISLES + 1, PSCH_AISLES_TOTAL + 1
    )
]
PSCH_LEVELS_PER_AISLE = int(os.getenv("PSCH_LEVELS_PER_AISLE", "12"))
PSCH_BAYS_PER_LEVEL = int(os.getenv("PSCH_BAYS_PER_LEVEL", "3"))
PSCH_BOXES = ["A", "B", "C"]
PSCH_HAZMAT_AISLE = PSCH_AMBIENT_AISLES[-1]

# Staging lanes at the inbound (receiving) and outbound (releasing) areas of PSCH.
RECEIVING_LANES = [str(i) for i in range(1, RECEIVING_LANES_TOTAL + 1)]

# Physical releasing lanes at the PSCH dispatch area: 26 lanes drawn as narrow
# vertical blocks parked side by side (like dock doors along a warehouse wall),
# numbered plainly 1..26. An MCC consolidation container is allocated one lane,
# or a contiguous group of adjacent lanes, to stage the multiple pallets waiting
# to be loaded into that same container; the agent plans which lane(s) every
# group uses. PALLETS_PER_LANE sets how many staged pallets a lane holds.
RELEASING_LANES_TOTAL = int(os.getenv("PSCH_RELEASING_LANES", "26"))
RELEASING_LANES = [str(i) for i in range(1, RELEASING_LANES_TOTAL + 1)]
PALLETS_PER_LANE = int(os.getenv("PSCH_PALLETS_PER_LANE", "8"))
