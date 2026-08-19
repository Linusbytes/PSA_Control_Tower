"""Central configuration for the PSA Port-PSCH MCC coordination prototype."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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
# reference point. The world then runs as a living ecosystem: sim_now() advances
# from SIM_NOW at SIM_SPEED sim-seconds per real second (default 60x, so one
# sim hour passes every real minute and a full wave lifecycle takes a couple of
# real hours). Set SIM_SPEED=0 to freeze the clock (deterministic tests).
SIM_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
SIM_SPEED = int(os.getenv("SIM_SPEED", "60"))
_SIM_BOOT_UTC = datetime.now(timezone.utc)


def sim_now() -> datetime:
    """The live simulation clock, advancing with wall-clock time.

    Every status and metric that should "move" in real life (journey stages,
    stacker charges, bin occupancy, outbound loading, deliveries) is derived
    from this clock, so the UI's 8-second state poll shows the world evolving
    by itself — no button presses, no "enable live mode".
    """
    if SIM_SPEED <= 0:
        return SIM_NOW
    elapsed = (datetime.now(timezone.utc) - _SIM_BOOT_UTC).total_seconds()
    return SIM_NOW + timedelta(seconds=elapsed * SIM_SPEED)

# --- Data -----------------------------------------------------------------------
DB_PATH = ROOT / "data" / "port.db"
SEED = int(os.getenv("SIM_SEED", "42"))
# Larger synthetic population so the hub flows (MCC / LCL / FCL / Top Up /
# Transload) each have a real container pool and the racks reach realistic
# occupancy; 24/7 operation means arrivals are spread continuously across the
# whole day, never shift-banded. The facility also carries a deterministic
# dwell-stock floor from "previous waves" (see data/facility.py) so every
# aisle sits in a realistic 30-70% utilisation band on top of this wave.
N_CONTAINERS = int(os.getenv("N_CONTAINERS", "400"))
DRAYAGE_TOTAL = int(os.getenv("DRAYAGE_TOTAL", "12"))

# --- Agentic AI API (the seam) --------------------------------------------------
# The agent brain is swappable: the deterministic rule-based planner runs by
# default (zero API, reproducible demos). Set AGENTIC_API_ENDPOINT to switch the
# runtime to the external agentic AI API -- the same tool registry drives both.
# AGENTIC_API_ENDPOINT is any OpenAI-compatible agentic endpoint (e.g. the
# chat/completions URL); AGENTIC_API_KEY / AGENTIC_API_MODEL are optional.
#
# AGENTIC_AUTONOMY gates what the agent may do without a human in the loop:
#   advisory        -- agent proposes, nothing executes without review (default)
#   semi_autonomous -- low-risk (mutate) actions auto-applied, execution-level
#                      (approval) actions still need a human
#   autonomous      -- everything auto-applied (demo mode only)
AGENTIC_API_ENDPOINT = os.getenv("AGENTIC_API_ENDPOINT", "")
AGENTIC_API_KEY = os.getenv("AGENTIC_API_KEY", "")
AGENTIC_API_MODEL = os.getenv("AGENTIC_API_MODEL", "")
AGENTIC_AUTONOMY = os.getenv("AGENTIC_AUTONOMY", "advisory").strip().lower()
AGENTIC_MAX_TOOL_ITERATIONS = int(os.getenv("AGENTIC_MAX_TOOL_ITERATIONS", "20"))

# --- MCC journey planning constants (minutes) -----------------------------------
# The agent derives the full port-to-PSCH timeline from a vessel's ETA using
# these fixed stage durations (see PORT_PROCESS_FLOW.md for the domain model).
UNLOAD_MIN = int(os.getenv("UNLOAD_MIN", "180"))          # quay discharge of one MCC container
YARD_TRANSFER_MIN = int(os.getenv("YARD_TRANSFER_MIN", "45"))   # quay -> depot yard
DEPOT_DWELL_MIN = int(os.getenv("DEPOT_DWELL_MIN", "120"))      # wait before road dispatch
ROAD_TRANSIT_MIN = int(os.getenv("ROAD_TRANSIT_MIN", "45"))     # port depot -> PSCH doorstep

# PSCH in-house processing (once the container is at the doorstep).
STAGING_MIN = int(os.getenv("STAGING_MIN", "20"))          # wait at the receiving staging area
MOVE_TO_BIN_MIN = int(os.getenv("MOVE_TO_BIN_MIN", "15"))   # receiving area -> bin (AS/RS stacker move)

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

# Physical releasing lanes at the PSCH dispatch area: 40 lanes drawn as narrow
# vertical blocks parked side by side (like dock doors along a warehouse wall),
# numbered plainly 1..40 (the row scrolls horizontally). An MCC consolidation
# container is allocated one lane, or a contiguous group of adjacent lanes, to
# stage the multiple pallets waiting to be loaded into that same container. The
# lanes are a CYCLING STAGING BUFFER: the agent reuses lanes whose previous
# group has already been stuffed and released (see agents/mcc_planner.py), the
# way a real dispatch area works. PALLETS_PER_LANE sets how many staged pallets
# one lane holds.
RELEASING_LANES_TOTAL = int(os.getenv("PSCH_RELEASING_LANES", "40"))
RELEASING_LANES = [str(i) for i in range(1, RELEASING_LANES_TOTAL + 1)]
PALLETS_PER_LANE = int(os.getenv("PSCH_PALLETS_PER_LANE", "8"))

# MCC consolidation: one vessel call takes several consolidation boxes, so the
# agent chunks each destination's inbound MCC containers into groups of at most
# MCC_GROUP_SIZE, each stuffed into its own outbound container.
MCC_GROUP_SIZE = int(os.getenv("MCC_GROUP_SIZE", "6"))
