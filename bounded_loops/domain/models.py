"""
Pure domain data types for bounded-loops.

Rules:
  - stdlib imports ONLY (dataclasses, enum, pathlib, typing).
  - No I/O, no framework, no side-effects.
  - All dataclasses are frozen=True → TypeError on any attribute mutation.
  - Timestamps (ts in LedgerEntry) are ISO-8601 strings produced by
    ClockPort.now_iso() at the application layer — never datetime.now() here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Optional


def _freeze_value(value: object) -> object:
    """Detach and recursively freeze serializable domain metadata.

    Frozen dataclasses protect attributes only. Evidence, execution environment,
    and budget snapshots also cross runner and ledger boundaries, so retaining a
    caller-owned mapping would let later mutation rewrite already-recorded facts.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(child) for child in value)
    return value


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Rung(str, Enum):
    """
    Safety rung on the L1→L2→L3 ladder.

      L1 = "report"     — human reads every verdict; no autonomous action.
      L2 = "assisted"   — agent acts but pauses for human approval at gates.
      L3 = "unattended" — agent acts; approval derived from bounds only.

    Being a (str, Enum) subclass means:
      Rung.L1 == "L1"   → True
      str(Rung.L1)       → "Rung.L1"   (use Rung.L1.value for the bare string)
      Rung("L1")         → Rung.L1     (construction from string works)

    This allows round-trip serialisation from loop.yaml without a manual map.
    """
    L1 = "L1"   # report   | human reviews every lap
    L2 = "L2"   # assisted | pauses for approval on pass
    L3 = "L3"   # unattended | autonomous (approval derived from bounds)


class Status(str, Enum):
    """
    Terminal status of a RunLoopUseCase.run() call.

      DONE   — gate passed AND (approval not required OR approval granted).
      HALT   — a safety bound was tripped (budget/cap/no-progress).
      PAUSE  — gate passed but approval is required and not yet granted.
      KILLED — external kill switch tripped between laps.
      ERROR  — runner/gate execution failed before a verdict could complete.
    """
    DONE   = "DONE"
    HALT   = "HALT"
    PAUSE  = "PAUSE"
    KILLED = "KILLED"
    ERROR  = "ERROR"


class TurnState(str, Enum):
    """Controller-visible lifecycle state for one external runner turn."""

    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class UsageState(str, Enum):
    """Whether usage is trustworthy enough for a future budget policy."""

    MEASURED = "measured"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Frozen dataclasses — field order matches  exactly
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """
    Immutable loop specification loaded from PROMPT.md + loop.yaml.

    Fields:
      name             — unique kebab-case identifier (== folder name).
      goal             — one-sentence goal for the loop.
      steps            — ordered per-turn instructions; tuple preserves
                         order and immutability (list would be mutable).
      stop_condition   — human-readable description of the exit criterion;
                         the gate *proves* it mechanically — this field is
                         for display/audit only, not evaluated as code.
      forbid           — tuple of forbidden actions (e.g. "edit test files").
                         Empty by default.

    Example:
      Spec(
          name="bug-fix-red-green",
          goal="Fix the failing test without modifying the test file.",
          steps=("Read the failing test.", "Patch the source file.",
                 "Run pytest."),
          stop_condition="pytest exits 0",
          forbid=("edit tests/",),
      )
    """
    name:            str
    goal:            str
    steps:           tuple[str, ...]
    stop_condition:  str
    forbid:          tuple[str, ...] = ()


