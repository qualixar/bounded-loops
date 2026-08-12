"""Executable contract for F0.2 controller-owned subprocess turns."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time

from bounded_loops.adapters.runners.process_lifecycle import (
    ProcessTurn,
    TurnState,
)

# Generous, bounded waits so heavy CI load cannot turn asynchronous process
# startup or teardown into a false failure. On an idle machine these return in
# well under a second; the ceilings only bite when the box is saturated.
_READY_TIMEOUT_S = 30.0
_DEATH_TIMEOUT_S = 10.0
_WAIT_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.02


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _read_pid_when_ready(path: Path, timeout_s: float) -> int:
    """Return a fixture descendant's pid once its pidfile is fully written.

    Each descendant writes its pidfile *after* it has spawned, so a readable
    integer here is a genuine readiness signal: the process (and the child it
    forked) already exist and belong to the turn's process group. This tolerates
    the brief window where the file exists but has not yet been populated, and
    replaces the previous fixed 2s deadline that three nested Python interpreter
    cold-starts could blow through under load.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            text = ""
        if text:
            return int(text)
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(
        f"fixture descendant {path.name} did not start within {timeout_s:.0f}s"
    )


def _assert_pid_dead(pid: int, timeout_s: float) -> None:
    """Poll until ``pid`` is fully gone, allowing the OS to finish an async kill.

    ``cancel`` signals the whole process group synchronously, but the kernel
    tears the descendants down, and init reaps the resulting orphans,
    asynchronously. Polling for ProcessLookupError proves the descendant is
    truly gone without demanding it happen within a single scheduler tick.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"pid {pid} still alive {timeout_s:.0f}s after cancellation"
            )
        time.sleep(_POLL_INTERVAL_S)


def test_completed_turn_caps_output_and_redacts_secret(tmp_path: Path) -> None:
    turn = ProcessTurn.start(
        _python("print('prefix ' + 'x' * 100 + ' CANARY_SECRET')"),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=32,
        redactions=("CANARY_SECRET",),
    )

    result = turn.wait(timeout_s=2)

    assert result.state is TurnState.COMPLETED
    assert "CANARY_SECRET" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert len(result.stdout.encode("utf-8")) <= 32
    assert result.output_truncated is True


def test_cancel_terminates_parent_and_descendant_process_group(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"
    child_code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
            f"pathlib.Path({str(grandchild_pid_path)!r}).write_text(str(grandchild.pid), encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    turn = ProcessTurn.start(
        _python(code),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=1024,
    )
    try:
        # Wait on a real readiness signal (both descendants recorded their pids)
        # instead of a fixed sleep, so cancellation cannot race process startup.
        child_pid = _read_pid_when_ready(child_pid_path, _READY_TIMEOUT_S)
        grandchild_pid = _read_pid_when_ready(grandchild_pid_path, _READY_TIMEOUT_S)

        turn.cancel("test cancellation")
        turn.cancel("idempotent second cancellation")
        result = turn.wait(timeout_s=_WAIT_TIMEOUT_S)

        assert result.state is TurnState.CANCELLED
        # The guarantee under test is unchanged: the parent AND the descendant
        # process group are both terminated. We only tolerate the OS finishing
        # the (asynchronous) teardown and orphan reap within a bounded window.
        _assert_pid_dead(child_pid, _DEATH_TIMEOUT_S)
        _assert_pid_dead(grandchild_pid, _DEATH_TIMEOUT_S)
    finally:
        # Never leak the sleep(60) chain if an assertion above fails early:
        # cancel is idempotent and kills the whole process group.
        turn.cancel("test cleanup")


def test_wait_deadline_terminates_a_hanging_turn(tmp_path: Path) -> None:
    turn = ProcessTurn.start(
        _python("import time; time.sleep(60)"),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=1024,
    )

    result = turn.wait(timeout_s=0.05)

    assert result.state is TurnState.TIMED_OUT
    assert result.returncode is not None


# ── CON-04: cancel() must not be blocked by an in-flight wait() ───────────────


def test_concurrent_cancel_is_not_blocked_by_in_flight_wait(tmp_path: Path) -> None:
    """cancel() must not be serialised behind a blocking wait().

    Two complementary assertions, from weakest to strongest:

    STRUCTURAL (primary): self._lock must be acquirable from a third thread
    while wait() is blocking on _process.wait().  With the old bug, wait()
    held the lock across the entire blocking call, so this acquire would time
    out.  With the fix, the lock is released before _process.wait() runs, so
    the acquire returns immediately.  This is a structural proof that does not
    depend on scheduling jitter.

    TEMPORAL (secondary): cancel() must return in < 1.0s.  The wait timeout is
    5.0s; if the lock were held (old code), cancel() would block for ≈ 5.0s.
    1.0s is well below 5.0s (5× margin) and well above any realistic correct
    cancel latency, so this threshold is simultaneously generous for correctness
    and strict against the bug.  It is a backstop, not the primary proof.

    Mutation proof: revert wait() to hold self._lock during _process.wait() →
    the structural assertion fails (lock.acquire returns False after 0.5s).
    """
    wait_timeout_s = 5.0  # long enough that wait() won't time out on its own

    turn = ProcessTurn.start(
        _python("import time; time.sleep(60)"),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        output_limit_bytes=1024,
    )

    wait_result: list[object] = []

    def _run_wait() -> None:
        wait_result.append(turn.wait(timeout_s=wait_timeout_s))

    wait_thread = threading.Thread(target=_run_wait, daemon=True)
    wait_thread.start()
    # Give wait() enough time to enter the blocking _process.wait() section.
    # 0.2s is ~1000× more than the brief with-lock section takes; even on a
    # heavily loaded CI machine, wait() will be in _process.wait() by then.
    time.sleep(0.2)

    # ── Structural assertion (primary) ────────────────────────────────────────
    # Old code: wait() holds self._lock here → acquire blocks for up to 0.5s,
    # returns False → assertion fails.
    # New code: lock is released before _process.wait() → acquire returns
    # immediately → assertion passes.
    lock_free = turn._lock.acquire(blocking=True, timeout=0.5)
    if lock_free:
        turn._lock.release()
    assert lock_free, (
        "self._lock is held while wait() blocks on _process.wait() — "
        "CON-04 regression: cancel() will be starved for the full wait timeout"
    )

    # ── Temporal assertion (secondary backstop) ────────────────────────────────
    t0 = time.monotonic()
    turn.cancel("test-cancel")
    cancel_elapsed = time.monotonic() - t0

    wait_thread.join(timeout=_WAIT_TIMEOUT_S)
    result = wait_result[0] if wait_result else None

    assert cancel_elapsed < 1.0, (
        f"cancel() took {cancel_elapsed:.3f}s — expected < 1.0s "
        f"(wait_timeout_s={wait_timeout_s}; old bug serialises cancel behind full wait)"
    )
    assert result is not None, "wait_thread did not return within deadline"
    assert getattr(result, "state") is TurnState.CANCELLED
