"""Third-party gate plugins — the four rules, and the wrapper that constrains a plugin's verdict.

WHY THIS FILE IS SHAPED THIS WAY. A previous attempt at this feature shipped 37 passing tests and
five defects, because every test exercised the loader module against itself. A test that imports the
module IS the caller you are missing, so it cannot detect that nothing else calls it. So the
integration tests here deliberately go through ``composition`` — ``merged_gate_registry`` and
``_instantiate_gate`` — rather than calling the loader directly, and there is a separate
``test_plugins_live.py`` that installs a real distribution.

Entry points are constructed rather than installed so these run in-suite with no venv mutation. The
one thing that cannot prove — that a genuinely installed distribution is discovered — is what the
live file covers.
"""
from __future__ import annotations

import logging
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Mapping

import pytest

from bounded_loops.adapters.gates import plugins as gp
from bounded_loops.adapters.gates.plugins import (
    GatePluginRefused,
    GuardedGate,
    _load_one,
    load_gate_plugins,
    merged_gate_registry,
)
from bounded_loops.application.ports import GatePort
from bounded_loops.domain.models import LoopContext, Rung, Verdict


def _ctx(workspace: Path) -> LoopContext:
    return LoopContext(workspace=workspace, lap=1, rung=Rung.L1, trace_id="t-plugins", env={})


class _MarkerGate:
    """A well-behaved third-party gate: a real mechanical check on the workspace."""

    def __init__(self, marker: str = "DONE.txt") -> None:
        self.marker = marker

    def check(self, ctx: LoopContext) -> Verdict:
        found = (ctx.workspace / self.marker).is_file()
        return Verdict(
            passed=found,
            detail=f"marker {self.marker!r} {'found' if found else 'absent'}",
            evidence={"marker": self.marker},
        )


def _entry(name: str, factory: object) -> EntryPoint:
    """An EntryPoint whose `load()` returns `factory`, without installing anything."""
    ep = EntryPoint(name=name, value=f"tests.fake:{name}", group=gp.GATE_ENTRY_POINT_GROUP)
    object.__setattr__(ep, "load", lambda: factory)
    return ep


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, entries: list[EntryPoint]) -> None:
    monkeypatch.setattr(gp, "entry_points", lambda group: entries if group else [])


# ── the shape a gate class must have (refused at LOAD, not mid-run) ─────────────────────────────

def test_a_non_class_is_refused() -> None:
    with pytest.raises(GatePluginRefused, match="not a class"):
        _load_one(_entry("p", lambda: {"k": "not-a-class"}), shipped=frozenset())


def test_a_class_without_check_is_refused() -> None:
    with pytest.raises(GatePluginRefused, match="no callable `check`"):
        _load_one(_entry("p", lambda: {"k": type("NoCheck", (), {})}), shipped=frozenset())


def test_a_check_with_the_wrong_arity_is_refused_at_load_not_mid_run() -> None:
    """`isinstance(obj, GatePort)` only checks that `check` EXISTS, so arity is inspected here.

    Without this, a gate whose check takes two required arguments loads fine and raises on the first
    lap of a real run, which is the worst place to discover it.
    """
    wrong = type("Wrong", (), {"check": lambda self, ctx, extra: None})
    with pytest.raises(GatePluginRefused, match="2 required parameters"):
        _load_one(_entry("p", lambda: {"k": wrong}), shipped=frozenset())


@pytest.mark.parametrize("bad_kind", ["", "Upper", "1leading", "trailing-", "-leading",
                                     "double--hyphen", "has_underscore", "x" * 41])
def test_a_malformed_gate_kind_is_refused(bad_kind: str) -> None:
    """The first pattern accepted a TRAILING hyphen; parametrised so one narrow fix cannot pass."""
    with pytest.raises(GatePluginRefused):
        _load_one(_entry("p", lambda: {bad_kind: _MarkerGate}), shipped=frozenset())


