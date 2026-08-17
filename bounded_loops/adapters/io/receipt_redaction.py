"""
Pre-write receipt redaction.

A receipt records the gate's command, its exit code and a bounded tail of its
output, because that is what makes a verdict re-derivable by someone who was not
there. Those fields carry absolute filesystem paths — which on most systems embed
an account name — and the output tail of a test-suite gate can contain fragments of
whatever the suite ran against. Right default for an audit record; wrong default for
a jurisdiction with data-residency rules.

Redaction has to happen BEFORE the row is serialised and hashed. The fields are
inside the chain, so redacting one afterwards breaks the property the chain exists
to provide (`ledger_chain`). That single fact fixes the whole design: this module
transforms `LedgerEntry` values on the way in, and `RedactingLedger` is the only
thing that calls it.

Why a decorator and not a call at each site: `run_loop` records in seven places.
Redacting at each would make the guarantee a property of remembering, and a bound
that holds only when every call site remembers is the defect class this project
publishes papers about. One wrapper, applied once at composition, cannot be
bypassed by a site that forgets.

Default is OFF, and OFF is byte-identical to 0.6.6 output. A run that redacts says
so in its receipt: a reader who cannot distinguish a redacted receipt from an
unredacted one has been handed a weaker record while being told nothing changed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from bounded_loops.domain.models import LedgerEntry

PLACEHOLDER_PATH = "<redacted-path>"
PLACEHOLDER_WORKSPACE = "<workspace>"
REDACTION_FIELD = "redaction"

# Absolute POSIX-style paths and Windows drive paths. Deliberately greedy about
# what counts as a path character and deliberately anchored on a separator, so a
# bare word is never mistaken for a path.
_ABS_POSIX = re.compile(r"(?<![\w/])/(?:[\w.@+-]+/)*[\w.@+-]+")
_ABS_WINDOWS = re.compile(r"(?<![\w\\])[A-Za-z]:\\(?:[\w.@+ -]+\\)*[\w.@+ -]+")


class RedactionMode(str, Enum):
    """How much of a receipt to withhold.

    OFF     — record everything. The audit-grade default.
    PATHS   — rewrite absolute paths; keep the output tail. Removes the account
              name without removing the diagnosis.
    STRICT  — PATHS, plus replace the output tail with its SHA-256. The verdict
              stays re-derivable ("this exact output produced this verdict")
              while the content itself is gone.
    """

    OFF = "off"
    PATHS = "paths"
    STRICT = "strict"


@dataclass(frozen=True)
class RedactionPolicy:
    """A declared redaction policy. Immutable, and readable before a run."""

    mode: RedactionMode = RedactionMode.OFF
    workspace_root: Path | None = None

    @property
    def active(self) -> bool:
        return self.mode is not RedactionMode.OFF

    @classmethod
    def from_mode(cls, mode: str, *, workspace_root: Path | None = None) -> RedactionPolicy:
        """Build a policy from a CLI/manifest string, rejecting unknown modes.

        An unrecognised mode is refused rather than silently treated as OFF.
        Falling back to OFF would mean a deployment that asked for redaction, got
        none, and saw no error — declared and not enforced.
        """
        try:
            resolved = RedactionMode(mode)
        except ValueError as exc:
            allowed = ", ".join(m.value for m in RedactionMode)
            raise ValueError(f"unknown redaction mode {mode!r}; expected one of: {allowed}") from exc
        return cls(mode=resolved, workspace_root=workspace_root)


def _redact_text(text: str, policy: RedactionPolicy) -> str:
    """Rewrite absolute paths in one string.

    Paths under the declared workspace root become workspace-relative, because a
    reader debugging a run needs to know *which* file, just not whose machine.
    Every other absolute path is replaced wholesale.

    The two rules fight, which is not obvious and cost a test to find. Rewriting
    the root first leaves `<workspace>/out/report.json`, whose tail is still a
    match for the generic absolute-path pattern — so the naive order erases exactly
    the relative part the first rule exists to preserve. Workspace-rooted paths are
    therefore matched whole, parked behind a slash-free sentinel, and restored after
    the generic pass has run.
    """
    if not text:
        return text
    parked: list[str] = []

    def _park(value: str) -> str:
        parked.append(value)
        return f"\x00{len(parked) - 1}\x00"

    result = text
    root = policy.workspace_root
    if root is not None:
        # Longest-first so a nested root does not shadow its parent.
        for candidate in sorted({str(root), str(root.resolve())}, key=len, reverse=True):
            whole = re.compile(re.escape(candidate) + r"(?:[/\\][\w.@+-]+)*")
            result = whole.sub(
                lambda m, n=len(candidate): _park(PLACEHOLDER_WORKSPACE + m.group(0)[n:]),
                result,
            )

    result = _ABS_POSIX.sub(PLACEHOLDER_PATH, result)
    result = _ABS_WINDOWS.sub(PLACEHOLDER_PATH, result)

    for index, value in enumerate(parked):
        result = result.replace(f"\x00{index}\x00", value)
    return result


def _redact_value(value: object, policy: RedactionPolicy, *, key: str = "") -> object:
    """Walk an evidence bag, returning new containers rather than mutating."""
    if isinstance(value, Mapping):
        return {k: _redact_value(v, policy, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        rebuilt = [_redact_value(v, policy) for v in value]
        return tuple(rebuilt) if isinstance(value, tuple) else rebuilt
    if isinstance(value, str):
        if policy.mode is RedactionMode.STRICT and _is_output_tail(key):
            return _digest_of(value)
        return _redact_text(value, policy)
    return value


def _is_output_tail(key: str) -> bool:
    """Which evidence keys carry captured process output.

    Kept as a named predicate because the gate adapters spell it several ways
    (`tail`, `stdout_tail`, `output_tail`) and a missed spelling is a leak.
    """
    return key in {"tail", "stdout_tail", "stderr_tail", "output_tail", "combined_output"}


def _digest_of(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def redact_entry(entry: LedgerEntry, policy: RedactionPolicy) -> LedgerEntry:
    """Return a new entry with the policy applied. Never mutates the input.

    OFF returns the entry unchanged and unwrapped, so the default path costs
    nothing and produces the same bytes 0.6.6 produced.
    """
    if not policy.active:
        return entry
    verdict = entry.verdict
    redacted_verdict = replace(
        verdict,
        detail=_redact_text(verdict.detail, policy),
        evidence=_redact_value(verdict.evidence, policy),  # type: ignore[arg-type]
    )
    return replace(
        entry,
        verdict=redacted_verdict,
        handoff=_redact_text(str(entry.handoff or ""), policy),
    )


class RedactingLedger:
    """A `LedgerPort` that redacts every entry before delegating.

    Wraps rather than subclasses, so it composes with any ledger implementation
    and adds nothing to the ones that do not need it.
    """

    def __init__(self, inner: object, policy: RedactionPolicy) -> None:
        self._inner = inner
        self._policy = policy

    @property
    def policy(self) -> RedactionPolicy:
        return self._policy

    def record(self, entry: LedgerEntry) -> None:
        self._inner.record(redact_entry(entry, self._policy))  # type: ignore[attr-defined]

    def head(self) -> str:
        return self._inner.head()  # type: ignore[attr-defined,no-any-return]

    def path(self) -> Path:
        return self._inner.path()  # type: ignore[attr-defined,no-any-return]


def wrap_if_active(inner: object, policy: RedactionPolicy) -> object:
    """Compose the decorator only when it would do something."""
    return RedactingLedger(inner, policy) if policy.active else inner
