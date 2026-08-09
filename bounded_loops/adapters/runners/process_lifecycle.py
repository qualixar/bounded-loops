"""Controller-owned, bounded lifecycle for an external runner turn.

This module is deliberately independent of a particular agent CLI.  It gives
every future asynchronous runner the same non-negotiable process semantics:
one process group per turn, bounded tail logs, explicit timeout/cancellation,
and a terminal result that can be persisted as evidence.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import BinaryIO, Mapping, Sequence, cast

from bounded_loops.domain.models import TurnResult, TurnState

# Compatibility name while public callers migrate to the domain value.
ProcessTurnResult = TurnResult


class _TailBuffer:
    """Keep only the most recent bytes so child output cannot exhaust RAM."""

    def __init__(self, limit_bytes: int) -> None:
        self._limit_bytes = limit_bytes
        self._bytes = bytearray()
        self._truncated = False
        self._lock = threading.Lock()

    def append(self, data: bytes) -> None:
        with self._lock:
            self._bytes.extend(data)
            overflow = len(self._bytes) - self._limit_bytes
            if overflow > 0:
                del self._bytes[:overflow]
                self._truncated = True

    def text(self, redactions: Sequence[str]) -> str:
        with self._lock:
            value = bytes(self._bytes).decode("utf-8", errors="replace")
        for secret in redactions:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        encoded = value.encode("utf-8")
        if len(encoded) > self._limit_bytes:
            value = encoded[-self._limit_bytes :].decode("utf-8", errors="replace")
        return value

    @property
    def truncated(self) -> bool:
        with self._lock:
            return self._truncated


class ProcessTurn:
    """One controller-owned child process with bounded, group-wide shutdown."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        output_limit_bytes: int,
        redactions: Sequence[str],
        terminate_grace_s: float,
    ) -> None:
        self._process = process
        self._stdout = _TailBuffer(output_limit_bytes)
        self._stderr = _TailBuffer(output_limit_bytes)
        self._redactions = tuple(redactions)
        self._terminate_grace_s = terminate_grace_s
        self._state = TurnState.RUNNING
        self._lock = threading.RLock()
        self._reader_threads = (
            self._start_reader(cast(BinaryIO | None, process.stdout), self._stdout),
            self._start_reader(cast(BinaryIO | None, process.stderr), self._stderr),
        )

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        output_limit_bytes: int,
        redactions: Sequence[str] = (),
        input_text: str | None = None,
        terminate_grace_s: float = 0.25,
    ) -> ProcessTurn:
        """Start a turn in its own session so cancellation reaches descendants."""
        if not argv:
            raise ValueError("argv must not be empty")
        if output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if terminate_grace_s < 0:
            raise ValueError("terminate_grace_s must not be negative")

        stdin = subprocess.PIPE if input_text is not None else subprocess.DEVNULL
        if os.name == "posix":
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        else:  # pragma: no cover - exercised on Windows workers
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                env=dict(env),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        turn = cls(
            process,
            output_limit_bytes=output_limit_bytes,
            redactions=redactions,
            terminate_grace_s=terminate_grace_s,
        )
        if input_text is not None:
            threading.Thread(
                target=turn._write_stdin,
                args=(input_text.encode("utf-8"),),
                daemon=True,
                name="bounded-loops-turn-stdin",
            ).start()
        return turn

    @staticmethod
    def _start_reader(
        stream: BinaryIO | None,
        buffer: _TailBuffer,
    ) -> threading.Thread:
        def read() -> None:
            if stream is None:
                return
            while chunk := stream.read(8192):
                buffer.append(chunk)

        thread = threading.Thread(
            target=read,
            daemon=True,
            name="bounded-loops-turn-output",
        )
        thread.start()
        return thread

    def _write_stdin(self, data: bytes) -> None:
        stream = self._process.stdin
        if stream is None:
            return
        try:
            stream.write(data)
            stream.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                stream.close()
            except BrokenPipeError:
                pass

    def poll(self) -> TurnState:
        with self._lock:
            if self._state is TurnState.RUNNING and self._process.poll() is not None:
                self._state = TurnState.COMPLETED
            return self._state

    def cancel(self, _reason: str = "cancelled") -> None:
        """Cancel idempotently, killing the entire child process group if needed."""
        self._terminate(TurnState.CANCELLED)

    def wait(self, timeout_s: float | None = None) -> TurnResult:
        """Wait for completion, turning deadline expiry into a group-wide stop."""
        with self._lock:
            state = self.poll()
            if state is TurnState.RUNNING:
                try:
                    self._process.wait(timeout=timeout_s)
                    self._state = TurnState.COMPLETED
                except subprocess.TimeoutExpired:
                    self._terminate(TurnState.TIMED_OUT)
            self._join_readers()
            return TurnResult(
                state=self._state,
                returncode=self._process.returncode,
                stdout=self._stdout.text(self._redactions),
                stderr=self._stderr.text(self._redactions),
                output_truncated=self._stdout.truncated or self._stderr.truncated,
            )

    def _terminate(self, terminal_state: TurnState) -> None:
        with self._lock:
            if self._state is not TurnState.RUNNING:
                return
            if self._process.poll() is not None:
                self._state = TurnState.COMPLETED
                return
            self._signal_group(signal.SIGTERM)
            try:
                self._process.wait(timeout=self._terminate_grace_s)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                self._process.wait()
            self._state = terminal_state

    def _signal_group(self, sig: signal.Signals) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(self._process.pid), sig)
                return
            except ProcessLookupError:
                return
        if sig is signal.SIGTERM:  # pragma: no cover - exercised on Windows workers
            self._process.terminate()
        else:  # pragma: no cover - exercised on Windows workers
            self._process.kill()

    def _join_readers(self) -> None:
        for thread in self._reader_threads:
            thread.join(timeout=1)