@dataclass(frozen=True)
class Bounds:
    """
    Immutable loop bounds loaded from bounds.yaml.

    Maps 1:1 to the bounds.yaml schema; defaults match the YAML defaults.

    Fields:
      max_iterations      — hard lap cap (bound #1). Required.
      no_progress_window  — consecutive no-change laps before HALT (bound #6?).
                            Default 3.
      max_tokens          — cumulative token budget, or None (bound #7?).
      max_wallclock_s     — wall-clock timeout in seconds, or None.
      sandbox             — agent runs in a sandbox (bound #2). Default True.
      quarantine_inputs   — bound #3, the "governed workspace" guarantee.
                            When True (default), secret-bearing paths (.env*,
                            .ssh, .aws, *.pem/*.key, id_rsa, credentials, .git,
                            …) are EXCLUDED from the sandbox copy so a shared/
                            community loop's seed can neither smuggle
                            credentials to the agent nor exfiltrate them
                            (enforced in composition._make_scratch_workspace).
                            Set False only for a loop that legitimately needs
                            such a file in its sandbox (e.g. a secret-scanning
                            demo). Default True.
      schema              — path string to a JSON Schema file for output
                            validation (bound #4), or None.
      trace               — emit OTel spans per lap (bound #5). Default True.
      require_approval    — bound #8; None means "derive from rung":
                              L1 → False, L2/L3 → True.

    RESOLVED: the nine bounds
    are enforced ACROSS layers, not all as Bounds fields. Only bounds that
    are simple booleans/limits live here:
      #1 lap/cost cap    → max_iterations (+ BudgetMeter)
      #2 sandbox         → sandbox (+ workspace isolation, composition.py)
      #3 quarantine      → quarantine_inputs
      #4 schema          → schema
      #5 tracing         → trace (+ TracerPort)
      #7 token budget     → max_tokens (+ BudgetMeter)
      #8 approval        → require_approval (+ ApprovalPort)
      #9 wallclock cap    → max_wallclock_s (+ BudgetMeter)
    Bound #6 (regression-eval) lives in the GATE choice, not in Bounds — it
    is satisfied by which GatePort adapter a loop selects (promptfoo/
    great_expectations/etc.), not a Bounds field. This mapping table also
    ships in the repo README's "Nine bounds" section (kept in one place,
    not duplicated into a separate docs/ file).

    Example:
      Bounds(
          max_iterations=10,
          no_progress_window=3,
          max_tokens=50_000,
          max_wallclock_s=300,
          sandbox=True,
          quarantine_inputs=True,
          schema="seed/output-schema.json",
          trace=True,
          require_approval=None,   # → derived: L1=False, L2/L3=True
      )
    """
    max_iterations:     int
    no_progress_window: int           = 3
    max_tokens:         Optional[int] = None
    max_wallclock_s:    Optional[int] = None
    sandbox:            bool          = True
    quarantine_inputs:  bool          = True
    schema:             Optional[str] = None
    trace:              bool          = True
    require_approval:   Optional[bool]= None
    #: Seconds RESERVED OUT OF `max_wallclock_s` for one final wind-down turn when a bound stops the
    #: run — the agent's chance to write down what it did, what is left, and what it would do next.
    #:
    #: Reserved, not added. A bound that grants a bonus turn on expiry does not bound anything an
    #: operator can read off the manifest: the ceiling would silently mean "max_wallclock_s plus
    #: however long the summary takes". So the work budget is `max_wallclock_s - handoff_reserve_s`
    #: and the total is unchanged, which is what keeps every termination guarantee intact.
    #:
    #: Set to 0 to decline the wind-down. Its purpose: without it, a run that hits its ceiling
    #: mid-task discards everything the attempt had worked out, and the next run starts from the same
    #: seed with the same budget and no knowledge of what the last one learned — so a task needing
    #: more than one budget window can never finish, however many times it is run.
    handoff_reserve_s:  int           = 90


@dataclass(frozen=True)
class Verdict:
    """
    Gate result for a single lap.

    Fields:
      passed   — True iff the gate mechanically confirmed the stop_condition.
      detail   — human-readable gate summary (required, non-empty).
      evidence — arbitrary key→value bag for structured gate output
                 (stdout tail, schema diff, test counts, etc.).
                 Default: empty dict (produced by field(default_factory=dict)
                 so each instance gets its own dict, not a shared default).

    IMPORTANT: Verdict.passed=True is a NECESSARY but NOT SUFFICIENT
    condition for loop exit. The rules layer additionally checks
    stop_condition_met(spec, verdict) before deciding done.

    Mutable-default-factory note: dataclass(frozen=True) allows
    field(default_factory=dict) — the factory is called at __init__ time,
    so each Verdict gets its own evidence dict. After construction the
    field is frozen (reassignment raises TypeError), but the dict itself
    is mutable. This is an acknowledged trade-off: a truly deep-immutable
    evidence would require a frozendict or tuple-of-pairs, but  specifies
    `dict` without qualification. This follows that exactly and flags
    this for review.

    Example (gate passed):
      Verdict(passed=True, detail="pytest: 42 passed in 0.8s",
              evidence={"tests_passed": 42, "duration_s": 0.8})

    Example (gate failed — NOT an exception):
      Verdict(passed=False, detail="pytest: 1 failed",
              evidence={"tail": "FAILED tests/test_foo.py::test_bar"})
    """
    passed:   bool
    detail:   str
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze_value(self.evidence))


