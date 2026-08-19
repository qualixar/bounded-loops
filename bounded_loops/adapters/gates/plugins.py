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

**3. A plugin cannot claim a shipped gate kind — by NAME.** The loader refuses the name and
``merged_gate_registry`` layers plugins UNDER the shipped set, so a package cannot register
``pytest`` and become the kind existing loops name.

Stated narrowly on purpose. Three independent audits showed that a plugin CAN still replace the
class behind a shipped kind, and no amount of registry hardening prevents it: freeze the dict and
the module attribute is rebound; stop the rebind and the class is monkey-patched; and for
first-class kinds ``_build_gate`` reads a BARE MODULE GLOBAL — ``return PytestGate(...)`` — so the
registry is not even consulted. All three were reproduced live.

So this rule is anti-collision, not containment, and the registries being frozen is a convenience
rather than a boundary. The property that actually holds is rule 5.

**4. One distribution cannot supply both a loop's runner and its gate.** ``def:independence``
requires disjoint write authority. A package that provides the worker AND the thing that certifies
the worker is precisely the configuration the soundness argument excludes, and an author cannot be
relied on to notice. See ``same_distribution_refusal``.

**5. Every verdict is checked, whoever produced it.** ``composition._instantiate_gate`` wraps EVERY
gate in ``GuardedGate`` — shipped and third-party alike — so a raise becomes FAIL, a non-boolean pass
is not a pass, and a passing verdict with no detail is refused. This is the invariant that survives
an in-process adversary, because it inspects the VERDICT and never asks who produced it: a hijacked
``CommandGate`` returning ``passed="yes"`` is refused by the same code that refuses a third party's.

It also hardens our own catalogue, which is the larger prize. A shipped gate with a bug that returns
a non-bool, or that raises, previously reached the rules layer unchecked.

**On measurement, stated plainly.** §7 of the paper reports 47 shipped gates that were satisfied by
the ABSENCE of the thing they check. That rate belongs to a specific gate measured against a
specific corpus, so we cannot transfer it to a gate we have never seen. A third-party gate is
therefore installed, usable, and reported UNMEASURED — and ``PLUGIN_GATE_KINDS`` is written by this
loader, never by the plugin, because a package must not be able to describe itself as measured
against a corpus it has never run. A gate can be correct and unmeasured. What it cannot be is
silently credited with someone else's number.

**This is a boundary, not a sandbox.** A gate plugin is arbitrary code in this process: it can
monkey-patch the engine, rebind module attributes, or replace a shipped gate class outright. Nothing
here stops that and nothing here claims to — the first version of this docstring implied otherwise
and the audits were right to call it.

What IS guaranteed, exactly: the checks in this module read a snapshot taken BEFORE any plugin code
runs, so they cannot be defeated by mutating what they read; and every verdict reaching the rules
layer has been through ``GuardedGate`` regardless of which class produced it. The worst a hostile
plugin achieves is a refusal or a wrapped gate — not an unchecked pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points, packages_distributions
from types import MappingProxyType
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


@dataclass(frozen=True)
class LoadedPlugins:
    """What discovery produced: the gates, and which distribution supplied each kind.

    The distribution map is recorded from ``entry.dist`` HERE, at load time, because that is the only
    place the association is known for certain. The first version tried to recover it later by
    checking whether the gate kind appeared in ``entry.value`` — but a real entry point is
    ``name="acme", value="acme_gates:gates"`` for a kind like ``invoice-check``, so the kind is never
    in either field and the check matched nothing at all.
    """

    gates: Mapping[str, type]
    distributions: Mapping[str, str]


