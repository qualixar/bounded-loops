"""
Tests for domain/models.py (frozen contracts).
Covers: immutability, equality, Enum behaviour, factory defaults.
"""
import pytest
from pathlib import Path
from bounded_loops.domain.models import (
    Rung, Status, Spec, Bounds, Verdict, RunResult,
    LoopContext, LedgerEntry, Outcome,
)


# ---- Rung ----------------------------------------------------------------

class TestRung:
    def test_values_are_strings(self):
        assert Rung.L1.value == "L1"
        assert Rung.L2.value == "L2"
        assert Rung.L3.value == "L3"

    def test_rung_is_str_subclass(self):
        # (str, Enum) → Rung.L1 == "L1"
        assert Rung.L1 == "L1"
        assert Rung.L2 == "L2"

    def test_construct_from_string(self):
        assert Rung("L1") is Rung.L1
        assert Rung("L3") is Rung.L3

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            Rung("L4")

    def test_all_three_members_exist(self):
        members = {r.value for r in Rung}
        assert members == {"L1", "L2", "L3"}


# ---- Status --------------------------------------------------------------

class TestStatus:
    def test_all_four_members(self):
        values = {s.value for s in Status}
        assert values == {"DONE", "HALT", "PAUSE", "KILLED"}

    def test_is_str_subclass(self):
        assert Status.DONE == "DONE"

    def test_construct_from_string(self):
        assert Status("HALT") is Status.HALT


# ---- Spec ----------------------------------------------------------------

class TestSpec:
    def _make(self, **kwargs):
        defaults = dict(
            name="bug-fix",
            goal="Fix the failing test.",
            steps=("Read.", "Patch.", "Run."),
            stop_condition="pytest exits 0",
        )
        defaults.update(kwargs)
        return Spec(**defaults)

    def test_basic_construction(self):
        s = self._make()
        assert s.name == "bug-fix"
        assert s.steps == ("Read.", "Patch.", "Run.")

    def test_frozen_name_raises_on_mutation(self):
        s = self._make()
        with pytest.raises((AttributeError, TypeError)):
            s.name = "changed"  # type: ignore

    def test_frozen_steps_tuple_is_immutable(self):
        s = self._make()
        with pytest.raises(TypeError):
            s.steps[0] = "x"  # type: ignore

    def test_forbid_default_is_empty_tuple(self):
        s = self._make()
        assert s.forbid == ()

    def test_forbid_explicit(self):
        s = self._make(forbid=("edit tests/",))
        assert "edit tests/" in s.forbid

    def test_equality_same_values(self):
        a = self._make()
        b = self._make()
        assert a == b

    def test_equality_different_values(self):
        a = self._make(name="a")
        b = self._make(name="b")
        assert a != b

    def test_hashable(self):
        # frozen dataclasses are hashable
        s = self._make()
        _ = {s: "ok"}

    def test_steps_order_matters_for_equality(self):
        a = self._make(steps=("A", "B"))
        b = self._make(steps=("B", "A"))
        assert a != b


# ---- Bounds --------------------------------------------------------------

class TestBounds:
    def _make(self, **kwargs):
        defaults = dict(max_iterations=5)
        defaults.update(kwargs)
        return Bounds(**defaults)

    def test_required_field(self):
        b = self._make()
        assert b.max_iterations == 5

    def test_defaults(self):
        b = self._make()
        assert b.no_progress_window == 3
        assert b.max_tokens is None
        assert b.max_wallclock_s is None
        assert b.sandbox is True
        assert b.quarantine_inputs is True
        assert b.schema is None
        assert b.trace is True
        assert b.require_approval is None

    def test_all_fields_explicit(self):
        b = Bounds(
            max_iterations=10,
            no_progress_window=2,
            max_tokens=50_000,
            max_wallclock_s=300,
            sandbox=False,
            quarantine_inputs=False,
            schema="seed/schema.json",
            trace=False,
            require_approval=True,
        )
        assert b.max_iterations == 10
        assert b.require_approval is True

    def test_frozen_raises_on_mutation(self):
        b = self._make()
        with pytest.raises((AttributeError, TypeError)):
            b.max_iterations = 99  # type: ignore

    def test_equality(self):
        a = self._make(max_iterations=5)
        b = self._make(max_iterations=5)
        assert a == b

    def test_inequality_on_one_field(self):
        a = self._make(max_iterations=5)
        b = self._make(max_iterations=6)
        assert a != b

    def test_require_approval_none_is_falsy(self):
        b = self._make()
        assert b.require_approval is None


