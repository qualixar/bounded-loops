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

    monkeypatch.setattr(
        gp, "load_gate_plugins",
        lambda **kw: gp.LoadedPlugins(gates={"pytest": _Hijack}, distributions={}),
    )
    assert merged_gate_registry({"pytest": _MarkerGate}).registry["pytest"] is _MarkerGate


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
    assert dict(loaded.gates) == {"good-kind": _MarkerGate}, "a broken plugin must not block a good one"
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
        assert dict(load_gate_plugins(shipped=frozenset()).gates) == {}
    assert "SystemExit" in caplog.text


def test_keyboard_interrupt_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's Ctrl-C is not a plugin defect and must not be logged and swallowed."""
    def _interrupted() -> Mapping[str, type]:
        raise KeyboardInterrupt

    _patch_entry_points(monkeypatch, [_entry("interrupted", _interrupted)])
    with pytest.raises(KeyboardInterrupt):
        load_gate_plugins(shipped=frozenset())


def test_a_colliding_second_plugin_is_skipped_entirely_and_the_first_keeps_its_kind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Named for what the code DOES. The earlier name claimed "neither is registered" while the
    assertion checked first-wins — the same declared-vs-enforced gap this project keeps finding,
    living in a test name. Registration order is deterministic (entry-point iteration), so the
    first registrant keeps the kind and the LATER plugin contributes nothing at all, including its
    non-colliding kinds. That is rule 2, and it is what stops load order deciding a partial set.
    """
    _patch_entry_points(monkeypatch, [
        _entry("first", lambda: {"dup": _MarkerGate}),
        _entry("second", lambda: {"dup": _MarkerGate, "unique": _MarkerGate}),
    ])
    with caplog.at_level(logging.WARNING):
        loaded = load_gate_plugins(shipped=frozenset())
    assert dict(loaded.gates) == {"dup": _MarkerGate}
    assert "unique" not in loaded.gates, "rule 2: the colliding plugin contributes NOTHING"
    assert "second" in caplog.text, "the skip must be visible to an operator"


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


def test_a_failing_verdict_with_a_thin_detail_keeps_its_own_reason(tmp_path: Path) -> None:
    """Calibration for the rule above, and a correction: the guard applies to PASSES only.

    The first version rejected any empty detail regardless of `passed`, which is stricter than the
    documented rule and would replace a gate's own failure reason with a generic one for nothing.
    """
    class _Terse:
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed=False, detail="   ", evidence={"why": "kept"})

    verdict = GuardedGate(_Terse(), kind="terse").check(_ctx(tmp_path))
    assert verdict.passed is False
    assert verdict.evidence == {"why": "kept"}, "the gate's own evidence must survive"


# ── measurement is written by the loader, never by the plugin ───────────────────────────────────

def test_plugin_kinds_are_recorded_by_the_loader_and_exclude_shipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package must not be able to describe itself as measured against a corpus it never ran."""
    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"third-party": _MarkerGate})])
    loaded = merged_gate_registry({"pytest": _MarkerGate})
    assert set(loaded.registry) == {"pytest", "third-party"}
    assert loaded.plugin_kinds == frozenset({"third-party"})
    assert "pytest" not in loaded.plugin_kinds, "a shipped kind is never reported as third-party"


def test_merging_leaks_no_state_between_callers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first version rebound a module global, so one caller's plugins leaked into the next."""
    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"first-only": _MarkerGate})])
    first = merged_gate_registry({"pytest": _MarkerGate}).plugin_kinds
    _patch_entry_points(monkeypatch, [])
    second = merged_gate_registry({"pytest": _MarkerGate}).plugin_kinds
    assert first == frozenset({"first-only"})
    assert second == frozenset(), "the previous call's plugin kinds leaked into this one"


# ── INTEGRATION: through manifest.load and composition, in the DEFAULT suite ────────────────────
#
# This section exists because both auditors made the same point about the tests above: delete the
# call in composition and every one of them still passes. They exercise the loader; they cannot
# detect that nothing calls it, or that `manifest.load` refuses the kind before the engine is ever
# reached — which is exactly the blocker that shipped. These go through the real entry points and
# run WITHOUT the opt-in env var, so a regression fails the ordinary suite.

REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_LOOP = REPO_ROOT / "loops" / "osv-scanner-example"


def _extend_registry(monkeypatch: pytest.MonkeyPatch, comp: object, kind: str) -> None:
    """Add one plugin kind by rebinding a fresh frozen proxy, as composition itself does."""
    from types import MappingProxyType
    extended = dict(comp.GATE_REGISTRY) | {kind: _MarkerGate}  # type: ignore[attr-defined]
    monkeypatch.setattr(comp, "GATE_REGISTRY", MappingProxyType(extended))
    monkeypatch.setattr(comp, "PLUGIN_GATE_KINDS", frozenset({kind}))


def _loop_dir_with_gate_kind(tmp_path: Path, kind: str) -> Path:
    """A copy of a SHIPPED loop package with only `gate.kind` changed.

    Copying the whole package matters: `loop.yaml` alone leaves PROMPT.md and bounds.yaml behind and
    the manifest fails for an unrelated reason, which is how a repro accidentally 'confirms' a bug
    that is not there. The baseline assertion below is what makes the substitution the only variable.
    """
    import re
    import shutil as _shutil
    dest = tmp_path / "pkg"
    _shutil.copytree(_REAL_LOOP, dest)
    manifest = dest / "loop.yaml"
    original = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        re.sub(r"(?m)^(\s*)kind:\s*osv\s*$", rf"\1kind: {kind}", original, count=1),
        encoding="utf-8",
    )
    assert f"kind: {kind}" in manifest.read_text(encoding="utf-8"), "substitution did not apply"
    return dest