@dataclass(frozen=True)
class RunResult:
    """
    What the runner reports back after a single agent turn.

    Fields:
      changed             — True iff the agent modified the workspace this lap
                            (detected by the runner via e.g. git diff HEAD).
      agent_claimed_done  — the agent's OWN "I'm done" claim. Advisory only;
                            NEVER used directly as the loop-exit criterion.
                            The gate verdict governs exit, not this field.
      tokens              — tokens consumed this lap (0 if unknown/stub).
      log                 — agent stdout/stderr for this lap, truncated by
                            the runner as appropriate (empty string default).

    Example (stub runner, no change):
      RunResult(changed=False, agent_claimed_done=False, tokens=0, log="")

    Example (real runner, partial progress):
      RunResult(changed=True, agent_claimed_done=False, tokens=1240,
                log="Patched src/calculator.py, line 17.")
    """
    changed:            bool
    agent_claimed_done: bool
    tokens:             int  = 0
    log:                str  = ""


@dataclass(frozen=True)
class TurnRequest:
    """A V2 request to execute exactly one bounded loop turn."""

    spec: Spec
    context: LoopContext


@dataclass(frozen=True)
class TurnResult:
    """Bounded, redacted terminal evidence emitted by a running turn."""

    state: TurnState
    returncode: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    usage_state: UsageState = UsageState.UNKNOWN


@dataclass(frozen=True)
class WallclockBudget:
    """
    What is left of `Bounds.max_wallclock_s` at the moment an attempt starts.

    Exists because the declared ceiling bounds the RUN and the thing that has to
    respect it is a single ATTEMPT. Handing every attempt the whole ceiling would
    make the run's worst case (laps x ceiling), so the number in the manifest
    would bound nothing a reader cares about. Only the controller knows how much
    is left, so the controller measures and the runner is told.

    `declared_s` travels alongside `remaining_s` for one reason: when the ceiling
    is reached, the failure has to name the bound the operator actually wrote, not
    the residue the runner happened to be handed.

    Fields:
      declared_s  — `bounds.max_wallclock_s` verbatim, for the halt reason.
      remaining_s — seconds still unspent when the controller handed off. Never
                    negative: the controller's pre-turn budget check already
                    refuses to start an attempt once the ceiling is reached.
    """
    declared_s:  int
    remaining_s: float

    def __post_init__(self) -> None:
        if self.remaining_s < 0:
            raise ValueError(
                f"remaining_s must be >= 0, got {self.remaining_s}; a negative remainder means "
                "the controller started an attempt after the ceiling was already reached"
            )


@dataclass(frozen=True)
class LoopContext:
    """
    Per-lap context passed to every port (runner, gate, memory, etc.).

    Fields:
      workspace  — absolute path to the loop's working directory.
      lap        — current lap number (1-based; 0 = pre-loop init).
      rung       — the loop's safety rung (L1/L2/L3).
      trace_id   — unique string identifier for the OTel trace of this run.
      env        — arbitrary key→value bag for adapter configuration
                   (e.g. {"gate_timeout_s": 30}). Mutable-dict same caveat
                   as Verdict.evidence applies.
      wallclock  — the declared wallclock ceiling and what is left of it, or None
                   when the loop declares no ceiling. Runners clamp their own
                   wait to it; see adapters/runners/attempt_deadline.py.

    Example:
      LoopContext(
          workspace=Path("/home/user/loops/bug-fix-red-green"),
          lap=1,
          rung=Rung.L2,
          trace_id="run-2026-07-04T09:00:00Z-abc123",
          env={"gate_timeout_s": 30},
      )

    NOTE: The RunLoopUseCase builds ctx0 at lap=0 for the initial
    memory.load() call, then rebuilds with lap=N for each actual lap.
    Because LoopContext is frozen, "update with new lap" means constructing
    a new instance — see  flow pseudocode in HLD.
    """
    workspace: Path
    lap:       int
    rung:      Rung
    trace_id:  str
    env:       dict = field(default_factory=dict)
    memory_snapshot: str = ""
    wallclock: Optional[WallclockBudget] = None
    #: Replaces the loop's own prompt for this turn. Set only by the controller, only for the
    #: wind-down turn: an agent handed its original goal again keeps working instead of summarising.
    prompt_override: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", _freeze_value(self.env))


