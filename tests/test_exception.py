"""Tests for the exception agent (agents/exception.py)."""
from datetime import timedelta
from pathlib import Path

from agents import mcc_planner
from agents.exception import ExceptionBrain, find_exceptions, scan_exceptions
from agents.runtime import AgentRuntime
from data import store
from data.simulator import generate
from config import SIM_NOW


def _seeded(tmp_path: Path, seed: int = 13, n: int = 60) -> Path:
    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=seed, n_containers=n), db)
    mcc_planner.plan(db)
    return db


def test_scan_runs_at_seed_time(tmp_path):
    db = _seeded(tmp_path)
    exc = find_exceptions(db, now=SIM_NOW)
    assert isinstance(exc, list)
    # Every finding is complete: issue + detail + recommendation.
    for e in exc:
        assert e["issue"] and e["detail"] and e["recommendation"]


def test_customs_holds_flagged(tmp_path):
    db = _seeded(tmp_path)
    containers = store.get_containers(db)
    held = [c for c in containers if c.get("customs_status") == "held"]
    exc = find_exceptions(db, now=SIM_NOW)
    held_kinds = [e for e in exc if e["kind"] == "customs_hold"]
    assert len(held_kinds) == len(held)  # every held container is surfaced


def test_receipt_eta_missed_flagged_for_delayed_container(tmp_path):
    db = _seeded(tmp_path)
    plans = store.get_mcc_plans(db)
    # Pick a container delayed by >= 2h and probe just past its promised ETA
    # (still before the delayed arrival) -> the slip is an exception.
    delayed = [p for p in plans if float(p.get("delay_hours") or 0.0) >= 2.0]
    assert delayed, "the seeded wave should contain road-delayed containers"
    p = delayed[0]
    probe = p["psch_receipt_eta"] + timedelta(hours=1)
    exc = find_exceptions(db, now=probe)
    mine = [e for e in exc if e["container_id"] == p["container_id"]]
    assert any(e["kind"] == "receipt_eta_missed" for e in mine)


def test_exception_brain_returns_ranked_summary(tmp_path):
    db = _seeded(tmp_path)
    brain = ExceptionBrain()
    result = brain.run("exception watch", {}, db)
    assert result.ok
    assert isinstance(result.events, list)
    assert "Attention needed" in result.summary or "No exceptions" in result.summary


def test_suggested_actions_are_concrete(tmp_path):
    """Every finding carries a gated AI suggestion the panel can propose."""
    db = _seeded(tmp_path)
    exc = find_exceptions(db, now=SIM_NOW + timedelta(days=14))
    assert exc
    for e in exc:
        act = e["suggested_action"]
        if e["kind"] == "customs_hold":
            assert act is None  # the correct action is inaction until cleared
        elif e["kind"] == "vessel_etd_slip":
            if act is not None:
                assert act["tool"] == "release_lane" and act["args"].get("container_id")
        else:
            assert act and act["tool"] and act["args"] and act["label"]
            assert act["tool"] in {"reassign_bin", "reschedule_receiving_area", "release_lane"}


def test_scan_is_pure_and_untraced(tmp_path):
    db = _seeded(tmp_path)
    trace_before = len(store.get_trace(path=db))
    plans = store.get_mcc_plans(db)
    outbounds = store.get_outbound_containers(db)
    containers = store.get_containers(db)
    vessels = store.get_vessels(db)
    scan_exceptions(plans, outbounds, containers, vessels, now=SIM_NOW + timedelta(days=14))
    trace_after = len(store.get_trace(path=db))
    assert trace_after == trace_before  # the 8s poll must never flood the trace


def test_vessel_etd_slip_flagged_when_clock_passes_etd(tmp_path):
    db = _seeded(tmp_path)
    vessels = store.get_vessels(db)
    docked = [v for v in vessels if v.get("status") == "docked"]
    assert docked
    # Advance well past every ETD: docked vessels have slipped.
    late = SIM_NOW + timedelta(days=30)
    exc = find_exceptions(db, now=late)
    kinds = {e["kind"] for e in exc}
    assert "vessel_etd_slip" in kinds


def test_trace_contains_exception_agent_run(tmp_path):
    db = _seeded(tmp_path)
    rt = AgentRuntime(brain=ExceptionBrain(), store_path=db, autonomy="advisory")
    rt.run("what needs attention?")
    events = [e["event"] for e in store.get_trace(path=db)]
    assert "agent_run_start" in events and "agent_run_end" in events
    start = next(e for e in store.get_trace(path=db) if e["event"] == "agent_run_start")
    assert start["detail"]["brain"] == "exception-agent-v1"
