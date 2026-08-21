from __future__ import annotations

import sqlite3

import pytest

from plugins.platforms.workshop.storage import (
    WorkshopBacklogExceeded,
    WorkshopConflictError,
    WorkshopLedger,
)


def _ledger(tmp_path, **kwargs):
    return WorkshopLedger(tmp_path / "state.db", **kwargs)


def _turn(ledger: WorkshopLedger, **overrides):
    values = {
        "client_turn_id": "client-1",
        "workspace_id": "workspace-1",
        "chat_id": "chat-1",
        "session_key": "agent:main:workshop:thread:workspace-1:chat-1",
        "session_id": "session-1",
        "catalog_version": "catalog-a",
        "request_digest": "request-a",
    }
    values.update(overrides)
    return ledger.create_turn(**values)


def test_schema_lives_in_shared_state_db(tmp_path):
    ledger = _ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workshop_%'"
            )
        }
    assert {
        "workshop_turns",
        "workshop_events",
        "workshop_tool_calls",
        "workshop_wakes",
        "workshop_deltas",
    }.issubset(tables)


def test_turn_creation_is_idempotent_but_conflicting_reuse_fails(tmp_path):
    ledger = _ledger(tmp_path)
    created, is_new = _turn(ledger)
    repeated, repeated_is_new = _turn(ledger)

    assert is_new is True
    assert repeated_is_new is False
    assert repeated.turn_id == created.turn_id

    with pytest.raises(WorkshopConflictError):
        _turn(ledger, request_digest="different")


def test_replay_persists_text_and_complete_args_but_not_live_only_deltas(tmp_path):
    ledger = _ledger(tmp_path)
    turn, _ = _turn(ledger)

    started = ledger.append_event(
        turn_id=turn.turn_id,
        event="turn.started",
        payload={"catalog_version": "catalog-a"},
    )
    thinking = ledger.append_event(
        turn_id=turn.turn_id,
        event="thinking.delta",
        payload={"delta": "secret thought"},
    )
    text = ledger.append_event(
        turn_id=turn.turn_id,
        event="text.delta",
        payload={"delta": "hello"},
    )
    arguments = ledger.append_event(
        turn_id=turn.turn_id,
        event="tool_call.arguments.delta",
        payload={"call_id": "call-1", "delta": '{"pa'},
    )
    complete = ledger.append_event(
        turn_id=turn.turn_id,
        event="tool_call.end",
        payload={"call_id": "call-1", "arguments": {"path": "README.md"}},
    )

    replay = ledger.list_events(turn.turn_id)
    assert [event.seq for event in replay] == [started.seq, text.seq, complete.seq]
    assert thinking.seq not in {event.seq for event in replay}
    assert arguments.seq not in {event.seq for event in replay}
    assert replay[-1].payload["arguments"] == {"path": "README.md"}


def test_finish_turn_is_atomic_and_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    turn, _ = _turn(ledger)
    terminal = ledger.finish_turn(
        turn_id=turn.turn_id,
        state="completed",
        stop_reason="complete",
        payload={"final_text": "done"},
    )
    repeated = ledger.finish_turn(
        turn_id=turn.turn_id,
        state="completed",
        stop_reason="complete",
        payload={"final_text": "done"},
    )

    assert repeated == terminal
    record = ledger.get_turn(turn.turn_id)
    assert record is not None
    assert record.state == "completed"
    assert record.stop_reason == "complete"
    assert ledger.count_active_turns() == 0

    with pytest.raises(WorkshopConflictError):
        ledger.finish_turn(
            turn_id=turn.turn_id,
            state="completed",
            stop_reason="different",
            payload={"final_text": "done"},
        )


def test_backlog_limit_fails_without_advancing_sequence(tmp_path):
    ledger = _ledger(tmp_path, max_event_backlog_bytes=250)
    turn, _ = _turn(ledger)

    with pytest.raises(WorkshopBacklogExceeded):
        ledger.append_event(
            turn_id=turn.turn_id,
            event="text.delta",
            payload={"delta": "x" * 1000},
        )
    record = ledger.get_turn(turn.turn_id)
    assert record is not None
    assert record.next_seq == 1
    assert record.event_bytes == 0


def test_pending_tool_calls_are_bounded_and_results_are_idempotent(tmp_path):
    ledger = _ledger(tmp_path, max_pending_calls=2)
    turn, _ = _turn(ledger)
    assert ledger.register_tool_call(
        turn_id=turn.turn_id, call_id="call-1", name="writeFile", arguments={"a": 1}
    )
    assert ledger.register_tool_call(
        turn_id=turn.turn_id, call_id="call-2", name="readFile", arguments={"b": 2}
    )
    with pytest.raises(WorkshopConflictError):
        ledger.register_tool_call(
            turn_id=turn.turn_id, call_id="call-3", name="editFile", arguments={}
        )

    assert ledger.resolve_tool_call(
        turn_id=turn.turn_id, call_id="call-1", result={"ok": True}, is_error=False
    )
    assert not ledger.resolve_tool_call(
        turn_id=turn.turn_id, call_id="call-1", result={"ok": True}, is_error=False
    )
    with pytest.raises(WorkshopConflictError):
        ledger.resolve_tool_call(
            turn_id=turn.turn_id, call_id="call-1", result={"ok": False}, is_error=False
        )


def test_dead_letter_and_retention_are_durable(tmp_path):
    ledger = _ledger(tmp_path, completed_retention_seconds=10)
    turn, _ = _turn(ledger)
    assert ledger.record_wake(
        producer_type="spawn_result", producer_id="child-1", turn_id=turn.turn_id
    )
    ledger.mark_wake_dead_letter(
        producer_type="spawn_result", producer_id="child-1", error="HTTP 401"
    )
    assert ledger.dead_letter_wake_count() == 1

    ledger.finish_turn(
        turn_id=turn.turn_id,
        state="error",
        stop_reason="wake_rejected",
        timestamp=100,
    )
    assert ledger.prune_completed(now=109) == 0
    assert ledger.prune_completed(now=111) == 1
    assert ledger.get_turn(turn.turn_id) is None


def test_restart_recovery_durably_interrupts_active_turns(tmp_path):
    ledger = _ledger(tmp_path)
    first, _ = _turn(ledger)
    second, _ = _turn(
        ledger,
        client_turn_id="client-2",
        request_digest="request-2",
        turn_id="wturn_second",
    )
    ledger.set_turn_state(second.turn_id, "running")

    assert ledger.recover_active_turns() == 2
    assert ledger.recover_active_turns() == 0
    for turn_id in (first.turn_id, second.turn_id):
        record = ledger.get_turn(turn_id)
        assert record is not None
        assert record.state == "interrupted"
        terminal = ledger.list_events(turn_id)[-1]
        assert terminal.event == "turn.end"
        assert terminal.payload["stop_reason"] == "gateway_restart"
