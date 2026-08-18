"""Gate plugins (ADR-13 B1): the seven rules, each proven capable of failing.

A gate plugin is the sharpest thing this engine loads from a third party, because a mistake here is
not a crash — it is a node reaching DONE on work nobody checked. So every rule below is tested with
a plugin that actually tries the thing, not with a well-behaved fixture.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from bounded_loops.graph.adapters.workers.gate_plugins import (
    GATE_ENTRY_POINT_GROUP,
    PROVIDER_ENTRY_POINT_GROUP,
    RESERVED_GATE_KINDS,
    GuardedGate,
    RegisteredGate,
    load_gate_plugins,
    same_distribution_refusal,
)
from bounded_loops.graph.application.node_contracts import GateVerdict

_KW = {"plan": None, "node": None, "result": None, "attempt": 1, "repair_round": 0}


class _Gate:
    """A gate whose evaluate returns whatever it was constructed with, or raises it."""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def evaluate(self, **_kwargs: object) -> object:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _entry(name: str, factory) -> EntryPoint:  # noqa: ANN001
    """An EntryPoint whose load() returns `factory`, with no packaging involved."""
    ep = EntryPoint(name=name, value=f"tests.fake:{name}", group=GATE_ENTRY_POINT_GROUP)
    object.__setattr__(ep, "load", lambda: factory)
    return ep


def _load(entries, reserved=RESERVED_GATE_KINDS, monkeypatch=None):  # noqa: ANN001
    import bounded_loops.graph.adapters.workers.gate_plugins as mod

    monkeypatch.setattr(mod, "entry_points", lambda group: tuple(entries))
    return load_gate_plugins(reserved=reserved)


# ── rule 5: a truthy non-bool is not a pass ──────────────────────────────────


@pytest.mark.parametrize("truthy", ["yes", 1, [1], {"ok": True}, object()])
def test_a_truthy_non_bool_is_not_a_pass(truthy: object) -> None:
    """The defect this rule closes. `if verdict.passed` would accept every value here.

    That is a node reaching DONE because a field was non-empty, which is indistinguishable from a
    gate that actually approved the work.
    """
    guarded = GuardedGate("mine", _Gate(GateVerdict(passed=truthy, reason="r")), distribution="d")

    verdict = guarded.evaluate(**_KW)

    assert verdict.passed is False, f"passed={truthy!r} must not be accepted as a pass"
    assert "not a bool" in verdict.reason


def test_a_real_bool_pass_is_honoured() -> None:
    """Proof the guard above is not simply refusing everything."""
    guarded = GuardedGate("mine", _Gate(GateVerdict(passed=True, reason="ok")), distribution="d")

    verdict = guarded.evaluate(**_KW)

    assert verdict.passed is True
    assert verdict.reason == "ok"


@pytest.mark.parametrize("bad", [GateVerdict(passed=True, reason=""), GateVerdict(True, "   ")])
def test_an_unreadable_reason_is_refused(bad: GateVerdict) -> None:
    """A verdict nobody can read is not auditable, and a passing one least of all."""
    verdict = GuardedGate("mine", _Gate(bad), distribution="d").evaluate(**_KW)

    assert verdict.passed is False
    assert "empty reason" in verdict.reason


def test_a_malformed_evidence_digest_is_refused() -> None:
    """The digest is what makes a verdict tamper-evident rather than merely readable."""
    bad = GateVerdict(passed=True, reason="ok", evidence_digest="sha256:nothex")

    verdict = GuardedGate("mine", _Gate(bad), distribution="d").evaluate(**_KW)

    assert verdict.passed is False
    assert "evidence_digest" in verdict.reason


def test_a_non_verdict_return_is_refused() -> None:
    verdict = GuardedGate("mine", _Gate({"passed": True}), distribution="d").evaluate(**_KW)

    assert verdict.passed is False
    assert "not a GateVerdict" in verdict.reason


# ── rule 6: raising is a failure, never a pass ───────────────────────────────


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyError("k"), SystemExit(1)])
def test_a_gate_that_raises_fails_closed(exc: BaseException) -> None:
    """An exception in third-party code must not be able to end a step successfully.

    ``SystemExit`` is in the list on purpose: it is a BaseException, so an `except Exception`
    handler would let it propagate and kill the run. That exact escape was a real audit finding in
    this project's provider plugin path.
    """
    guarded = GuardedGate("mine", _Gate(exc), distribution="d")

    verdict = guarded.evaluate(**_KW)

    assert verdict.passed is False
    assert type(exc).__name__ in verdict.reason
    assert "mine" in verdict.reason, "the reason must name the plugin so an operator can find it"


def test_the_wrapper_forwards_every_keyword_it_is_given() -> None:
    """A wrapper that dropped a keyword would hand the gate less context than the engine believes
    it gave, which is how a gate stops being able to tell one attempt from another."""
    seen: dict[str, object] = {}

    class _Recorder:
        def evaluate(self, **kwargs: object) -> GateVerdict:
            seen.update(kwargs)
            return GateVerdict(passed=True, reason="ok")

    GuardedGate("mine", _Recorder(), distribution="d").evaluate(**_KW)

    assert seen == _KW, "attempt and repair_round in particular must reach the gate"


# ── rule 7: one distribution cannot both do and check the work ───────────────


def test_the_same_distribution_cannot_supply_both_worker_and_gate() -> None:
    """def:independence requires disjoint write authority. Compared mechanically, not promised."""
    gate = RegisteredGate(kind="mine", gate=_Gate(None), distribution="acme-agents")

    refusal = same_distribution_refusal(gate, worker_distribution="ACME-Agents")

    assert refusal is not None, "the comparison must be case-insensitive"
    assert "disjoint write authority" in refusal


def test_a_different_distribution_is_allowed_and_an_unknown_one_is_not_a_match() -> None:
    """Refusing on absence would block every shipped provider, whose worker is not a plugin and so
    has no distribution at all."""
    gate = RegisteredGate(kind="mine", gate=_Gate(None), distribution="acme-agents")

    assert same_distribution_refusal(gate, worker_distribution="other-pkg") is None
    assert same_distribution_refusal(gate, worker_distribution=None) is None
    assert same_distribution_refusal(gate, worker_distribution="") is None


# ── rules 1-3 and the measurement claim ──────────────────────────────────────


def test_a_plugin_that_raises_on_load_is_skipped_not_fatal(monkeypatch) -> None:
    def _explode():
        raise ImportError("no module named hope")

    loaded = _load([_entry("bad", _explode), _entry("good", lambda: {"fine": _Gate(None)})],
                   monkeypatch=monkeypatch)

    assert set(loaded) == {"fine"}, "one broken plugin must not take the others down"


def test_registration_is_all_or_nothing_per_plugin(monkeypatch) -> None:
    """A plugin offering three gates and one bad one contributes nothing.

    Half-registering would leave an author with a gate set that depends on iteration order.
    """
    def _mixed():
        return {"aa": _Gate(None), "bb": object(), "cc": _Gate(None)}

    loaded = _load([_entry("mixed", _mixed)], monkeypatch=monkeypatch)

    assert loaded == {}, "a plugin with one bad gate must contribute none of them"


@pytest.mark.parametrize("reserved", sorted(RESERVED_GATE_KINDS))
def test_a_plugin_cannot_claim_a_shipped_gate_kind(reserved: str, monkeypatch) -> None:
    """The supply-chain move: register `pytest` and silently become the gate nine loops bind to."""
    loaded = _load([_entry("evil", lambda: {reserved: _Gate(None)})], monkeypatch=monkeypatch)

    assert loaded == {}, f"{reserved!r} is shipped and must not be claimable"


def test_two_plugins_offering_one_kind_do_not_depend_on_install_order(monkeypatch) -> None:
    first = _entry("first", lambda: {"dup": _Gate(None)})
    second = _entry("second", lambda: {"dup": _Gate(None)})

    forward = _load([first, second], monkeypatch=monkeypatch)
    assert set(forward) == {"dup"}
    assert forward["dup"].distribution == "<unpackaged:first>"


def test_a_colliding_plugin_is_skipped_whole_not_partially(monkeypatch) -> None:
    """Matches the provider loader's policy. Admitting the non-colliding half would let install
    order decide which of a plugin's gates a graph actually gets."""
    first = _entry("first", lambda: {"dup": _Gate(None)})
    second = _entry("second", lambda: {"dup": _Gate(None), "unique": _Gate(None)})

    loaded = _load([first, second], monkeypatch=monkeypatch)

    assert set(loaded) == {"dup"}, "'unique' must not survive its plugin being refused"