def test_the_baseline_loop_package_loads_unmodified() -> None:
    """Guard for every test below: if the shipped package stopped loading they would prove nothing."""
    from bounded_loops.application.manifest import load
    assert load(_REAL_LOOP).gate_kind == "osv"


def test_the_registration_push_is_what_makes_a_plugin_kind_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The push must be LOAD-BEARING: refused before it, accepted after, nothing else changed.

    An earlier version of this test asserted `comp.PLUGIN_GATE_KINDS <= recognized_gate_kinds()`.
    With no plugin installed that set is empty, and the empty set is a subset of everything — so it
    passed with the registration call commented out. Proved by doing exactly that. A tautology of the
    same family the audits keep finding, written while trying to close one.

    `_PLUGIN_GATE_KINDS` is reset to empty first so the assertion cannot ride on real state, and the
    ONLY statement between the two `load` calls is the push.
    """
    from bounded_loops.application import manifest as manifest_mod

    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"acme-check": _MarkerGate})])
    loaded = merged_gate_registry({"pytest": _MarkerGate})
    assert loaded.plugin_kinds == frozenset({"acme-check"}), "discovery itself failed"

    monkeypatch.setattr(manifest_mod, "_PLUGIN_GATE_KINDS", frozenset())
    loop_dir = _loop_dir_with_gate_kind(tmp_path, "acme-check")
    with pytest.raises(manifest_mod.ManifestError, match="not a recognized kind"):
        manifest_mod.load(loop_dir)

    manifest_mod.register_plugin_gate_kinds(loaded.plugin_kinds)  # <- the only change

    assert manifest_mod.load(loop_dir).gate_kind == "acme-check"


def test_shipped_kinds_survive_a_registration_push() -> None:
    """The push REPLACES the plugin set; it must not disturb the shipped allowlist."""
    from bounded_loops.application import manifest as manifest_mod
    import bounded_loops.composition  # noqa: F401 — importing is what performs the real push

    assert {"pytest", "command", "composite", "osv"} <= manifest_mod.recognized_gate_kinds()


def test_a_plugin_kind_passes_manifest_load_and_instantiates_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end on the path a USER takes: a real loop.yaml naming a third-party gate.

    The live test proved discovery from a real installed distribution but built its manifest with a
    SimpleNamespace, so it never touched `manifest.load` — and that is precisely where the feature
    was broken. Discovery is faked here so this can run in the default suite; the validation and
    instantiation paths are real.
    """
    import bounded_loops.composition as comp
    from bounded_loops.application import manifest as manifest_mod

    # setitem is impossible here BY DESIGN — the registry is a frozen proxy — so extend it the way
    # composition does, by binding a new proxy. That the naive form fails is itself the guarantee.
    _extend_registry(monkeypatch, comp, "acme-check")
    monkeypatch.setattr(manifest_mod, "_PLUGIN_GATE_KINDS", frozenset({"acme-check"}))

    loaded = manifest_mod.load(_loop_dir_with_gate_kind(tmp_path, "acme-check"))
    assert loaded.gate_kind == "acme-check"

    gate = comp._instantiate_gate("acme-check", loaded)
    assert isinstance(gate, GuardedGate), "a third-party gate reached the engine UNWRAPPED"
    assert gate.gate_kind == "acme-check"


def test_an_unknown_gate_kind_is_still_refused_so_the_allowlist_did_not_become_a_no_op(
    tmp_path: Path,
) -> None:
    """Calibration: widening for plugins must not accept anything a user typos."""
    from bounded_loops.application.manifest import ManifestError, load
    with pytest.raises(ManifestError, match="not a recognized kind"):
        load(_loop_dir_with_gate_kind(tmp_path, "no-such-gate"))


def test_a_plugin_kind_is_usable_as_a_composite_child_and_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composite is the only way to combine a shipped gate with a third-party one."""
    import bounded_loops.composition as comp

    _extend_registry(monkeypatch, comp, "acme-check")

    import types
    fake_manifest = types.SimpleNamespace(
        bounds=types.SimpleNamespace(max_wallclock_s=30, schema=None), loop_dir=Path("."),
    )
    child = comp._instantiate_gate_from_config({"kind": "acme-check"}, fake_manifest)
    assert isinstance(child, GuardedGate), "a composite CHILD reached the aggregator unwrapped"


def test_the_shipped_registry_cannot_be_mutated_by_anything_at_all() -> None:
    """Rule 3's structural half, at the object level: the live registries are frozen.

    A gate plugin's factory runs arbitrary code in this process, and `_instantiate_gate` reads shipped
    classes straight out of these registries WITHOUT GuardedGate — so a mutable registry is a path to
    an unchecked verdict on the rules layer. Freezing is only meaningful because the backing dicts are
    built inside functions and are unreachable once the proxies exist.
    """
    import bounded_loops.composition as comp
    for name in ("GATE_REGISTRY", "_P2_GATE_REGISTRY", "_QUALIXAR_GATE_REGISTRY"):
        with pytest.raises(TypeError):
            comp.__dict__[name]["osv"] = _MarkerGate
    assert not hasattr(comp, "built"), "a backing dict leaked to module scope; the freeze is a no-op"
