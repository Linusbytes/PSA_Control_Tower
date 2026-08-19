"""Tests for the PSA Intelligence rule brain (agents/intel.py)."""
from pathlib import Path

from agents import mcc_planner
from agents.brain import AgenticAPIBrain
from agents.intel import IntelRuleBrain, default_intel_brain
from data import store
from data.simulator import generate


def _seeded(tmp_path: Path, seed: int = 7, n: int = 60) -> Path:
    db = tmp_path / "port.db"
    store.init_db(db)
    store.load_scenario(generate(seed=seed, n_containers=n), db)
    mcc_planner.plan(db)
    return db


def _ask(brain: IntelRuleBrain, db: Path, q: str) -> str:
    return brain.run(q, {}, db).summary


# --- container tracking --------------------------------------------------------


def test_tracks_a_container(tmp_path):
    db = _seeded(tmp_path)
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    ans = _ask(IntelRuleBrain(), db, f"where is {cid}?")
    assert cid in ans
    assert "journey status" in ans.lower()
    assert "receiving" in ans.lower()


def test_explains_plan_reasoning(tmp_path):
    db = _seeded(tmp_path)
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    ans = _ask(IntelRuleBrain(), db, f"why is {cid} in that bin?")
    assert cid in ans
    assert "plan" in ans.lower()


def test_unknown_container_reports_missing(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "where is ABCU0000001?")
    assert "ABCU0000001" in ans
    assert "couldn't find" in ans.lower()


# --- warehouse / KPIs ----------------------------------------------------------


def test_warehouse_answer_has_bin_util(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "what is the bin utilisation?")
    assert "bin utilisation" in ans.lower()
    assert "%" in ans


def test_pipeline_counts_stages(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "how many containers are at sea?")
    assert "pipeline" in ans.lower()
    assert "en route (sea)" in ans.lower()


# --- vessels / flows / trace ---------------------------------------------------


def test_vessel_lookup(tmp_path):
    db = _seeded(tmp_path)
    vessels = store.get_vessels(db)
    name = vessels[0]["vessel_name"]
    ans = _ask(IntelRuleBrain(), db, f"where is {name}?")
    assert name in ans


def test_flow_counts(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "how many top up jobs?")
    assert "top up" in ans.lower()


def test_mcc_defined_as_multi_country_consolidation(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "what is MCC?")
    assert "multi-country consolidation" in ans.lower()
    # It must never describe MCC as "merged" or "mixed" container consolidation.
    assert "merged" not in ans.lower()
    assert "mixed" not in ans.lower()


def test_recent_trace(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "what changed in the last hour?")
    assert "trace" in ans.lower()


# --- help / fallback -----------------------------------------------------------


def test_help_lists_capabilities(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "help")
    assert "PSA Intelligence" in ans
    assert "where is SEAU" in ans


def test_fallback_is_helpful(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "what is the meaning of life?")
    assert "don't have a direct answer" in ans.lower()


# --- plan-change proposals (Phase 4) --------------------------------------------


def _binned_plan(db):
    from data.facility import bin_id_of, is_bin

    for p in store.get_mcc_plans(db):
        if is_bin(bin_id_of(p.get("bin_location"))):
            return p
    raise AssertionError("no binned plan")


def _ask_brain(brain, db, q):
    return brain.run(q, {}, db)


def test_proposes_reassign_bin_with_approval_event(tmp_path):
    db = _seeded(tmp_path)
    p = _binned_plan(db)
    result = _ask_brain(IntelRuleBrain(), db, f"move {p['container_id']} to bin 5-08-1B")
    assert "Proposed" in result.summary
    assert "waiting for your approval" in result.summary.lower()
    assert len(result.events) == 1
    ev = result.events[0]
    assert ev["kind"] == "pending_approval"
    assert ev["tool"] == "reassign_bin"
    assert ev["args"]["container_id"] == p["container_id"]
    assert ev["args"]["bin_location"] == "5-08-1B"
    # Nothing executed — the plan is unchanged until a human approves.
    plan = next(x for x in store.get_mcc_plans(db) if x["container_id"] == p["container_id"])
    assert plan["bin_location"] == p["bin_location"]


