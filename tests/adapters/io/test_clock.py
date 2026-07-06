import re
import datetime
from bounded_loops.adapters.io.clock import UtcClock

ISO8601_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
)


def test_now_iso_matches_pattern():
    ts = UtcClock().now_iso()
    assert ISO8601_RE.match(ts), f"Bad format: {ts!r}"


def test_now_iso_is_utc():
    ts = UtcClock().now_iso()
    dt = datetime.datetime.fromisoformat(ts.rstrip("Z")).replace(
        tzinfo=datetime.timezone.utc
    )
    delta = abs((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
    assert delta < 2, "Clock drift > 2s — not wall-clock UTC"


def test_two_calls_are_monotone():
    c = UtcClock()
    t1, t2 = c.now_iso(), c.now_iso()
    assert t1 <= t2  # lexicographic order == chronological for this format


def test_conforms_to_clock_port():
    from bounded_loops.application.ports import ClockPort  # noqa: F401
    # Protocol structural check: UtcClock exposes now_iso() -> str
    assert hasattr(UtcClock(), "now_iso")
