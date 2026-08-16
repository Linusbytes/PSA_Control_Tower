from data import store


def test_trace_round_trip(tmp_path):
    db = tmp_path / "t.db"
    store.init_db(db)
    store.record_event("test", "event1", {"k": "v"}, db)
    store.record_event("test", "event2", None, db)

    trace = store.get_trace(path=db)
    assert len(trace) == 2
    assert trace[0]["event"] == "event2"  # newest first
    assert trace[0]["detail"] == {}
    assert trace[1]["detail"] == {"k": "v"}


def test_trace_cleared_on_reseed(tmp_path):
    db = tmp_path / "t.db"
    from data.simulator import generate

    store.init_db(db)
    store.load_scenario(generate(seed=1, n_containers=20), db)
    store.record_event("agent", "run", {}, db)

    store.load_scenario(generate(seed=2, n_containers=20), db)
    assert store.get_trace(path=db) == []
