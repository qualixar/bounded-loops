"""Third-party gate packages, discovered through entry points.

The independent gate IS the product, so a third party publishing one is the extensibility story
that matches the thesis. Until now the only way to ship a gate was to ship a whole loop package.
The mechanism is the Packaging-Authority standard one, declared by a third-party package as::

    [project.entry-points."bounded_loops.gates"]
    mycompany = "mycompany_gates:gates"

where ``gates`` is a callable returning ``Mapping[str, type]`` — gate kind to a class satisfying
``GatePort``. The kind is what a ``loop.yaml`` names in ``gate.kind``.

This mirrors ``graph/adapters/connectors/provider_plugins.py`` deliberately: that loader survived
the P3 audit, and its four rules are the same four a gate needs. Reinventing them would mean
rediscovering the same holes.

**1. A broken plugin is skipped, never fatal.** An entry point that raises on import or on call is
logged and dropped. ``BaseException``, not ``Exception`` — a plugin calling ``sys.exit()`` must not
take the run down, which is the identical hole the P3 audit found in the provider loader.

**2. Registration is all-or-nothing per plugin.** A plugin offering three gates and one bad one
contributes nothing, rather than leaving an operator with a gate set that depends on import order.

**3. A plugin cannot claim a shipped gate kind.** This closes the supply-chain move where a package
registers ``pytest`` and silently becomes the gate every existing loop already trusts. Enforced
twice on purpose — the loader refuses the name, AND ``merged_gate_registry`` layers plugins UNDER
the shipped set so shipped wins structurally even if the check were wrong.

**4. One distribution cannot supply both a loop's runner and its gate.** ``def:independence``
requires disjoint write authority. A package that provides the worker AND the thing that certifies
the worker is precisely the configuration the soundness argument excludes, and an author cannot be
relied on to notice. See ``same_distribution_refusal``.

**On measurement, stated plainly.** §7 of the paper reports 47 shipped gates that were satisfied by
the ABSENCE of the thing they check. That rate belongs to a specific gate measured against a
specific corpus, so we cannot transfer it to a gate we have never seen. A third-party gate is
therefore installed, usable, and reported UNMEASURED — and ``PLUGIN_GATE_KINDS`` is written by this
loader, never by the plugin, because a package must not be able to describe itself as measured
against a corpus it has never run. A gate can be correct and unmeasured. What it cannot be is
silently credited with someone else's number.

**This is a boundary, not a sandbox.** A gate plugin is arbitrary code in this process. What is
guaranteed is narrower and worth stating exactly: the checks in this module read a snapshot taken
BEFORE any plugin code runs, so they cannot be defeated by mutating what they read, and a plugin's
verdict passes through ``GuardedGate`` before the rules layer sees it.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
import inspect
import logging
import re
from typing import Mapping

from bounded_loops.domain.models import LoopContext, Verdict

_LOGGER = logging.getLogger(__name__)

#: The entry-point group third-party gate packages declare.
GATE_ENTRY_POINT_GROUP = "bounded_loops.gates"

#: Lowercase, hyphen-separated, no leading/trailing/doubled hyphen. The first version of this was
#: ``^[a-z][a-z0-9-]{1,39}$``, which accepted a TRAILING hyphen — caught by its own test, not review.
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_KIND_MAX_LEN = 40


class GatePluginRefused(Exception):
    """One plugin failed validation. Carries a reason an operator can act on.

    A distinct type rather than a reused one: this is the only signal that separates "a plugin is
    wrong" (log and skip, rule 1) from "the engine is wrong" (must surface). Catching a shared
    exception type here would swallow real engine errors as plugin problems.
    """


def _validated_kind(kind: object, *, source: str) -> str:
    if not isinstance(kind, str) or not kind:
        raise GatePluginRefused(f"{source} used a non-string gate kind ({type(kind).__name__})")
    if len(kind) > _KIND_MAX_LEN:
        raise GatePluginRefused(
            f"{source} offers gate kind {kind!r}, longer than {_KIND_MAX_LEN} characters"
        )
    if not _KIND_PATTERN.match(kind):
        raise GatePluginRefused(
            f"{source} offers gate kind {kind!r}; a kind is lowercase alphanumeric with single "
            "internal hyphens (no leading, trailing or doubled hyphen)"
        )
    return kind


def _validated_gate_class(kind: str, offered: object, *, source: str) -> type:
    """A class whose instances can satisfy GatePort — checked on the CLASS, before any instance.

    ``isinstance(obj, GatePort)`` on a runtime_checkable Protocol only checks that ``check`` exists,
    so it would accept a gate whose ``check`` takes the wrong arguments and blow up mid-run instead
    of at load. The signature is inspected here so a mis-shaped gate is refused while there is still
    a clean place to report it.
    """
    if not isinstance(offered, type):
        raise GatePluginRefused(
            f"{source} returned {type(offered).__name__} for gate {kind!r}, not a class"
        )
    check = getattr(offered, "check", None)
    if not callable(check):
        raise GatePluginRefused(f"{source} gate {kind!r} has no callable `check`")
    try:
        params = [
            p for name, p in inspect.signature(check).parameters.items() if name != "self"
        ]
    except (TypeError, ValueError) as exc:  # builtins and C callables have no signature
        raise GatePluginRefused(f"{source} gate {kind!r} has an uninspectable `check`: {exc}")
    required = [
        p for p in params
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if len(required) != 1:
        raise GatePluginRefused(
            f"{source} gate {kind!r} declares `check` with {len(required)} required parameters; "
            "GatePort.check takes exactly one (the LoopContext)"
        )
    return offered


def _load_one(entry: EntryPoint, *, shipped: frozenset[str]) -> Mapping[str, type]:
    """Load and fully validate one plugin, or contribute nothing at all (rule 2)."""
    factory = entry.load()
    offered = factory()
    if not isinstance(offered, Mapping):
        raise GatePluginRefused(
            f"gate plugin {entry.name!r} returned {type(offered).__name__}, not a mapping of "
            "gate kind to gate class"
        )
    source = f"gate plugin {entry.name!r}"
    accepted: dict[str, type] = {}
    for raw_kind, gate_cls in offered.items():
        kind = _validated_kind(raw_kind, source=source)
        if kind in shipped:
            raise GatePluginRefused(
                f"{source} tries to redefine the shipped gate kind {kind!r}. A package may not "
                "silently become the gate existing loops already trust."
            )
        accepted[kind] = _validated_gate_class(kind, gate_cls, source=source)
    return accepted


def load_gate_plugins(
    *,
    shipped: frozenset[str],
    group: str = GATE_ENTRY_POINT_GROUP,
) -> Mapping[str, type]:
    """Every gate offered by installed third-party packages, fail-safe. Never raises.

    ``shipped`` is taken as a frozenset by the CALLER, before this function runs, so plugin code
    that mutates the live registry on import cannot widen what it is allowed to claim.
    """
    discovered: dict[str, type] = {}
    for entry in entry_points(group=group):
        try:
            accepted = _load_one(entry, shipped=shipped)
        except GatePluginRefused as refused:
            _LOGGER.warning("gate plugin %r refused: %s", entry.name, refused)
            continue
        except KeyboardInterrupt:
            raise  # the operator's Ctrl-C, not the plugin's to swallow
        except BaseException as broken:  # noqa: BLE001 — rule 1: never fatal
            _LOGGER.warning(
                "gate plugin %r could not be loaded (%s): %s",
                entry.name, type(broken).__name__, broken,
            )
            continue
        collision = sorted(set(accepted) & set(discovered))
        if collision:
            _LOGGER.warning(
                "gate plugin %r offers gate kind(s) %s already offered by another plugin; "
                "skipping this plugin entirely rather than letting load order decide",
                entry.name, ", ".join(collision),
            )
            continue
        discovered.update(accepted)
    return discovered


def same_distribution_refusal(
    *, gate_kind: str, gate_group: str = GATE_ENTRY_POINT_GROUP, runner_module: str | None,
) -> str | None:
    """Rule 4: the reason to refuse when one distribution supplies both runner and gate.

    Returns a message, or None when the pairing is legitimate. Compared by DISTRIBUTION rather than
    by module name, because a package is free to split its runner and its gate across modules and
    the independence requirement is about who can write, not about file layout.
    """
    if runner_module is None:
        return None
    for entry in entry_points(group=gate_group):
        if entry.name != gate_kind and gate_kind not in (entry.value or ""):
            continue
        gate_dist = getattr(getattr(entry, "dist", None), "name", None)
        if gate_dist is None:
            continue
        runner_root = runner_module.split(".", 1)[0].replace("_", "-").lower()
        if runner_root == gate_dist.replace("_", "-").lower():
            return (
                f"distribution {gate_dist!r} supplies both this loop's runner ({runner_module}) "
                f"and its gate ({gate_kind!r}). An independent gate requires write authority "
                "disjoint from the worker it certifies, so one package cannot be both."
            )
    return None


class GuardedGate:
    """Wraps a third-party gate so a plugin's verdict cannot reach the rules layer unchecked.

    Load-time validation proves a gate has the right SHAPE. This is what constrains its BEHAVIOUR,
    and each rule below exists because the permissive version is a way for a loop to reach DONE
    without the stop condition being met — which is the one outcome the whole design exists to
    prevent.
    """

    def __init__(self, inner: object, *, kind: str) -> None:
        self._inner = inner
        self._kind = kind

    @property
    def gate_kind(self) -> str:
        """The kind this wraps. Read by callers reporting WHICH gate produced a verdict."""
        return self._kind

    def check(self, ctx: LoopContext) -> Verdict:
        try:
            raw = self._inner.check(ctx)  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            raise  # the operator's, not the plugin's to convert into a verdict
        except BaseException as exc:  # noqa: BLE001
            # A gate that raises FAILS. `except Exception` was the first version and a test with
            # SystemExit(1) broke it — the same defect the P3 audit already found in the provider
            # loader. A gate crashing must never be read as "nothing to report, carry on".
            return Verdict(
                passed=False,
                detail=(
                    f"gate {self._kind!r} raised {type(exc).__name__}: {exc}. A gate that cannot "
                    "complete has not confirmed anything, so this lap does not pass."
                ),
                evidence={"gate_kind": self._kind, "error": type(exc).__name__},
            )

        if not isinstance(raw, Verdict):
            return Verdict(
                passed=False,
                detail=(
                    f"gate {self._kind!r} returned {type(raw).__name__}, not a Verdict; a lap "
                    "cannot pass on a value the rules layer cannot read."
                ),
                evidence={"gate_kind": self._kind},
            )

        # `if raw.passed` would accept "yes", 1, [1] — a loop reaching DONE because a field was
        # non-empty. `passed` is documented as "True iff the gate mechanically confirmed the
        # stop_condition", so anything that is not the boolean True is not a confirmation.
        if raw.passed is not True and raw.passed is not False:
            return Verdict(
                passed=False,
                detail=(
                    f"gate {self._kind!r} set passed={raw.passed!r} ({type(raw.passed).__name__}), "
                    "not a bool. A truthy value is not a mechanical confirmation."
                ),
                evidence={"gate_kind": self._kind, "offered_passed": repr(raw.passed)},
            )

        if not (isinstance(raw.detail, str) and raw.detail.strip()):
            # Verdict documents detail as required and non-empty. A passing verdict with no detail
            # leaves the ledger with an unexplainable DONE, which is unreviewable after the fact.
            return Verdict(
                passed=False,
                detail=f"gate {self._kind!r} returned a verdict with no detail",
                evidence={"gate_kind": self._kind},
            )

        return raw


def merged_gate_registry(
    shipped: Mapping[str, type], *, group: str = GATE_ENTRY_POINT_GROUP,
) -> tuple[dict[str, type], frozenset[str]]:
    """``(registry, plugin_kinds)`` — the registry the loop engine uses, and which kinds are foreign.

    Precedence is expressed by ORDER, not only by the loader's name check: plugins go in first and
    shipped overwrites them, so rule 3 holds structurally even if the check above were wrong. Two
    independent mechanisms for the one property worth being certain about.

    ``shipped`` names are snapshotted into a frozenset before ``load_gate_plugins`` runs any plugin
    code, so a plugin that mutates the live registry on import cannot widen what it may claim.

    RETURNS the plugin-kind set rather than rebinding a module global, which is what the first
    version did. A global rebound as a side effect of a function anyone may call leaked state
    between callers — proved by calling it once and watching a later reader see the previous
    caller's plugins. The caller now owns the value, so there is nothing to leak.

    The set is derived HERE and never taken from a plugin: a package must not be able to describe
    itself as measured against a corpus it has never run. A third-party gate is installed, usable,
    and reported UNMEASURED.
    """
    shipped_names = frozenset(shipped)
    merged: dict[str, type] = dict(load_gate_plugins(shipped=shipped_names, group=group))
    plugin_kinds = frozenset(merged) - shipped_names
    merged.update(shipped)
    return merged, plugin_kinds


def instantiate_guarded(kind: str, gate_cls: type, gate_extra: Mapping[str, object]) -> GuardedGate:
    """Construct a third-party gate and wrap it, or refuse the loop. Never returns unwrapped.

    Lives here rather than in ``composition`` for two reasons. It belongs beside ``GuardedGate``,
    whose invariant it upholds — there is no path that builds a plugin gate and forgets to wrap it.
    And ``composition`` sits under an 800-line cap enforced by ``test_no_module_exceeds_the_line_cap``,
    which this feature pushed it over; the cap is a real rule, so the code moved rather than the cap.

    FAILS CLOSED on a constructor that raises. A gate that cannot be built means the loop has no
    independent gate, and running ungated is the single outcome the whole design exists to prevent —
    so refusing is both louder and safer than proceeding.
    """
    try:
        instance = gate_cls(**gate_extra)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — a plugin constructor must not kill the run
        raise GatePluginRefused(
            f"gate.kind {kind!r} is provided by a third-party package whose gate could not be "
            f"constructed ({type(exc).__name__}: {exc}). Refusing the loop rather than running it "
            "without an independent gate."
        ) from exc
    return GuardedGate(instance, kind=kind)
