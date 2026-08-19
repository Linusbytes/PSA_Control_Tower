"""Generate and load a scenario into SQLite.

Usage:
    python -m data.seed --seed 42 --containers 60
"""
from __future__ import annotations

import argparse

from config import DB_PATH, N_CONTAINERS, SEED, SIM_NOW, sim_now as _live_now
from data import store
from data.simulator import generate


def seed(
    seed: int = SEED,
    n_containers: int = N_CONTAINERS,
    db_path=DB_PATH,
    sim_now=None,
) -> None:
    """Generate and load a scenario anchored to ``sim_now`` (default: the live clock)."""
    if sim_now is None:
        sim_now = _live_now()
    store.init_db(db_path)
    scenario = generate(seed=seed, n_containers=n_containers, sim_now=sim_now)
    store.load_scenario(scenario, db_path)
    store.record_event(
        "system",
        "scenario_seeded",
        {
            "seed": seed,
            "containers": len(scenario.containers),
            "bookings": len(scenario.bookings),
            "shipments": len(scenario.shipments),
            "vessels": len(scenario.vessels),
        },
        db_path,
    )
    print(
        f"Seeded {len(scenario.containers)} containers, {len(scenario.bookings)} bookings "
        f"-> {db_path}"
    )


def seed_if_empty(db_path=DB_PATH) -> bool:
    """Seed only if the store has no data yet. Returns True when it seeded."""
    if store.has_data(db_path):
        return False
    seed(db_path=db_path)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the synthetic port/PSCH scenario")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--containers", type=int, default=N_CONTAINERS)
    args = parser.parse_args()
    seed(seed=args.seed, n_containers=args.containers)