def test_proposes_reschedule_receiving_area(tmp_path):
    db = _seeded(tmp_path)
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    result = _ask_brain(IntelRuleBrain(), db, f"change the receiving area of {cid} to RA-4")
    assert "Proposed" in result.summary
    assert result.events[0]["tool"] == "reschedule_receiving_area"
    assert result.events[0]["args"]["receiving_area"] == "RA-4"


def test_proposes_release_lane_for_outbound(tmp_path):
    db = _seeded(tmp_path)
    outbounds = store.get_outbound_containers(db)
    cid = outbounds[0]["container_id"]
    result = _ask_brain(IntelRuleBrain(), db, f"release the lane of {cid}")
    assert "Proposed" in result.summary
    assert result.events[0]["tool"] == "release_lane"


def test_exception_intent_answers(tmp_path):
    db = _seeded(tmp_path)
    ans = _ask(IntelRuleBrain(), db, "what needs attention right now?")
    assert "attention" in ans.lower() or "No exceptions" in ans


# --- conversational follow-ups (memory) ----------------------------------------


def _followup_history(first_q: str, first_answer: str) -> list[dict]:
    return [
        {"role": "user", "text": first_q},
        {"role": "assistant", "text": first_answer},
    ]


def test_followup_its_vessel_resolves_to_last_container(tmp_path):
    db = _seeded(tmp_path)
    brain = IntelRuleBrain()
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    first = brain.run(f"where is {cid}?", {}, db).summary
    assert cid in first
    second = brain.run(
        "and what about its vessel?",
        {},
        db,
        history=_followup_history(f"where is {cid}?", first),
    ).summary
    assert cid in second
    # The answer is the carrying vessel's live track (name + voyage id).
    plan = next(x for x in store.get_mcc_plans(db) if x["container_id"] == cid)
    assert (plan["carrying_vessel_name"] or "").upper() in second.upper()


def test_followup_pronoun_re_tracks_last_container(tmp_path):
    db = _seeded(tmp_path)
    brain = IntelRuleBrain()
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    first = brain.run(f"where is {cid}?", {}, db).summary
    second = brain.run(
        "where is it now?",
        {},
        db,
        history=_followup_history(f"where is {cid}?", first),
    ).summary
    assert cid in second
    assert "journey status" in second.lower()


def test_followup_without_history_falls_back(tmp_path):
    db = _seeded(tmp_path)
    ans = IntelRuleBrain().run("and what about its vessel?", {}, db).summary
    assert "don't have a direct answer" in ans.lower()


def test_followup_does_not_break_new_questions(tmp_path):
    # "utilisation" contains "it" as a substring — a new question must not be
    # hijacked into a follow-up about the last container.
    db = _seeded(tmp_path)
    brain = IntelRuleBrain()
    plans = store.get_mcc_plans(db)
    cid = plans[0]["container_id"]
    first = brain.run(f"where is {cid}?", {}, db).summary
    second = brain.run(
        "what is the bin utilisation?",
        {},
        db,
        history=_followup_history(f"where is {cid}?", first),
    ).summary
    assert "bin utilisation" in second.lower()


# --- the seam: LLM brain takes over via config ---------------------------------


def test_default_intel_brain_swaps_on_config(monkeypatch, tmp_path):
    monkeypatch.setattr("agents.intel.AgenticAPIBrain.configured", property(lambda self: False))
    assert isinstance(default_intel_brain(), IntelRuleBrain)

    monkeypatch.setattr("agents.intel.AgenticAPIBrain.configured", property(lambda self: True))
    brain = default_intel_brain()
    assert isinstance(brain, AgenticAPIBrain)
