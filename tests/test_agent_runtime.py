"""Tests for the agentic AI seam: tool registry, brains, and runtime gates."""
import json
from pathlib import Path

import pytest

from agents import mcc_planner
from agents import tools
from agents.brain import AgenticAPIBrain, BrainResult, RuleBasedBrain, default_brain
from agents.runtime import AgentRuntime, ADVISORY, AUTONOMOUS, SEMI_AUTONOMOUS
from data import store
from data.simulator import generate


def _seeded(tmp_path, seed=11, n=60):
    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=seed, n_containers=n), db)
    return db


# --- Tool registry --------------------------------------------------------------


def test_registry_exposes_openai_style_schemas():
    schemas = tools.tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert "list_vessels" in names
    # The LLM may only adjust plans granularly: the whole-batch writers are
    # rule-planner-only, and the granular change tools are visible instead.
    assert "save_mcc_plans" not in names
    assert "save_outbound_containers" not in names
    assert {"reassign_bin", "reschedule_receiving_area", "release_lane"} <= names
    for s in schemas:
        assert s["type"] == "function"
        assert s["function"]["parameters"]["type"] == "object"


def test_registry_keeps_bulk_tools_for_rule_planner():
    # The rule planner still reaches the bulk writers through call_tool.
    assert "save_mcc_plans" in tools.TOOLS
    assert "save_outbound_containers" in tools.TOOLS
    assert tools.TOOLS["save_mcc_plans"].expose_to_llm is False


def test_call_tool_reads_and_traces(tmp_path):
    db = _seeded(tmp_path)
    vessels = tools.call_tool("list_vessels", {}, db)
    assert isinstance(vessels, list) and len(vessels) > 0

    trace = store.get_trace(path=db)
    call = next(e for e in trace if e["event"] == "tool_call")
    assert call["actor"] == "agent"
    assert call["detail"]["tool"] == "list_vessels"
    assert call["detail"]["permission"] == "read"


def test_call_tool_unknown_raises(tmp_path):
    db = _seeded(tmp_path)
    with pytest.raises(KeyError):
        tools.call_tool("no_such_tool", {}, db)


def test_planner_routes_through_registry(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    trace = store.get_trace(path=db)
    calls = {e["detail"]["tool"] for e in trace if e["event"] == "tool_call"}
    assert {"list_vessels", "list_containers", "list_shipments", "list_vessel_stowage",
            "save_mcc_plans", "save_outbound_containers"} <= calls


# --- Brains ----------------------------------------------------------------------


def test_rule_based_brain_produces_result(tmp_path):
    db = _seeded(tmp_path)
    brain = RuleBasedBrain()
    result = brain.run("Plan MCC consolidation for the next 24h", {}, db)
    assert isinstance(result, BrainResult)
    assert result.ok
    assert "MCC containers" in result.summary
    assert len(store.get_mcc_plans(db)) > 0


def test_agentic_api_brain_not_configured_is_safe(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    # Hermetic: ignore any AGENTIC_API_ENDPOINT in .env for this test.
    monkeypatch.setattr("agents.brain.AGENTIC_API_ENDPOINT", "")
    brain = AgenticAPIBrain(endpoint=None)
    assert not brain.configured
    result = brain.run("Plan MCC consolidation", {}, db)
    assert result.ok is False
    assert "not configured" in result.error
    # The stack stays usable: the rule brain still runs.
    assert RuleBasedBrain().run("plan", {}, db).ok


def test_default_brain_falls_back_to_rule_brain(monkeypatch):
    monkeypatch.setattr("agents.brain.AGENTIC_API_ENDPOINT", "")
    brain = default_brain()
    assert isinstance(brain, RuleBasedBrain)

    monkeypatch.setattr("agents.brain.AGENTIC_API_ENDPOINT", "https://example.com/v1/chat/completions")
    brain = default_brain()
    assert isinstance(brain, AgenticAPIBrain)


# --- Runtime gates ---------------------------------------------------------------


def test_read_tools_never_need_approval(tmp_path):
    db = _seeded(tmp_path)
    for autonomy in (ADVISORY, SEMI_AUTONOMOUS, AUTONOMOUS):
        rt = AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=autonomy)
        assert rt.requires_approval("list_vessels") is False


def test_mutate_tools_gated_by_autonomy(tmp_path):
    db = _seeded(tmp_path)
    assert AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=ADVISORY).requires_approval("save_mcc_plans") is True
    assert AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=SEMI_AUTONOMOUS).requires_approval("save_mcc_plans") is False
    assert AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=AUTONOMOUS).requires_approval("save_mcc_plans") is False