@dataclass(frozen=True)
class LedgerEntry:
    """
    One append-only row in the verdict ledger.

    Fields:
      lap           — lap number this entry records.
      ts            — ISO-8601 timestamp string. MUST be injected via
                      ClockPort.now_iso() at the application layer; domain
                      never calls datetime.now().
      verdict       — the gate Verdict for this lap.
      decision      — one of exactly five canonical values:
                        "continue"  → loop proceeds to next lap
                        "done"      → stop_condition met + approved
                        "halt"      → a bound was tripped (the WHY lives in
                                      Outcome.reason, NOT encoded in decision)
                        "pause"     → approval required, not yet granted
                        "killed"    → kill switch tripped
                        "error"     → runner/gate execution failed
      budget_spent  — point-in-time snapshot of consumed budget, e.g.:
                        {"laps": 3, "tokens": 4200, "wallclock_s": 18}

    RESOLVED: `decision` is typed `Literal["continue", "done", "halt",
    "pause", "killed", "error"]`, not a bare `str` and NOT `"halt:<reason>"` — the
    colon-suffix pattern used in an earlier draft is dropped. The halt
    reason (e.g. "no-progress", "max_iterations reached") is carried
    exclusively in `Outcome.reason`; `LedgerEntry.decision` never encodes it.
    This keeps `decision` a closed, type-checkable enum-like value.

    Example:
      LedgerEntry(
          lap=2,
          ts="2026-07-04T09:00:18Z",
          verdict=Verdict(passed=True, detail="pytest: 42 passed"),
          decision="done",
          budget_spent={"laps": 2, "tokens": 2400, "wallclock_s": 18},
      )
    """
    lap:          int
    ts:           str      # ISO-8601, injected via ClockPort — never datetime.now()
    verdict:      Verdict
    decision:     Literal["continue", "done", "halt", "pause", "killed", "error"]
    budget_spent: dict
    #: Whether the worker was actually invoked on this lap. False only for the checks the
    #: controller performs BEFORE the turn — the kill switch and the budget ceiling — which
    #: record a lap that spent no attempt.
    #:
    #: Without this, a lap count and an attempt count are indistinguishable from the receipt:
    #: a ceiling halt at `max_iterations: 10` writes an eleventh row, and anyone computing
    #: consumed/declared from the ledger gets 1.1 for a bound that in fact held exactly. A cost
    #: claim that cannot be audited from its own receipt is not a cost claim.
    #:
    #: Scope: a WORK attempt. The wind-down turn on a bound halt is not one, and is recorded by
    #: `handoff` below — so `attempted: false` with a non-empty `handoff` reads exactly right: no
    #: work was attempted on this lap, and a handoff turn ran.
    attempted:    bool = True
    #: Filename of the handoff document written when a bound stopped the run, alongside the ledger,
    #: or empty when none was written. Present so the receipt records that a run was wound down
    #: rather than merely cut off, and so a reader can find the evidence without knowing a convention.
    handoff:      str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "budget_spent", _freeze_value(self.budget_spent))


@dataclass(frozen=True)
class Outcome:
    """
    The final result returned by RunLoopUseCase.run().

    Fields:
      status      — terminal status (DONE/HALT/PAUSE/KILLED).
      reason      — human-readable explanation (e.g. "gate-passed",
                    "no-progress", "awaiting-approval", "killed").
      laps        — total laps executed when the loop terminated.
      ledger_path — absolute path to the JSON-Lines ledger file for this run.
      ledger_head — digest of the ledger's last line at the moment the run ended.

    `ledger_head` is reported rather than merely stored because the hash chain in
    the ledger has no secret in it: anyone who can rewrite the whole file can
    recompute every link. What that adversary cannot do is change a value already
    copied somewhere they do not control, so the head is surfaced on stdout and in
    the JSON outcome, making an operator's terminal and a CI log into witnesses. A
    chain with no witness is a weaker claim than it looks, and the field exists to
    stop us making the stronger one by accident.

    Exit-code mapping:
      status == DONE  → exit 0
      anything else   → exit 1  (enforced by cli.py, not here)

    Example:
      Outcome(
          status=Status.DONE,
          reason="gate-passed",
          laps=3,
          ledger_path=Path("/home/user/loops/bug-fix-red-green/.ledger.jsonl"),
      )
    """
    status:      Status
    reason:      str
    laps:        int
    ledger_path: Path
    #: Defaulted so every existing construction site keeps working, and so a caller
    #: that never chained anything reports an empty string rather than a plausible
    #: digest it did not earn.
    ledger_head: str = ""