# ---- Verdict -------------------------------------------------------------

class TestVerdict:
    def test_passed_true(self):
        v = Verdict(passed=True, detail="all good")
        assert v.passed is True

    def test_passed_false_not_exception(self):
        # A gate FAIL is a Verdict, never an exception
        v = Verdict(passed=False, detail="1 failed")
        assert v.passed is False

    def test_default_evidence_is_empty_dict(self):
        v = Verdict(passed=True, detail="ok")
        assert v.evidence == {}

    def test_evidence_per_instance_not_shared(self):
        # Each instance must get its own dict — not a shared default
        v1 = Verdict(passed=True, detail="ok")
        v2 = Verdict(passed=True, detail="ok")
        assert v1.evidence is not v2.evidence

    def test_evidence_key_set(self):
        v = Verdict(passed=True, detail="ok", evidence={"tests_passed": 42})
        assert v.evidence["tests_passed"] == 42

    def test_frozen_passed_field_immutable(self):
        v = Verdict(passed=True, detail="ok")
        with pytest.raises((AttributeError, TypeError)):
            v.passed = False  # type: ignore

    def test_equality_on_content(self):
        a = Verdict(passed=True, detail="ok", evidence={"k": 1})
        b = Verdict(passed=True, detail="ok", evidence={"k": 1})
        assert a == b

    def test_inequality_detail_differs(self):
        a = Verdict(passed=True, detail="ok")
        b = Verdict(passed=True, detail="different")
        assert a != b


# ---- RunResult -----------------------------------------------------------

class TestRunResult:
    def test_required_fields(self):
        r = RunResult(changed=False, agent_claimed_done=False)
        assert r.changed is False
        assert r.agent_claimed_done is False

    def test_defaults(self):
        r = RunResult(changed=True, agent_claimed_done=True)
        assert r.tokens == 0
        assert r.log == ""

    def test_agent_claimed_done_advisory_only(self):
        # This test documents the semantic: even claimed_done=True does not
        # mean the loop exits — the gate verdict governs that.
        r = RunResult(changed=False, agent_claimed_done=True)
        assert r.agent_claimed_done is True
        # The loop should NOT exit based on this alone — that's a rules concern.

    def test_frozen(self):
        r = RunResult(changed=True, agent_claimed_done=False, tokens=100)
        with pytest.raises((AttributeError, TypeError)):
            r.tokens = 9999  # type: ignore

    def test_equality(self):
        a = RunResult(changed=True, agent_claimed_done=False, tokens=100, log="x")
        b = RunResult(changed=True, agent_claimed_done=False, tokens=100, log="x")
        assert a == b


# ---- LoopContext ---------------------------------------------------------

class TestLoopContext:
    def _make(self, **kwargs):
        defaults = dict(
            workspace=Path("/tmp/loop"),
            lap=1,
            rung=Rung.L2,
            trace_id="run-abc123",
        )
        defaults.update(kwargs)
        return LoopContext(**defaults)

    def test_construction(self):
        ctx = self._make()
        assert ctx.lap == 1
        assert ctx.rung is Rung.L2

    def test_default_env_empty_dict(self):
        ctx = self._make()
        assert ctx.env == {}

    def test_env_per_instance_not_shared(self):
        a = self._make()
        b = self._make()
        assert a.env is not b.env

    def test_frozen(self):
        ctx = self._make()
        with pytest.raises((AttributeError, TypeError)):
            ctx.lap = 99  # type: ignore

    def test_new_lap_requires_new_instance(self):
        # Because LoopContext is frozen, "update lap" = new object.
        # Test documents the pattern used by RunLoopUseCase.
        ctx0 = self._make(lap=0)
        ctx1 = LoopContext(
            workspace=ctx0.workspace,
            lap=1,
            rung=ctx0.rung,
            trace_id=ctx0.trace_id,
            env=ctx0.env,
        )
        assert ctx1.lap == 1
        assert ctx1.workspace == ctx0.workspace

    def test_workspace_is_path(self):
        ctx = self._make(workspace=Path("/some/dir"))
        assert isinstance(ctx.workspace, Path)