def test_approval_tool_queued_for_human(tmp_path):
    db = _seeded(tmp_path)

    # A test-only execution-level tool (real-world action, e.g. "release lane").
    tools.register_tool(
        "release_container",
        "TEST ONLY: execute a real-world container release.",
        {"container_id": {"type": "string"}},
        tools.APPROVAL,
        lambda args, path: {"released": args.get("container_id")},
    )
    try:
        rt = AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=ADVISORY)
        assert rt.requires_approval("release_container") is True
        out = rt.gate("release_container", {"container_id": "MSCU1"})
        assert out["status"] == "pending_approval"
        # The action did NOT execute; approving it does.
        trace = store.get_trace(path=db)
        assert any(e["event"] == "approval_required" for e in trace)
        assert rt.approve("release_container", {"container_id": "MSCU1"}) == {"released": "MSCU1"}
        trace = store.get_trace(path=db)
        assert any(e["event"] == "approved" for e in trace)
    finally:
        tools.TOOLS.pop("release_container", None)


def test_runtime_records_run_in_trace(tmp_path):
    db = _seeded(tmp_path)
    rt = AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=ADVISORY)
    result = rt.run("Plan MCC consolidation for the next 24h")
    assert result.ok
    events = [e["event"] for e in store.get_trace(path=db)]
    assert "agent_run_start" in events and "agent_run_end" in events
    start = next(e for e in store.get_trace(path=db) if e["event"] == "agent_run_start")
    assert start["detail"]["brain"] == "rule-based-mcc-v1"
    assert start["detail"]["autonomy"] == "advisory"


def test_invalid_autonomy_rejected(tmp_path):
    db = _seeded(tmp_path)
    with pytest.raises(ValueError):
        AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy="rogue")


# --- Granular plan-change tools (Phase 4) ----------------------------------------


def _mcc_plan_that_is_binned(db):
    """First plan whose bin_location is a real rack bin (AISLE-LEVEL-BAY)."""
    from data.facility import bin_id_of, is_bin

    for p in store.get_mcc_plans(db):
        if is_bin(bin_id_of(p.get("bin_location"))):
            return p
    raise AssertionError("no binned MCC plan in the seeded scenario")


