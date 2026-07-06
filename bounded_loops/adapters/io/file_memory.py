"""
FileMemory — concrete `MemoryPort` adapter.

STATE.md is a CHECKED-IN template (the loop's static context + instructions).
Runtime lap history is appended to a GITIGNORED sibling file
(`.STATE.md.runtime`) — NOT to STATE.md itself — so running a loop never
dirties the tracked template in a user's clone. `load()` returns the template plus
the runtime log concatenated, so the agent still sees prior lap history in
its context window. FileMemory never parses Markdown; it is purely string I/O.
"""

from __future__ import annotations

from pathlib import Path

from bounded_loops.application.ports import ClockPort
from bounded_loops.adapters.io.clock import UtcClock
from bounded_loops.domain.models import LoopContext, Verdict


class FileMemory:
    """
    Implements MemoryPort.
    Reads the STATE.md template + the runtime log (returns "" if both absent).
    Appends one lap-summary block per update() to the gitignored runtime log.
    NEVER writes to the checked-in STATE.md template.
    """

    def __init__(
        self,
        memory_path: Path,
        clock: ClockPort | None = None,
    ) -> None:
        self._template_path: Path = memory_path
        # Gitignored sibling (dot-prefixed, ends in .runtime — see .gitignore).
        self._runtime_path: Path = memory_path.parent / ("." + memory_path.name + ".runtime")
        self._clock: ClockPort = clock or UtcClock()

    def load(self, ctx: LoopContext) -> str:
        """Return the STATE.md template + the runtime lap log, concatenated;
        "" if neither exists."""
        parts: list[str] = []
        if self._template_path.exists():
            parts.append(self._template_path.read_text(encoding="utf-8"))
        if self._runtime_path.exists():
            parts.append(self._runtime_path.read_text(encoding="utf-8"))
        return "\n".join(parts) if parts else ""

    def update(self, ctx: LoopContext, lap: int, verdict: Verdict, decision: str) -> None:
        """Append one lap-summary block to the gitignored runtime log — never
        to the tracked STATE.md template. Creates the runtime file if absent."""
        self._runtime_path.parent.mkdir(parents=True, exist_ok=True)
        ts = self._clock.now_iso()
        verdict_tag = "PASS" if verdict.passed else "FAIL"
        block = (
            f"\n<!-- bl:lap:{lap} ts:{ts} verdict:{verdict_tag} decision:{decision} -->\n"
            f"Gate {'passed' if verdict.passed else 'failed'}: {verdict.detail}\n"
            "---\n"
        )
        with self._runtime_path.open("a", encoding="utf-8") as fh:
            fh.write(block)
            fh.flush()
