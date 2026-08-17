"""
Structured terminal-status events on stderr.

The operational question this answers: how does a HALT at 3am reach on-call?
Before 0.6.7 it did not. HALT, PAUSE, KILLED and ERROR all exit 1 and printed
prose, so a scheduler could tell that something went wrong and nothing else, and a
log scraper had to parse English to learn which.

Why not distinct exit codes, which would be the obvious fix: anyone already
scripting `bl run` branches on 0-versus-non-zero today, and handing 2 to PAUSE
silently reclassifies their PAUSE as a usage error. That is a breaking change
dressed as an improvement. **Exit codes are unchanged.** The addition is one
machine-readable line on stderr, which nothing existing parses.

Why stderr and not stdout: stdout carries the `--json` outcome, and adding a second
object to that stream would break a caller who parses it. (Note that `bl run` already
prints its trust banner to stdout ahead of the JSON, so that stream is not
machine-pure today — a separate pre-existing wart, not one this event should widen.)

Why always on rather than opt-in: an alert nobody enabled is not an alert.
`BOUNDED_LOOPS_NO_EVENTS=1` exists for test fixtures that assert on exact stderr,
not as a deployment knob.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import IO

EVENT_PREFIX = "BL_EVENT"
SUPPRESS_ENV = "BOUNDED_LOOPS_NO_EVENTS"

# DONE is included deliberately. A monitor that only ever hears about failures
# cannot distinguish "healthy" from "not running", which is how a silently dead
# scheduler goes unnoticed for a week.
_ALERTING_STATUSES = frozenset({"HALT", "PAUSE", "KILLED", "ERROR"})


def events_suppressed() -> bool:
    return os.environ.get(SUPPRESS_ENV, "").strip().lower() in {"1", "true", "yes"}


def build_terminal_event(
    *,
    status: str,
    reason: str = "",
    subject: str = "",
    run_id: str = "",
    laps: int | None = None,
    ledger_head: str = "",
    handoff: str = "",
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the event payload.

    Split from emission so a test can assert on the structure without capturing
    a stream, and so the fields are visible in one place rather than inferred
    from a format string.

    `alert` is computed here rather than left to the consumer: deciding which
    statuses page someone is a property of the contract, not of each reader's
    config file.
    """
    payload: dict[str, object] = {
        "event": "terminal",
        "status": status,
        "alert": status.upper() in _ALERTING_STATUSES,
    }
    if reason:
        payload["reason"] = reason
    if subject:
        payload["subject"] = subject
    if run_id:
        payload["run_id"] = run_id
    if laps is not None:
        payload["laps"] = laps
    # The head is what makes the event checkable later: an alert naming a digest
    # can be tied back to a ledger, and `bl verify --expect-head` accepts it.
    if ledger_head:
        payload["ledger_head"] = ledger_head
    if handoff:
        payload["handoff"] = handoff
    if extra:
        payload.update(dict(extra))
    return payload


def emit_terminal_event(
    *,
    status: str,
    reason: str = "",
    subject: str = "",
    run_id: str = "",
    laps: int | None = None,
    ledger_head: str = "",
    handoff: str = "",
    extra: Mapping[str, object] | None = None,
    stream: IO[str] | None = None,
) -> dict[str, object] | None:
    """Write one `BL_EVENT {json}` line to stderr. Returns the payload, or None.

    Never raises. A broken stderr must not change a run's terminal status: an
    alerting path that can fail a run it was added to observe is worse than no
    alerting path.
    """
    payload = build_terminal_event(
        status=status,
        reason=reason,
        subject=subject,
        run_id=run_id,
        laps=laps,
        ledger_head=ledger_head,
        handoff=handoff,
        extra=extra,
    )
    if events_suppressed():
        return None
    target = stream if stream is not None else sys.stderr
    try:
        # `default=str` and the TypeError branch are both load-bearing. Serialising
        # runs at the very end of a run, and a field that is not JSON-native — a
        # value object, a stub, anything a caller passes through `extra` — would
        # otherwise raise here and take the whole run down after it had already
        # finished. Found by the CLI suite, whose doubles hand this a mock `laps`.
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        target.write(f"{EVENT_PREFIX} {encoded}\n")
        target.flush()
    except (OSError, ValueError, TypeError):
        return payload
    return payload
