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


def test_composition_itself_performs_the_push_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload composition so the REAL import-time push runs. Nothing else can catch its deletion.

    Two earlier attempts at this test were vacuous and all three auditors said so. One asserted
    ``{"pytest","command","osv"} <= recognized_gate_kinds()`` — true from ``VALID_GATE_KINDS`` alone,
    independent of any push. The other called ``register_plugin_gate_kinds`` itself, proving the
    mechanism while leaving the WIRING untested. An Opus reviewer settled it empirically: it deleted
    ``composition.py``'s push line and ran all 45 tests — 45 passed.

    ``importlib.reload`` is the only way to execute that line in-process with discovery faked. The
    reload is undone in a ``finally`` so the module other tests import is the real one.
    """
    import importlib
    from bounded_loops.application import manifest as manifest_mod
    import bounded_loops.composition as comp

    _patch_entry_points(monkeypatch, [_entry("p", lambda: {"reload-probe": _MarkerGate})])
    monkeypatch.setattr(manifest_mod, "_PLUGIN_GATE_KINDS", frozenset())
    try:
        importlib.reload(comp)
        assert "reload-probe" in manifest_mod.recognized_gate_kinds(), (
            "composition imported without telling the manifest validator about plugin kinds"
        )
        assert "reload-probe" in comp.GATE_REGISTRY
    finally:
        monkeypatch.undo()
        importlib.reload(comp)
        manifest_mod.register_plugin_gate_kinds(comp.PLUGIN_GATE_KINDS)


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


def test_a_plugin_kind_is_usable_as_a_composite_child_through_a_real_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real loop.yaml with a composite whose CHILD is third-party, through manifest.load.

    The previous version called `_instantiate_gate_from_config` with a SimpleNamespace, so the
    load-time half of this fix could be reverted with the suite still green — an auditor showed that
    restoring `_validate_composite_gate` to the shipped-only allowlist kept it passing. It also hid a
    real coupling: the composer now reads `manifest.runner_kind` to decide whether a worker module
    exists, and a hand-made namespace does not have one. A real manifest exercises both.

    Composite is the only way to combine a shipped gate with a third-party one, which is the case a
    company most wants: their own check alongside ours.
    """
    import re
    import shutil
    import bounded_loops.composition as comp
    from bounded_loops.application import manifest as manifest_mod

    _extend_registry(monkeypatch, comp, "acme-check")
    monkeypatch.setattr(manifest_mod, "_PLUGIN_GATE_KINDS", frozenset({"acme-check"}))

    pkg = tmp_path / "pkg"
    shutil.copytree(_REAL_LOOP, pkg)
    mf = pkg / "loop.yaml"
    mf.write_text(
        re.sub(
            r"(?m)^gate:\n\s*kind:\s*osv\s*$",
            "gate:\n  kind: composite\n  gates:\n    - kind: acme-check\n    - kind: command\n      run: \"true\"",
            mf.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )
    assert "composite" in mf.read_text(encoding="utf-8"), "substitution did not apply"

    loaded = manifest_mod.load(pkg)
    assert loaded.gate_kind == "composite"

    child = comp._instantiate_gate_from_config({"kind": "acme-check"}, loaded)
    assert isinstance(child, GuardedGate), "a composite CHILD reached the aggregator unwrapped"


def test_a_composite_child_naming_an_unregistered_plugin_kind_is_refused_not_a_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kind declared third-party but absent from the registry means the two disagree.

    `registry[child_kind]` raised a bare KeyError straight past `wire()`'s handler as a traceback.
    """
    import types
    import bounded_loops.composition as comp

    monkeypatch.setattr(comp, "PLUGIN_GATE_KINDS", frozenset({"ghost-kind"}))
    manifest = types.SimpleNamespace(
        runner_kind="stub", raw={}, gate_config={},
        bounds=types.SimpleNamespace(max_wallclock_s=30, schema=None), loop_dir=tmp_path,
    )
    from bounded_loops.domain.errors import ManifestError
    with pytest.raises(ManifestError, match="not in the gate registry"):
        comp._instantiate_gate_from_config({"kind": "ghost-kind"}, manifest)


def test_a_substituted_shipped_gate_cannot_land_a_pass(tmp_path: Path) -> None:
    """The invariant that replaced registry freezing, stated as the audits forced it to be.

    The old test here was named "cannot be mutated by anything at all" and only tried
    ``proxy["osv"] = ...``. Three separate writes it never attempted all succeed: rebinding the module
    attribute, patching the gate CLASS, and — the one that mattered — rebinding the bare global that
    ``_build_gate`` actually calls, since first-class kinds are read as ``return PytestGate(...)`` and
    never through the registry at all.

    So provenance is not defensible in-process and the freeze is a convenience, not a boundary. What
    IS defensible is the verdict: whatever produced it, a non-boolean pass is refused. This test
    performs the strongest available substitution and asserts the outcome, not the mechanism.
    """
    import types
    import bounded_loops.composition as comp

    class _Hijack:
        def __init__(self, **kwargs: object) -> None: ...
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed="yes", detail="hijacked", evidence={})  # type: ignore[arg-type]

    original = comp.PytestGate
    try:
        comp.PytestGate = _Hijack  # type: ignore[misc]
        manifest = types.SimpleNamespace(
            gate_kind="pytest", gate_config={},
            bounds=types.SimpleNamespace(max_wallclock_s=30, schema=None), loop_dir=tmp_path,
        )
        gate = comp._instantiate_gate("pytest", manifest)
        assert isinstance(gate, GuardedGate), "a shipped gate reached the engine unwrapped"
        verdict = gate.check(_ctx(tmp_path))
        assert verdict.passed is False, "a substituted gate landed a PASS on the rules layer"
        assert "not a bool" in verdict.detail
    finally:
        comp.PytestGate = original  # type: ignore[misc]


def test_a_genuine_shipped_gate_is_unaffected_by_the_wrapper(tmp_path: Path) -> None:
    """Calibration for universal wrapping: a well-formed verdict must pass through untouched."""
    import types
    import bounded_loops.composition as comp

    (tmp_path / "check.sh").write_text("exit 0\n", encoding="utf-8")
    manifest = types.SimpleNamespace(
        gate_kind="command", gate_config={"run": "true"},
        bounds=types.SimpleNamespace(max_wallclock_s=30, schema=None), loop_dir=tmp_path,
    )
    gate = comp._instantiate_gate("command", manifest)
    assert isinstance(gate, GuardedGate)
    verdict = gate.check(_ctx(tmp_path))
    assert verdict.passed is True, "wrapping broke a shipped gate that should pass"
    assert verdict.detail.strip()


# ── rule 4 had ZERO test coverage; an Opus reviewer's grep found no reference at all ────────────

def _dists(**kw: str) -> Mapping[str, str]:
    return kw


def test_rule_four_refuses_a_worker_and_gate_from_one_distribution() -> None:
    """The case the rule exists for. `bounded_loops` resolves to exactly one distribution here."""
    refusal = gp.same_distribution_refusal(
        gate_kind="acme-check",
        gate_distributions=_dists(**{"acme-check": "bounded-loops"}),
        worker_module="bounded_loops.adapters.runners.stub",
    )
    assert refusal is not None
    assert "bounded-loops" in refusal


@pytest.mark.parametrize("gate_dist", ["Bounded_Loops", "bounded.loops", "BOUNDED-LOOPS"])
def test_rule_four_normalises_distribution_names_per_pep503(gate_dist: str) -> None:
    """`acme.gates`, `acme_gates` and `Acme-Gates` are one project; hand-rolled matching missed that."""
    assert gp.same_distribution_refusal(
        gate_kind="k", gate_distributions=_dists(k=gate_dist),
        worker_module="bounded_loops.x",
    ) is not None


def test_rule_four_does_not_refuse_unrelated_distributions() -> None:
    assert gp.same_distribution_refusal(
        gate_kind="k", gate_distributions=_dists(k="totally-unrelated"),
        worker_module="bounded_loops.x",
    ) is None


def test_rule_four_fails_open_when_a_namespace_root_has_several_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false positive both CLI auditors proved on this host, one with a real `jaraco.*` pairing.

    `packages_distributions()` maps a top-level name to EVERY distribution shipping under it, so a
    namespace package makes unrelated siblings indistinguishable — `google` is shared by protobuf,
    google-auth and google-api-core. Set membership refused loops whose worker and gate came from
    genuinely different projects. Ambiguity now fails OPEN, because wrongly blocking a legitimate loop
    is worse than not enforcing a rule the metadata cannot decide.
    """
    monkeypatch.setattr(
        gp, "packages_distributions", lambda: {"myco": ["myco-finance", "myco-tools"]},
    )
    assert gp.same_distribution_refusal(
        gate_kind="k", gate_distributions=_dists(k="myco-tools"),
        worker_module="myco.finance.worker",
    ) is None, "an ambiguous namespace root must not produce a refusal"


@pytest.mark.parametrize("boom", [OSError("io"), UnicodeDecodeError("utf-8", b"", 0, 1, "bad"),
                                 TypeError("empty dist-info"), RuntimeError("corrupt")])
def test_rule_four_treats_broken_metadata_as_non_evidence(
    monkeypatch: pytest.MonkeyPatch, boom: BaseException,
) -> None:
    """Only OSError was caught. A non-UTF-8 METADATA raises UnicodeDecodeError, an empty .dist-info
    TypeError — verified live by an auditor as a traceback out of `wire()`. Damaged metadata is not
    evidence of a violation, and a crash is a worse outcome than an unenforced rule."""
    def _raise() -> Mapping[str, list[str]]:
        raise boom

    monkeypatch.setattr(gp, "packages_distributions", _raise)
    assert gp.same_distribution_refusal(
        gate_kind="k", gate_distributions=_dists(k="bounded-loops"),
        worker_module="bounded_loops.x",
    ) is None


def test_rule_four_is_silent_for_runners_that_import_no_module() -> None:
    """Documented scope: shell/agent_cmd have no module to compare, so nothing is checked."""
    assert gp.same_distribution_refusal(
        gate_kind="k", gate_distributions=_dists(k="bounded-loops"), worker_module=None,
    ) is None


def test_rule_four_skips_shipped_kinds() -> None:
    assert gp.same_distribution_refusal(
        gate_kind="pytest", gate_distributions=_dists(), worker_module="bounded_loops.x",
    ) is None


def test_refuse_if_same_distribution_raises_for_a_plugin_kind() -> None:
    """The guard wrapper `composition` actually calls — previously untested in either suite."""
    with pytest.raises(GatePluginRefused, match="cannot certify this loop"):
        gp.refuse_if_same_distribution(
            gate_kind="acme-check", plugin_kinds=frozenset({"acme-check"}),
            gate_distributions=_dists(**{"acme-check": "bounded-loops"}),
            worker_module="bounded_loops.x",
        )


def test_refuse_if_same_distribution_is_a_no_op_for_shipped_kinds() -> None:
    gp.refuse_if_same_distribution(
        gate_kind="pytest", plugin_kinds=frozenset({"acme-check"}),
        gate_distributions=_dists(**{"acme-check": "bounded-loops"}),
        worker_module="bounded_loops.x",
    )


# ── Round 4: the wrapper's own type test was the bypass ────────────────────────────────────
#
# Round 3 made wrapping unconditional but skipped anything that already looked wrapped:
#     return built if isinstance(built, GuardedGate) else GuardedGate(built, kind=gate_key)
# `isinstance` consults `__class__`, which the object being tested controls. So an object only
# had to CLAIM to be a GuardedGate to skip every check in `GuardedGate.check`. Reproduced
# end-to-end returning passed="yes" — a truthy non-bool reaching the rules layer, which is a
# loop reaching DONE with nothing verified.
#
# Two independent defences, tested independently below so that removing either one fails a test:
#   1. the call sites test the EXACT type, which cannot be spoofed
#   2. GuardedGate refuses to be subclassed, so the honest version of the vector cannot exist


class _ClassLiar:
    """Not a subclass of GuardedGate. Merely claims to be one, which `isinstance` believes."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    @property
    def __class__(self) -> type:  # type: ignore[override]
        return GuardedGate

    def check(self, ctx: LoopContext) -> Verdict:
        return Verdict(passed="yes", detail="verified nothing")  # type: ignore[arg-type]


def test_isinstance_believes_the_liar_which_is_why_the_call_sites_cannot_use_it() -> None:
    """Pins the language behaviour the bypass depended on, so the next reader sees WHY."""
    liar = _ClassLiar()
    assert isinstance(liar, GuardedGate) is True, "if this ever becomes False the risk is gone"
    assert type(liar) is not GuardedGate


def test_a_gate_that_lies_about_its_type_is_still_wrapped_and_still_checked() -> None:
    """The BLOCKER, end to end through the real composition path."""
    from bounded_loops import composition

    original = composition.CommandGate
    composition.CommandGate = _ClassLiar  # type: ignore[misc]
    try:
        manifest = object.__new__(composition.LoopManifest)
        object.__setattr__(manifest, "gate_config", {"run": "true"})
        object.__setattr__(manifest, "bounds", type("B", (), {"max_wallclock_s": 5})())
        verdict = composition._instantiate_gate("command", manifest).check(None)  # type: ignore[arg-type]
    finally:
        composition.CommandGate = original

    assert verdict.passed is False, "a liar's verdict reached the rules layer UNVALIDATED"
    assert "not a bool" in verdict.detail


def test_a_composite_child_that_lies_about_its_type_is_still_wrapped() -> None:
    """The SECOND call site. A fix that reaches one site and misses its sibling is this
    project's most-repeated defect, so the sibling gets its own test."""
    from bounded_loops import composition

    from bounded_loops.application.manifest import load as load_manifest

    manifest = load_manifest(Path("loops/assertion-density"))
    original = composition.CommandGate
    composition.CommandGate = _ClassLiar  # type: ignore[misc]
    try:
        child = composition._instantiate_gate_from_config(
            {"kind": "command", "run": "true"}, manifest
        )
    finally:
        composition.CommandGate = original

    assert type(child) is GuardedGate, "a lying composite child reached the aggregator unwrapped"
    assert child.check(None).passed is False  # type: ignore[arg-type]


def test_guarded_gate_refuses_to_be_subclassed() -> None:
    """The honest form of the same vector, killed at class-creation time."""
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class _Sneaky(GuardedGate):  # type: ignore[misc]
            pass


def test_a_verdict_that_raises_during_validation_becomes_a_failing_verdict() -> None:
    """Validation used to sit OUTSIDE the try, so a Verdict whose field raises escaped the
    wrapper. Availability rather than a forged pass — but not from inside the containment."""

    class _BombVerdict(Verdict):
        # A plain __init__ that skips the frozen-dataclass one. Without this the bomb detonates
        # inside the dataclass __init__ (object.__setattr__ onto a property with no setter), which
        # is INSIDE `_inner.check` — the path that already worked. The first version of this test
        # did exactly that and passed with the fix reverted.
        def __init__(self) -> None:
            pass

        @property
        def passed(self) -> bool:  # type: ignore[override]
            raise SystemExit("raises on ACCESS, during validation, not during check()")

    class _BombGate:
        def check(self, ctx: LoopContext) -> Verdict:
            return _BombVerdict()

    verdict = GuardedGate(_BombGate(), kind="bomb").check(None)  # type: ignore[arg-type]
    assert verdict.passed is False
    assert "raised" in verdict.detail


def test_a_wrapped_gate_cannot_have_its_inner_swapped() -> None:
    """Defence in depth, NOT containment: `object.__setattr__` and `gc.get_referrers` both defeat
    this. It makes the casual and accidental rebind fail loudly instead of silently changing
    which gate a verdict is read from."""
    gate = GuardedGate(_MarkerGate(), kind="k")
    with pytest.raises(AttributeError, match="frozen after construction"):
        gate._inner = _MarkerGate()


def test_gate_cmd_override_reaches_the_engine_wrapped() -> None:
    """The one construction site universal wrapping did not cover until round 4."""
    from bounded_loops import composition
    from bounded_loops.application.manifest import load as load_manifest

    manifest = load_manifest(Path("loops/assertion-density"))
    gate = composition.wire(manifest, gate_cmd_override="true")._deps.gate

    assert type(gate) is GuardedGate, "--gate-override built a RAW gate"
    assert gate.gate_kind == "command-override"
    assert type(gate.wraps).__name__ == "CommandGate"


def test_a_guarded_gate_refuses_to_be_copied_or_pickled() -> None:
    """The `__setattr__` freeze broke copy and pickle as a SIDE EFFECT, with a message about
    rebinding `_inner` that explained nothing. Refusing is also correct on the merits: a gate is
    the trust anchor a verdict is read from. Asserts the REASON, not merely that it raises —
    a wrong-arity TypeError from an aliased method also raises, and did."""
    import copy
    import pickle

    gate = GuardedGate(_MarkerGate(), kind="k")
    for label, call in (
        ("copy", lambda: copy.copy(gate)),
        ("deepcopy", lambda: copy.deepcopy(gate)),
        ("pickle", lambda: pickle.dumps(gate)),
    ):
        with pytest.raises(TypeError, match="cannot be pickled or copied") as caught:
            call()
        assert "trust anchor" in str(caught.value), f"{label} raised for the wrong reason"


def test_a_guarded_gate_can_still_be_weak_referenced() -> None:
    """`__slots__` SILENTLY removes weak-reference support unless "__weakref__" is listed.

    Not a security property — a regression guard. A wrapper that quietly withdraws a language
    capability is discovered by a caller tripping over it, and the wrapper is now on every gate.
    """
    import weakref

    gate = GuardedGate(_MarkerGate(), kind="k")
    assert weakref.ref(gate)() is gate


def test_a_plugin_cannot_claim_a_kind_the_harness_reserves_for_itself() -> None:
    """Provenance forgery: a plugin offering "command-override" produced a gate whose recorded
    kind was indistinguishable from the operator's own --gate-override gate. The receipt is the
    reason this matters — a provenance field an outsider can write is not provenance."""
    with pytest.raises(GatePluginRefused, match="reserves for gates it builds itself"):
        gp._validated_kind("command-override", source="acme-dist")


def test_reserving_names_did_not_break_ordinary_plugin_kinds() -> None:
    """Calibrated in both directions: the refusal must be narrow, not a blanket tightening."""
    assert gp._validated_kind("acme-check", source="acme-dist") == "acme-check"
    assert gp._validated_kind("command", source="acme-dist") == "command"


def test_every_reserved_kind_is_one_the_harness_actually_builds() -> None:
    """Anti-rot: a reserved name that nothing constructs is a refusal with no reason behind it,
    which is how a deny-list becomes folklore. If the override's kind string is ever renamed,
    this fails rather than leaving the old name reserved and the new one squattable."""
    from pathlib import Path

    from bounded_loops import composition
    from bounded_loops.application.manifest import load as load_manifest

    manifest = load_manifest(Path("loops/assertion-density"))
    built = composition.wire(manifest, gate_cmd_override="true")._deps.gate
    assert built.gate_kind in gp._RESERVED_INTERNAL_KINDS
    assert gp._RESERVED_INTERNAL_KINDS == {"command-override"}, (
        "a name was added to the reserved set — assert here that the harness builds it"
    )


def test_a_verdict_cannot_change_its_answer_after_being_validated() -> None:
    """TOCTOU: the wrapper validated `raw` and then returned `raw`. A property answers per call,
    so the value checked and the value delivered were allowed to differ.

    Reproduced: False for every validation read, True afterwards, detail='' — the rules layer got
    a PASSING verdict with no explanation, the exact unexplainable DONE the detail rule forbids.
    A snapshot is only worth something if the snapshot is what ships.
    """

    class _FlipVerdict(Verdict):
        def __init__(self) -> None:
            object.__setattr__(self, "_n", 0)
            object.__setattr__(self, "evidence", {"cmd": "true"})

        @property
        def passed(self) -> bool:  # type: ignore[override]
            object.__setattr__(self, "_n", self._n + 1)
            return False if self._n <= 3 else True

        @property
        def detail(self) -> str:  # type: ignore[override]
            return ""

    class _FlipGate:
        def check(self, ctx: LoopContext) -> Verdict:
            return _FlipVerdict()

    out = GuardedGate(_FlipGate(), kind="flip").check(None)  # type: ignore[arg-type]

    assert type(out) is Verdict, "the gate's OWN object was handed onward, so it can still flip"
    assert out.passed is False
    assert out.passed is False, "two reads disagreed — the returned verdict is not a snapshot"
    assert not (out.passed and not out.detail.strip()), "passing verdict with no detail escaped"


def test_a_well_formed_verdict_survives_validation_with_its_content_intact() -> None:
    """Calibration in the other direction. Returning a fresh Verdict must not quietly drop the
    gate's detail or evidence — a receipt built from a laundered verdict explains nothing."""

    class _GoodGate:
        def check(self, ctx: LoopContext) -> Verdict:
            return Verdict(passed=True, detail="pytest: 42 passed", evidence={"code": 0})

    out = GuardedGate(_GoodGate(), kind="pytest").check(None)  # type: ignore[arg-type]
    assert out.passed is True
    assert out.detail == "pytest: 42 passed"
    assert dict(out.evidence) == {"code": 0}