def load_gate_plugins(
    *,
    shipped: frozenset[str],
    group: str = GATE_ENTRY_POINT_GROUP,
) -> LoadedPlugins:
    """Every gate offered by installed third-party packages, fail-safe. Never raises.

    ``shipped`` is taken as a frozenset by the CALLER, before this function runs, so plugin code
    that mutates the live registry on import cannot widen what it is allowed to claim.

    ``entry_points()`` is INSIDE the try: enumerating installed metadata touches the filesystem, so a
    corrupt or unreadable ``.dist-info`` raises OSError there. With that call outside, "never raises"
    was false in the worst place — this runs at ``import bounded_loops.composition``, so the traceback
    landed on any user with damaged metadata, whether or not they had a gate plugin at all.
    """
    discovered: dict[str, type] = {}
    distributions: dict[str, str] = {}
    try:
        entries = list(entry_points(group=group))
    except KeyboardInterrupt:
        raise
    except BaseException as broken:  # noqa: BLE001 — discovery itself must not be fatal
        _LOGGER.warning(
            "could not enumerate %r entry points (%s): %s; no gate plugins loaded",
            group, type(broken).__name__, broken,
        )
        return LoadedPlugins(gates=MappingProxyType({}), distributions=MappingProxyType({}))

    for entry in entries:
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
        # INSIDE the loop's guarded region, not after it. `entry.dist` is a property that reads
        # installed metadata, so it raises OSError on a damaged .dist-info — and this is the THIRD
        # place in this feature where an exception escaped a line I had just widened for a different
        # type. A missing distribution name costs only rule 4's precision for that one kind; letting
        # it abort discovery costs every plugin on the host.
        try:
            dist_name = getattr(getattr(entry, "dist", None), "name", None)
        except KeyboardInterrupt:
            raise
        except BaseException as broken:  # noqa: BLE001
            _LOGGER.warning(
                "gate plugin %r loaded but its distribution name is unreadable (%s: %s); "
                "the independence check cannot use it",
                entry.name, type(broken).__name__, broken,
            )
            dist_name = None
        if dist_name is not None:
            for kind in accepted:
                distributions[kind] = dist_name
    return LoadedPlugins(
        gates=MappingProxyType(discovered), distributions=MappingProxyType(distributions),
    )