def test_reassign_bin_adjusts_only_that_plan(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    plans_before = store.get_mcc_plans(db)
    target = _mcc_plan_that_is_binned(db)
    others = [p["bin_location"] for p in plans_before if p["container_id"] != target["container_id"]]

    result = tools.call_tool(
        "reassign_bin",
        {"container_id": target["container_id"], "bin_location": "5-08-1B", "reason": "test move"},
        db,
    )
    assert result["bin_location"] == "Bin 5-08-1B"
    plans_after = store.get_mcc_plans(db)
    moved = next(p for p in plans_after if p["container_id"] == target["container_id"])
    assert moved["bin_location"] == "Bin 5-08-1B"
    assert "test move" in moved["reasoning"]
    # Only one plan changed; no whole-batch rewrite.
    assert [p["bin_location"] for p in plans_after if p["container_id"] != target["container_id"]] == others
    assert len(plans_after) == len(plans_before)


def test_reassign_bin_rejects_invalid_bin(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    target = _mcc_plan_that_is_binned(db)
    with pytest.raises(ValueError):
        tools.call_tool(
            "reassign_bin",
            {"container_id": target["container_id"], "bin_location": "not-a-bin"},
            db,
        )


def test_reassign_bin_unknown_container(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    with pytest.raises(KeyError):
        tools.call_tool("reassign_bin", {"container_id": "NOPE0000000", "bin_location": "5-08-1B"}, db)


def test_granular_change_gated_in_advisory_then_applies_on_approve(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    target = _mcc_plan_that_is_binned(db)
    rt = AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=ADVISORY)
    args = {"container_id": target["container_id"], "bin_location": "3-04-2B", "reason": "operator preference"}

    out = rt.gate("reassign_bin", args)
    assert out["status"] == "pending_approval"
    # Nothing changed until a human approves.
    plan = next(p for p in store.get_mcc_plans(db) if p["container_id"] == target["container_id"])
    assert plan["bin_location"] == target["bin_location"]

    rt.approve("reassign_bin", args)
    plan = next(p for p in store.get_mcc_plans(db) if p["container_id"] == target["container_id"])
    assert plan["bin_location"] == "Bin 3-04-2B"
    trace = store.get_trace(path=db)
    assert any(e["event"] == "approved" and e["detail"]["tool"] == "reassign_bin" for e in trace)


def test_release_lane_is_approval_and_advances_status(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "SIM_SPEED", 0)  # freeze the clock for determinism
    from agents.mcc_planner import outbound_status

    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    outbounds = store.get_outbound_containers(db)
    target = outbounds[0]
    rt = AgentRuntime(brain=RuleBasedBrain(), store_path=db, autonomy=ADVISORY)

    assert rt.requires_approval("release_lane") is True
    out = rt.gate("release_lane", {"container_id": target["container_id"], "reason": "berth waiting"})
    assert out["status"] == "pending_approval"
    # Not staged anymore once released (lane_release_time moved to now).
    rt.approve("release_lane", {"container_id": target["container_id"], "reason": "berth waiting"})
    updated = next(o for o in store.get_outbound_containers(db) if o["container_id"] == target["container_id"])
    assert outbound_status(updated, updated["lane_release_time"]) != "staged"


def test_llm_brain_never_executes_mutate_tools(tmp_path, monkeypatch):
    """The LLM loop returns mutate/approval tools as pending proposals only."""
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    target = _mcc_plan_that_is_binned(db)
    before = next(p for p in store.get_mcc_plans(db) if p["container_id"] == target["container_id"])["bin_location"]

    calls = {"n": 0}

    def fake_post(self, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "reassign_bin",
                                        "arguments": json.dumps(
                                            {
                                                "container_id": target["container_id"],
                                                "bin_location": "5-08-1B",
                                                "reason": "llm test",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": "Proposed the move; awaiting approval."}}]}

    monkeypatch.setattr(AgenticAPIBrain, "_post", fake_post)
    brain = AgenticAPIBrain(endpoint="https://example.invalid/v1/chat/completions")
    # The real path: the runtime runs the brain and records the proposal.
    result = AgentRuntime(brain=brain, store_path=db, autonomy=ADVISORY).run("move the container")
    assert result.ok
    assert any(e["tool"] == "reassign_bin" for e in result.events)
    # The plan did NOT change: the brain proposed, it never executed.
    after = next(p for p in store.get_mcc_plans(db) if p["container_id"] == target["container_id"])["bin_location"]
    assert after == before
    trace = store.get_trace(path=db)
    assert any(e["event"] == "approval_required" and e["detail"]["tool"] == "reassign_bin" for e in trace)


def test_terminal_snapshot_has_flow_vessel_and_outbound_counts(tmp_path):
    db = _seeded(tmp_path)
    mcc_planner.plan(db)
    snap = tools.call_tool("get_terminal_snapshot", {}, db)
    assert "vessels_by_status" in snap and sum(snap["vessels_by_status"].values()) > 0
    assert "containers_by_flow" in snap
    assert sum(snap["containers_by_flow"].values()) == snap["total_containers_planned"]
    assert "outbound_by_destination" in snap
    assert "containers_overdue_receipt" in snap
    assert "outbound_near_or_past_loading" in snap