def test_every_loaded_gate_is_wrapped_and_reported_unmeasured(monkeypatch) -> None:
    """Two claims in one, because they are the same guarantee.

    No unwrapped third-party gate may reach the controller, and no plugin may describe itself as
    measured against a corpus it has never seen. `measured` is written by the loader.
    """
    loaded = _load([_entry("p", lambda: {"mine": _Gate(GateVerdict(passed="truthy", reason="r"))})],
                   monkeypatch=monkeypatch)

    registered = loaded["mine"]
    assert isinstance(registered.gate, GuardedGate), "the engine must never see an unwrapped gate"
    assert registered.measured is False

    # And the wrapping is load-bearing, not decorative: the truthy pass is still refused.
    assert registered.gate.evaluate(**_KW).passed is False


@pytest.mark.parametrize(
    "kind", ["", "A", "has space", "trailing-", "-leading", "double--hyphen", "x" * 41, "1leading"],
)
def test_a_malformed_gate_kind_is_refused(kind: str, monkeypatch) -> None:
    """A kind becomes a manifest value and a receipt field; restricting the shape keeps it usable
    without quoting rules nobody remembers."""
    assert _load([_entry("p", lambda: {kind: _Gate(None)})], monkeypatch=monkeypatch) == {}


def test_discovery_failure_returns_none_rather_than_breaking_the_engine(monkeypatch) -> None:
    import bounded_loops.graph.adapters.workers.gate_plugins as mod

    def _broken(group):  # noqa: ANN001, ARG001
        raise SystemExit("broken install")

    monkeypatch.setattr(mod, "entry_points", _broken)

    assert load_gate_plugins() == {}


def test_the_provider_group_string_has_not_drifted() -> None:
    """Rule 7 compares against provider entry points, so the two group names must agree. Mirrored
    rather than imported to keep this adapter off another adapter, which is exactly the drift this
    asserts against."""
    from bounded_loops.graph.adapters.connectors.provider_plugins import (
        PROVIDER_ENTRY_POINT_GROUP as canonical,
    )

    assert PROVIDER_ENTRY_POINT_GROUP == canonical
