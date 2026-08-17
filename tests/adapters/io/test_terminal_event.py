"""
Terminal-event tests.

The two things that must hold: the line is machine-readable, and adding it changed
no exit code. The second is why `test_exit_codes_are_unchanged_by_the_event` exists
in the CLI test rather than here — but the contract is stated in this module's
docstring and pinned there.
"""

from __future__ import annotations

import io
import json

import pytest

from bounded_loops.adapters.io.terminal_event import (
    EVENT_PREFIX,
    SUPPRESS_ENV,
    build_terminal_event,
    emit_terminal_event,
)


def _parse(stream: io.StringIO) -> dict:
    line = stream.getvalue().strip()
    assert line.startswith(EVENT_PREFIX + " "), line
    return json.loads(line[len(EVENT_PREFIX) + 1 :])


def test_emits_one_parseable_line() -> None:
    stream = io.StringIO()
    emit_terminal_event(status="HALT", reason="no-progress", stream=stream)
    assert stream.getvalue().count("\n") == 1, "must be exactly one line"
    payload = _parse(stream)
    assert payload["event"] == "terminal"
    assert payload["status"] == "HALT"
    assert payload["reason"] == "no-progress"


@pytest.mark.parametrize("status", ["HALT", "PAUSE", "KILLED", "ERROR"])
def test_non_done_statuses_are_flagged_for_alerting(status: str) -> None:
    """Deciding what pages someone belongs to the contract, not each reader's config."""
    assert build_terminal_event(status=status)["alert"] is True


def test_done_is_emitted_but_not_flagged() -> None:
    """A monitor that only hears failures cannot tell healthy from not-running."""
    payload = build_terminal_event(status="DONE")
    assert payload["alert"] is False
    assert payload["status"] == "DONE"


def test_carries_the_ledger_head_so_the_alert_is_checkable() -> None:
    digest = "a" * 64
    payload = build_terminal_event(status="HALT", ledger_head=digest)
    assert payload["ledger_head"] == digest


def test_empty_optional_fields_are_omitted_not_null() -> None:
    """A consumer should not have to distinguish absent from null."""
    payload = build_terminal_event(status="DONE")
    for key in ("reason", "subject", "run_id", "ledger_head", "handoff"):
        assert key not in payload


def test_suppression_env_silences_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SUPPRESS_ENV, "1")
    stream = io.StringIO()
    assert emit_terminal_event(status="HALT", stream=stream) is None
    assert stream.getvalue() == ""


def test_a_broken_stream_never_raises() -> None:
    """An alerting path that can fail the run it observes is worse than none."""

    class Exploding(io.StringIO):
        def write(self, _s: str) -> int:
            raise OSError("stderr is gone")

    payload = emit_terminal_event(status="HALT", stream=Exploding())
    assert payload is not None, "the payload should still be returned"
    assert payload["status"] == "HALT"


def test_goes_to_the_given_stream_not_stdout(capsys: pytest.CaptureFixture) -> None:
    """stdout carries the --json outcome; a second object there breaks `jq`."""
    stream = io.StringIO()
    emit_terminal_event(status="HALT", stream=stream)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert stream.getvalue() != ""
