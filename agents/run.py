"""CLI: seed data (if needed) and run the MCC planning agent.

Usage:
    python -m agents.run
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from data import store  # noqa: E402
from data.seed import seed_if_empty  # noqa: E402
from agents import mcc_planner  # noqa: E402


def main() -> None:
    if seed_if_empty(DB_PATH):
        print("Seeded a fresh scenario.")

    mcc_planner.plan(DB_PATH)

    plans = store.get_mcc_plans(DB_PATH)
    outbounds = store.get_outbound_containers(DB_PATH)

    print(f"Planned {len(plans)} inbound MCC containers:")
    for p in sorted(plans, key=lambda x: x["psch_receipt_eta"]):
        status = mcc_planner.journey_status(p)
        print(
            f"  {status:<15} {p['container_id']}  via {p['carrying_vessel_name']}  "
            f"receipt ETA {p['psch_receipt_eta']:%d %b %H:%M}Z  -> "
            f"{p['receiving_area'].replace(chr(0xB7), '-')} / "
            f"{p['bin_location']} ({p['consolidation_group'] or 'no group yet'})"
        )

    print(f"Planned {len(outbounds)} outbound consolidation containers:")
    for o in sorted(outbounds, key=lambda x: x["eta_loading_area"]):
        status = mcc_planner.outbound_status(o)
        print(
            f"  {status:<10} {o['container_id']}  -> {o['destination']}  bound "
            f"{o['bound_vessel_name']} ({o['bound_vessel_id']})  "
            f"ETA loading area {o['eta_loading_area']:%d %b %H:%M}Z  {o['loading_lane']}"
        )


if __name__ == "__main__":
    main()
