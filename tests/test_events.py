from requiem.events import EventEmitter, parse_envelope


def test_emitter_assigns_monotonic_ids():
    log = []
    def append(envelope):
        envelope["event_id"] = len(log)
        log.append(envelope)
        return envelope["event_id"]

    em = EventEmitter(run_id="r1", append=append)
    em.emit_run_started("wf")
    em.emit_node_entered("a", attempt=1)
    em.emit_verb_completed("a", {"kind": "success"})

    assert [e["kind"] for e in log] == ["run_started", "node_entered", "verb_completed"]
    assert [e["event_id"] for e in log] == [0, 1, 2]
    for e in log:
        assert e["run_id"] == "r1"
        assert "ts" in e and "payload" in e


def test_envelope_validates():
    raw = {
        "event_id": 0, "run_id": "r1", "ts": "2026-05-31T12:00:00+00:00",
        "kind": "node_entered", "schema_version": 1, "node_id": "a",
        "team_id": None, "agent_id": None,
        "payload": {"attempt": 1},
    }
    ev = parse_envelope(raw)
    assert ev.kind == "node_entered"
    assert ev.node_id == "a"