@pytest.mark.parametrize("good_kind", ["demo", "demo-gate", "a", "a1-b2-c3", "x" * 40])
def test_a_well_formed_kind_is_accepted_so_the_pattern_is_not_refusing_everything(
    good_kind: str,
) -> None:
    """Calibration: a refusal test alone would pass against a pattern that matches nothing."""
    accepted = _load_one(_entry("p", lambda: {good_kind: _MarkerGate}), shipped=frozenset())
    assert dict(accepted) == {good_kind: _MarkerGate}


# ── rule 3: a plugin cannot claim a shipped kind. rule 2: all-or-nothing. ───────────────────────

def test_claiming_a_shipped_kind_is_refused_and_takes_the_whole_plugin_with_it() -> None:
    offered = {"fine-kind": _MarkerGate, "pytest": _MarkerGate}
    with pytest.raises(GatePluginRefused, match="shipped gate kind 'pytest'"):
        _load_one(_entry("hostile", lambda: offered), shipped=frozenset({"pytest"}))


def test_shipped_wins_structurally_even_if_the_name_check_were_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 3 is enforced TWICE. This proves the second mechanism independently of the first.

    `load_gate_plugins` is stubbed to return a kind that collides with a shipped one — i.e. the
    loader's own check is bypassed. `merged_gate_registry` must still hand back the shipped class,
    because it layers plugins UNDER shipped rather than over them.
    """
    class _Hijack:
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed=True, detail="hijacked", evidence={})

    monkeypatch.setattr(gp, "load_gate_plugins", lambda **kw: {"pytest": _Hijack})
    registry, _ = merged_gate_registry({"pytest": _MarkerGate})
    assert registry["pytest"] is _MarkerGate


# ── rule 1: a broken plugin is skipped, never fatal ─────────────────────────────────────────────

def test_a_plugin_that_raises_on_load_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def _explode() -> Mapping[str, type]:
        raise RuntimeError("plugin is broken")

    _patch_entry_points(monkeypatch, [
        _entry("broken", _explode), _entry("good", lambda: {"good-kind": _MarkerGate}),
    ])
    with caplog.at_level(logging.WARNING):
        loaded = load_gate_plugins(shipped=frozenset())
    assert dict(loaded) == {"good-kind": _MarkerGate}, "a broken plugin must not block a good one"
    assert "broken" in caplog.text


def test_a_plugin_calling_sys_exit_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """`except Exception` does NOT catch SystemExit. That exact hole was found in the provider
    loader by the P3 audit and reproduced here from scratch."""
    def _exiting() -> Mapping[str, type]:
        raise SystemExit(1)

    _patch_entry_points(monkeypatch, [_entry("exiting", _exiting)])
    with caplog.at_level(logging.WARNING):
        assert dict(load_gate_plugins(shipped=frozenset())) == {}
    assert "SystemExit" in caplog.text


def test_keyboard_interrupt_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's Ctrl-C is not a plugin defect and must not be logged and swallowed."""
    def _interrupted() -> Mapping[str, type]:
        raise KeyboardInterrupt

    _patch_entry_points(monkeypatch, [_entry("interrupted", _interrupted)])
    with pytest.raises(KeyboardInterrupt):
        load_gate_plugins(shipped=frozenset())