# ---- LedgerEntry ---------------------------------------------------------

class TestLedgerEntry:
    def _make(self, **kwargs):
        defaults = dict(
            lap=1,
            ts="2026-07-04T09:00:00Z",
            verdict=Verdict(passed=True, detail="ok"),
            decision="done",
            budget_spent={"laps": 1, "tokens": 500, "wallclock_s": 5},
        )
        defaults.update(kwargs)
        return LedgerEntry(**defaults)

    def test_construction(self):
        e = self._make()
        assert e.lap == 1
        assert e.decision == "done"

    def test_ts_is_string(self):
        # Timestamp must be a plain string (not datetime object) — injected
        # by ClockPort.now_iso() at application layer.
        e = self._make()
        assert isinstance(e.ts, str)
        assert "2026" in e.ts

    def test_frozen(self):
        e = self._make()
        with pytest.raises((AttributeError, TypeError)):
            e.decision = "continue"  # type: ignore

    def test_decision_continue(self):
        e = self._make(decision="continue")
        assert e.decision == "continue"

    def test_decision_halt_is_plain_value(self):
        # RESOLVED: decision is the closed literal "halt" — the reason
        # ("no-progress", "max_iterations reached", ...) lives in
        # Outcome.reason, never encoded into LedgerEntry.decision.
        e = self._make(decision="halt")
        assert e.decision == "halt"

    def test_decision_rejects_unknown_value_at_type_check_time(self):
        # Literal is a static-typing construct — mypy/pyright reject an
        # unknown value at type-check time; this test documents the
        # closed set for runtime readers (Literal does not raise at
        # construction in plain CPython, only under a type checker).
        allowed = {"continue", "done", "halt", "pause", "killed"}
        e = self._make(decision="done")
        assert e.decision in allowed

    def test_decision_pause(self):
        e = self._make(decision="pause")
        assert e.decision == "pause"

    def test_decision_killed(self):
        e = self._make(decision="killed")
        assert e.decision == "killed"

    def test_budget_spent_structure(self):
        e = self._make(budget_spent={"laps": 3, "tokens": 4200, "wallclock_s": 18})
        assert e.budget_spent["laps"] == 3
        assert e.budget_spent["tokens"] == 4200


# ---- Outcome -------------------------------------------------------------

class TestOutcome:
    def _make(self, **kwargs):
        defaults = dict(
            status=Status.DONE,
            reason="gate-passed",
            laps=3,
            ledger_path=Path("/tmp/loop/.ledger.jsonl"),
        )
        defaults.update(kwargs)
        return Outcome(**defaults)

    def test_done_outcome(self):
        o = self._make()
        assert o.status is Status.DONE
        assert o.laps == 3

    def test_halt_outcome(self):
        o = self._make(status=Status.HALT, reason="no-progress")
        assert o.status is Status.HALT

    def test_pause_outcome(self):
        o = self._make(status=Status.PAUSE, reason="awaiting-approval")
        assert o.status is Status.PAUSE

    def test_killed_outcome(self):
        o = self._make(status=Status.KILLED, reason="killed")
        assert o.status is Status.KILLED

    def test_frozen(self):
        o = self._make()
        with pytest.raises((AttributeError, TypeError)):
            o.laps = 99  # type: ignore

    def test_ledger_path_is_path(self):
        o = self._make()
        assert isinstance(o.ledger_path, Path)

    def test_exit_code_convention_documented(self):
        # DONE → exit 0; anything else → exit 1.
        # Enforced by cli.py, but the mapping is: status == DONE.
        done = self._make(status=Status.DONE)
        halt = self._make(status=Status.HALT, reason="x")
        assert done.status == Status.DONE
        assert halt.status != Status.DONE