def _pep503(name: str) -> str:
    """PEP 503 normalised name. ``acme.gates``, ``acme_gates`` and ``Acme-Gates`` are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def same_distribution_refusal(
    *, gate_kind: str, gate_distributions: Mapping[str, str], worker_module: str | None,
) -> str | None:
    """Rule 4: refuse when one distribution supplies both the loop's worker and its gate.

    ``def:independence`` requires write authority disjoint from the worker, so a package that
    provides the thing being certified AND the thing certifying it is the configuration the soundness
    argument excludes. An author cannot be relied on to notice.

    Compared BY DISTRIBUTION, resolved through ``packages_distributions()``, which is the real
    module-to-project map the installer wrote. Three defects in the first version came from doing this
    with string surgery instead: it matched on entry-point name and value so it never fired; it
    compared ``type(runner).__module__``, which for every shipped runner is
    ``bounded_loops.adapters.runners.*`` and so could never match a third party; and normalising by
    hand made ``acme.gates`` differ from ``acme_gates`` while letting an unrelated ``posix-ipc``
    package be blamed for a gate kind called ``os``.

    SCOPE, STATED PLAINLY BECAUSE IT IS NARROWER THAN THE RULE'S NAME SUGGESTS. This decides exactly
    one configuration: a ``python_callable`` loop whose ``runner.module_path`` resolves to a single
    distribution that also supplies the gate. For ``shell``, ``agent_cmd``, ``docker``, ``worktree``
    and every credentialed runner, the worker is an opaque subprocess with no distribution to compare,
    so this returns None — nothing was checked, because nothing is checkable from here. A gate author
    reading "one distribution cannot supply both" would over-trust that, so the docs must say
    python_callable and not imply general coverage. Detecting an ``agent_cmd`` binary's owning project
    would extend the rule; it is not attempted and is not claimed.
    """
    if worker_module is None:
        return None
    gate_dist = gate_distributions.get(gate_kind)
    if gate_dist is None:
        return None  # a shipped gate, or a plugin whose distribution metadata was unreadable
    try:
        worker_dists = packages_distributions().get(worker_module.split(".", 1)[0], [])
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001
        # Any metadata failure, not just OSError. `packages_distributions()` reads every installed
        # METADATA file, so a non-UTF-8 one raises UnicodeDecodeError and an empty .dist-info raises
        # TypeError — both verified by the audit, neither an OSError. Damaged metadata is NOT evidence
        # of a violation, and a traceback out of `wire()` is a worse outcome than an unchecked rule.
        return None
    # EXACTLY ONE provider, or no refusal. `packages_distributions()` maps a top-level name to EVERY
    # distribution that ships under it, so a namespace package makes unrelated siblings look identical:
    # `jaraco.something` and a `jaraco.context` gate both resolve to the `jaraco` root, and `google.*`
    # is shared by protobuf, google-auth and google-api-core. Set membership therefore refused loops
    # whose worker and gate came from genuinely different projects — proved on this host by two
    # auditors. Ambiguity now FAILS OPEN, because wrongly blocking a legitimate loop is worse than
    # not enforcing a rule the metadata cannot decide.
    if len(worker_dists) != 1:
        return None
    if _pep503(gate_dist) == _pep503(worker_dists[0]):
        return (
            f"distribution {gate_dist!r} supplies both this loop's worker module "
            f"({worker_module}) and its gate ({gate_kind!r}). An independent gate requires write "
            "authority disjoint from the worker it certifies, so one package cannot be both."
        )
    return None


class GuardedGate:
    """Wraps a third-party gate so a plugin's verdict cannot reach the rules layer unchecked.

    Load-time validation proves a gate has the right SHAPE. This is what constrains its BEHAVIOUR,
    and each rule below exists because the permissive version is a way for a loop to reach DONE
    without the stop condition being met — which is the one outcome the whole design exists to
    prevent.
    """

    __slots__ = ("_frozen", "_inner", "_kind")

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Subclassing is refused, because a subclass WAS the bypass.

        Round 4: `_instantiate_gate` shortcut `isinstance(built, GuardedGate)` to avoid double
        wrapping. A hostile gate class that subclasses this one therefore skipped every check
        below — reproduced returning passed="yes", a truthy non-bool, i.e. a loop reaching DONE
        with nothing verified. That call site now tests the exact type, and this makes the vector
        itself unconstructible so a future call site cannot reintroduce it by reaching for the
        more natural-looking `isinstance`.
        """
        raise TypeError(
            "GuardedGate cannot be subclassed: a subclass is indistinguishable from the wrapper "
            "under isinstance, which is how an unvalidated verdict reached the rules layer. Wrap "
            "an instance instead of inheriting from it."
        )

    def __init__(self, inner: object, *, kind: str) -> None:
        self._inner = inner
        self._kind = kind
        self._frozen = True

    def __setattr__(self, name: str, value: object) -> None:
        """Refuse rebinding after construction, so `gate._inner = Evil()` is not a one-liner.

        NOT containment, and must not be described as such: `object.__setattr__` bypasses this, and
        a determined plugin can reach the wrapper through `gc.get_referrers`. It is the same honest
        limit the registry-freezing attempt ran into — in-process policing of a hostile object in the
        same interpreter is unwinnable. What this buys is that the CASUAL and ACCIDENTAL rebind now
        fails loudly instead of silently swapping the gate a verdict is read from.
        """
        if getattr(self, "_frozen", False):
            raise AttributeError(
                f"GuardedGate is frozen after construction; refusing to rebind {name!r}. "
                "Construct a new wrapper rather than mutating the gate a verdict comes from."
            )
        object.__setattr__(self, name, value)

    @property
    def gate_kind(self) -> str:
        """The kind this wraps. Read by callers reporting WHICH gate produced a verdict."""
        return self._kind

    @property
    def wraps(self) -> object:
        """The gate underneath, for diagnostics and provenance — never to bypass the checks.

        Added because universal wrapping is now unconditional, so ``type(gate).__name__`` no longer
        names the gate that ran; a caller that needs to report WHAT executed has to be able to ask.
        The receipt surface needs exactly this: a verdict is only reviewable if it says which gate,
        from which distribution, produced it.

        Reading this to call ``.check()`` directly would defeat the wrapper. Nothing in the engine
        does, and a reviewer seeing it should treat it as a defect.
        """
        return self._inner

    def check(self, ctx: LoopContext) -> Verdict:
        """Every raise below this line becomes a FAILING verdict, including raises from VALIDATION.

        Round 4: only `self._inner.check(ctx)` used to sit inside the try, so the validation that
        follows — `isinstance`, the `passed` bool test, `detail.strip()` — ran unprotected. A Verdict
        subclass whose `passed` property raises therefore escaped this wrapper entirely. That is
        availability rather than a forged pass, but a gate crashing the harness from inside the code
        that exists to contain it is not a distinction worth shipping.
        """
        try:
            return self._checked(ctx)
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

    def _validate(self, raw: object) -> Verdict:
        """Reject anything that is not an unambiguous mechanical confirmation.

        Called from inside `check`'s try, so a Verdict whose fields raise on access becomes a
        failing verdict rather than an exception escaping the wrapper.
        """
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

        if raw.passed and not (isinstance(raw.detail, str) and raw.detail.strip()):
            # Only a PASSING verdict is blocked, which is what the documented rule says. A passing
            # verdict with no detail leaves the ledger with an unexplainable DONE — unreviewable
            # after the fact. A FAILING verdict with a thin detail is merely unhelpful, and
            # converting it to a different failure would discard the gate's own reason for nothing.
            return Verdict(
                passed=False,
                detail=f"gate {self._kind!r} passed but returned no detail, so nothing explains it",
                evidence={"gate_kind": self._kind},
            )

        return raw

    def _checked(self, ctx: LoopContext) -> Verdict:
        """The gate call and the validation of what it returned, as one guarded unit."""
        raw = self._inner.check(ctx)  # type: ignore[attr-defined]
        return self._validate(raw)