def test_two_plugins_claiming_one_kind_means_neither_is_registered(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Load order must not decide which of two packages owns a gate kind."""
    _patch_entry_points(monkeypatch, [
        _entry("first", lambda: {"dup": _MarkerGate}),
        _entry("second", lambda: {"dup": _MarkerGate, "unique": _MarkerGate}),
    ])
    with caplog.at_level(logging.WARNING):
        loaded = load_gate_plugins(shipped=frozenset())
    assert dict(loaded) == {"dup": _MarkerGate}, "the first wins; the colliding plugin is skipped"
    assert "unique" not in loaded, "rule 2: the whole colliding plugin contributes nothing"


# ── GuardedGate: what constrains a plugin's BEHAVIOUR ───────────────────────────────────────────

def test_guarded_gate_satisfies_the_port_it_stands_in_for() -> None:
    assert isinstance(GuardedGate(_MarkerGate(), kind="k"), GatePort)


def test_a_well_behaved_gate_passes_through_unchanged(tmp_path: Path) -> None:
    """Calibration for every rejection below: the wrapper must not fail everything."""
    gate = GuardedGate(_MarkerGate(), kind="marker")
    assert gate.check(_ctx(tmp_path)).passed is False
    (tmp_path / "DONE.txt").write_text("x", encoding="utf-8")
    verdict = gate.check(_ctx(tmp_path))
    assert verdict.passed is True
    assert "found" in verdict.detail


@pytest.mark.parametrize("truthy", ["yes", 1, [1], {"a": 1}, 0.5])
def test_a_truthy_non_bool_is_not_a_pass(tmp_path: Path, truthy: object) -> None:
    """`Verdict.__post_init__` validates `detail`, NOT the type of `passed`, so a plugin can build a
    real Verdict whose `passed` is truthy — and `if verdict.passed` would accept it as a confirmed
    stop condition. Parametrised because a guard written for one value would pass a single case."""
    class _Truthy:
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed=truthy, detail="claims done", evidence={})  # type: ignore[arg-type]

    verdict = GuardedGate(_Truthy(), kind="truthy").check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "not a bool" in verdict.detail


@pytest.mark.parametrize("raised", [RuntimeError("boom"), SystemExit(1), MemoryError()])
def test_a_gate_that_raises_fails_the_lap(tmp_path: Path, raised: BaseException) -> None:
    """SystemExit is the important one: `except Exception` misses it, and a crashed gate read as
    'nothing to report' would let a loop continue toward DONE with nothing verified."""
    class _Raising:
        def check(self, ctx: LoopContext) -> Verdict:
            raise raised

    verdict = GuardedGate(_Raising(), kind="raising").check(_ctx(tmp_path))
    assert verdict.passed is False
    assert type(raised).__name__ in verdict.detail


def test_a_gate_raising_keyboard_interrupt_does_not_become_a_verdict(tmp_path: Path) -> None:
    class _Interrupted:
        def check(self, ctx: LoopContext) -> Verdict:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        GuardedGate(_Interrupted(), kind="interrupted").check(_ctx(tmp_path))


def test_a_non_verdict_return_is_not_a_pass(tmp_path: Path) -> None:
    class _WrongShape:
        def check(self, ctx: LoopContext) -> Verdict:
            return {"passed": True}  # type: ignore[return-value]

    verdict = GuardedGate(_WrongShape(), kind="wrong").check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "not a Verdict" in verdict.detail


def test_a_passing_verdict_with_no_detail_is_refused(tmp_path: Path) -> None:
    """A DONE nobody can explain afterwards is unreviewable, so an empty detail cannot pass."""
    class _Silent:
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed=True, detail="   ", evidence={})

    verdict = GuardedGate(_Silent(), kind="silent").check(_ctx(tmp_path))
    assert verdict.passed is False
    assert "no detail" in verdict.detail


# ── measurement is written by the loader, never by the plugin ───────────────────────────────────

def test_plugin_kinds_are_recorded_by_the_loader_and_exclude_shipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package must not be able to describe itself as measured against a corpus it never ran."""
    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"third-party": _MarkerGate})])
    registry, plugin_kinds = merged_gate_registry({"pytest": _MarkerGate})
    assert set(registry) == {"pytest", "third-party"}
    assert plugin_kinds == frozenset({"third-party"})
    assert "pytest" not in plugin_kinds, "a shipped kind is never reported as third-party"


def test_merging_leaks_no_state_between_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first version rebound a module global, so one caller's plugins leaked into the next."""
    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"first-only": _MarkerGate})])
    _, first = merged_gate_registry({"pytest": _MarkerGate})
    _patch_entry_points(monkeypatch, [])
    _, second = merged_gate_registry({"pytest": _MarkerGate})
    assert first == frozenset({"first-only"})
    assert second == frozenset(), "the previous call's plugin kinds leaked into this one"