def merged_gate_registry(
    shipped: Mapping[str, type], *, group: str = GATE_ENTRY_POINT_GROUP,
) -> "LoadedGates":
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
    # SNAPSHOT THE CLASSES, not just the names, and do it BEFORE any plugin code runs.
    # `shipped` is a live dict the caller still holds, and `load_gate_plugins` executes
    # third-party factories. The first version snapshotted only the NAME set and then merged the
    # live dict back, so a plugin that popped a shipped key during load defeated the merge — the
    # name check passed while the class went missing or was replaced.
    snapshot: dict[str, type] = dict(shipped)
    shipped_names = frozenset(snapshot)

    loaded = load_gate_plugins(shipped=shipped_names, group=group)
    merged: dict[str, type] = dict(loaded.gates)
    plugin_kinds = frozenset(merged) - shipped_names
    merged.update(snapshot)  # the SNAPSHOT wins, so shipped classes are the ones taken pre-plugin

    # Returned FROZEN. `MappingProxyType` is a view, so the backing dict must be unreachable for the
    # freeze to mean anything — `merged` is a local, and the proxy is its only remaining reference
    # once this returns. `_instantiate_gate` reads shipped classes straight out of this registry
    # WITHOUT GuardedGate, so a mutable one is a path to an unchecked verdict.
    return LoadedGates(
        registry=MappingProxyType(merged),
        plugin_kinds=plugin_kinds,
        distributions=loaded.distributions,
    )


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


@dataclass(frozen=True)
class LoadedGates:
    """The gate registry the loop engine uses, plus what a caller needs to police it.

    A dataclass rather than a tuple because three values were already one too many to read at a call
    site, and rule 4 needs the distribution map that a two-tuple had nowhere to put — which is part of
    why the first version tried to re-derive it from entry-point strings and got it wrong.
    """

    registry: Mapping[str, type]
    plugin_kinds: frozenset[str]
    distributions: Mapping[str, str]


def refuse_if_same_distribution(
    *,
    gate_kind: str,
    plugin_kinds: frozenset[str],
    gate_distributions: Mapping[str, str],
    worker_module: str | None,
) -> None:
    """Rule 4 as a guard: raise ``GatePluginRefused`` or return. Shipped kinds are skipped.

    Lives here rather than in ``composition`` because that module is under an 800-line cap enforced
    by ``test_no_module_exceeds_the_line_cap``, and this feature pushed it over twice. The cap is
    doing its job: gate concerns belong with the gates.
    """
    if gate_kind not in plugin_kinds:
        return  # a shipped gate cannot belong to a third party's distribution
    refusal = same_distribution_refusal(
        gate_kind=gate_kind, gate_distributions=gate_distributions, worker_module=worker_module,
    )
    if refusal is not None:
        raise GatePluginRefused(f"gate.kind {gate_kind!r} cannot certify this loop: {refusal}")


def guarded_child_or_none(
    *, child_kind: object, plugin_kinds: frozenset[str], registry: Mapping[str, type],
    child_config: Mapping[str, object],
    gate_distributions: Mapping[str, str] = MappingProxyType({}),
    worker_module: str | None = None,
) -> GuardedGate | None:
    """A wrapped composite CHILD when the kind is third-party, else None so shipped branches run.

    Wrapping matters doubly under composite: an unchecked child verdict is aggregated into the
    parent's, so a truthy non-bool from one child could carry the whole composite to a pass.
    """
    if not (isinstance(child_kind, str) and child_kind in plugin_kinds):
        return None
    # A kind that is declared a plugin but absent from the registry means the two disagree — a polluted
    # `_PLUGIN_GATE_KINDS` or a partial reload. Refuse it; `registry[child_kind]` raised a bare
    # KeyError straight past `wire()`'s handler as a traceback.
    gate_cls = registry.get(child_kind)
    if gate_cls is None:
        raise GatePluginRefused(
            f"gate kind {child_kind!r} is registered as third-party but is not in the gate registry; "
            "the plugin set and the registry disagree, so this loop is refused rather than guessed at"
        )
    if worker_module is not None or gate_distributions:
        # Rule 4 applies to a CHILD exactly as to a top-level gate. It did not, so a same-distribution
        # gate slipped through by being wrapped in a composite — the one place the check was most
        # needed, since the parent aggregates the child's verdict.
        refuse_if_same_distribution(
            gate_kind=child_kind, plugin_kinds=plugin_kinds,
            gate_distributions=gate_distributions, worker_module=worker_module,
        )
    extra = {k: v for k, v in child_config.items() if k != "kind"}
    return instantiate_guarded(child_kind, gate_cls, extra)
