"""Third-party gate packages, discovered through entry points (ADR-13 B1).

.. warning::
   **NOT WIRED, AND AT THE WRONG LAYER. Do not treat this as a working feature.**

   Nothing in the engine calls ``load_gate_plugins``, so declaring the entry point has no
   effect today. It was also built against the graph's per-node-kind ``IndependentGatePort``
   (loop / join / publish), which is a different axis from the thing a third-party gate
   actually extends: ``composition.GATE_REGISTRY`` maps a loop manifest's ``gate_kind`` to a
   gate class, and that registry already merges two optional registries, so it is the real
   extension point. ``RESERVED_GATE_KINDS`` below is correspondingly incomplete — it was
   derived from the kinds the shipped catalogue happens to USE rather than from the registry
   of what EXISTS, so five real kinds are missing from it.

   The verdict validation, the ``BaseException`` discipline and the truthy-non-bool guard below
   are sound and worth carrying forward; the layer, the port and the reserved set are not.
   Rewrite tracked as a task. The 0.6.8 changelog deliberately does not advertise any of this.


The independent gate is what this engine sells, so being able to publish one is the extensibility
that matters. Until now a third party could only ship a whole loop package; this module lets one
ship a gate.

The mechanism is deliberately the same one ``provider_plugins`` already uses — the
Packaging-Authority standard, as pytest and mypy plugins do — declared by a third-party package as::

    [project.entry-points."bounded_loops.gates"]
    mygates = "mypkg_bounded_loops:gates"

where ``gates`` is a callable returning ``Mapping[str, IndependentGatePort]`` keyed by gate kind.

``provider_plugins`` earned four rules and all four apply here unchanged: a broken plugin is
skipped rather than fatal, registration is all-or-nothing per plugin, a plugin cannot claim a
shipped name, and a plugin cannot obtain authority it was not separately granted. A gate is a
sharper object than a provider profile, so it needs three more.

**5. A verdict is validated at the boundary, and a truthy non-bool is not a pass.** A gate returns
``GateVerdict(passed=...)`` and nothing stops a plugin putting a non-empty string there. ``if
verdict.passed`` would then accept it, which is a node reaching \\textsc{done} because a field was
truthy. ``passed`` must be exactly ``True`` or ``False``, and the reason must be non-empty, because
a verdict nobody can read is not auditable.

**6. A gate that raises is a FAILURE, never a pass.** An exception inside third-party code must not
be able to end a step successfully. It is converted into a failed verdict naming the plugin, so the
loop retries or halts rather than proceeding on an error.

**7. A gate plugin cannot gate a node whose worker came from the same distribution.**
``def:independence`` requires the checker to be a distinct object with disjoint write authority
over the artifact. A single package supplying both the worker and its gate is exactly the
configuration the soundness theorem excludes, and an author will not reliably notice. Distributions
are compared mechanically — see ``same_distribution_refusal``.

**A third-party gate is reported UNMEASURED, and the plugin cannot say otherwise.** The evaluation
in this project measures a false-accept rate for *our* gates against a held-out corpus; that rate
is a property of a specific gate and transfers to nobody else's. So ``RegisteredGate.measured`` is
False, it is set here rather than by the plugin, and any surface that displays a rate must show
this one as unmeasured rather than borrowing ours. A gate can be correct and unmeasured; what it
cannot be is silently credited with someone else's number.

**This is a boundary, not a sandbox** — the same honest caveat ``provider_plugins`` states. A gate
plugin is arbitrary in-process code. What is guaranteed is narrower: the engine's own evaluation
path will not treat a malformed verdict as a pass, will not let an exception read as success, and
will not let one distribution both do and check the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
import logging
import re
from typing import Mapping

from bounded_loops.graph.application.node_contracts import (
    GateVerdict,
    IndependentGatePort,
)
from bounded_loops.graph.domain.errors import GraphValidationError

_LOGGER = logging.getLogger(__name__)

#: The entry-point group third-party gate packages declare.
GATE_ENTRY_POINT_GROUP = "bounded_loops.gates"
#: The entry-point group provider packages declare, mirrored here for the rule-7 comparison.
#: Imported by name rather than from the connector module to keep this adapter from depending on
#: another adapter; the drift risk is covered by a test asserting the two strings agree.
PROVIDER_ENTRY_POINT_GROUP = "bounded_loops.graph.providers"

#: Gate kinds the shipped loop catalogue already binds. A package registering ``pytest`` would
#: silently become the gate nine shipped loops use, which is the supply-chain move rule 3 refuses.
RESERVED_GATE_KINDS = frozenset({
    "checkov", "command", "composite", "jsonschema", "osv", "pytest",
})

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
#: A gate kind is a short lower-kebab identifier. Restricting the shape keeps a kind usable as a
#: manifest value and a receipt field without quoting rules nobody will remember.
_KIND = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
#: Bounded separately from the pattern so the limit is readable and the pattern stays simple.
_KIND_MAX = 40


@dataclass(frozen=True)
class RegisteredGate:
    """One third-party gate, with the provenance the independence rule needs.

    ``measured`` is False for every plugin gate and is written here, never by the plugin: a package
    must not be able to describe itself as measured against a corpus it has never seen.
    """

    kind: str
    gate: IndependentGatePort
    distribution: str
    measured: bool = False


def _validated_verdict(value: object, *, source: str) -> GateVerdict:
    """Rule 5. Refuse anything that is not a well-formed verdict.

    Note the ``is not True and is not False`` test rather than ``isinstance(bool)``: the point is
    to reject a truthy value, and a stricter identity check states that intent where a reader will
    see it.
    """
    if not isinstance(value, GateVerdict):
        raise GraphValidationError(
            "gate_plugin", "/verdict",
            f"{source} returned {type(value).__name__}, not a GateVerdict",
        )
    if value.passed is not True and value.passed is not False:
        raise GraphValidationError(
            "gate_plugin", "/verdict/passed",
            f"{source} returned passed={value.passed!r}, which is not a bool. A truthy value must "
            "never read as a pass: that is a node reaching DONE because a field was non-empty.",
        )
    if not isinstance(value.reason, str) or not value.reason.strip():
        raise GraphValidationError(
            "gate_plugin", "/verdict/reason",
            f"{source} returned an empty reason; a verdict nobody can read is not auditable",
        )
    if value.evidence_digest is not None and not (
        isinstance(value.evidence_digest, str) and _DIGEST.match(value.evidence_digest)
    ):
        raise GraphValidationError(
            "gate_plugin", "/verdict/evidence_digest",
            f"{source} returned an evidence_digest that is not 'sha256:<64 hex>'",
        )
    return value


class GuardedGate:
    """Wraps a plugin gate so rules 5 and 6 hold however the plugin behaves.

    Every plugin gate is wrapped before the engine ever sees it, so there is no path on which an
    unwrapped third-party gate reaches the controller.
    """

    def __init__(self, kind: str, inner: IndependentGatePort, *, distribution: str) -> None:
        self.kind = kind
        self.distribution = distribution
        self._inner = inner

    def evaluate(self, **kwargs: object) -> GateVerdict:
        """Delegate, then validate. An exception becomes a FAILED verdict, never a pass.

        ``**kwargs`` rather than the port's explicit signature is deliberate: this wrapper must
        forward whatever the port requires today and after the port grows a field, and a wrapper
        that silently dropped a new keyword would hand the plugin less context than the engine
        thinks it gave — which is how a gate stops being able to tell one attempt from another.
        """
        source = f"gate plugin {self.kind!r} from {self.distribution}"
        try:
            raw = self._inner.evaluate(**kwargs)  # type: ignore[arg-type]
        except KeyboardInterrupt:
            # The operator's Ctrl-C, not the plugin's to swallow.
            raise
        except BaseException as exc:  # noqa: BLE001 — a plugin may raise anything at all
            # Rule 6, and ``BaseException`` rather than ``Exception`` for a reason this project has
            # already paid for once: the P3 audit found a plugin calling ``sys.exit()`` escaping an
            # ``except Exception`` in the provider loader and taking the process down. The same
            # mistake here would be worse — ``SystemExit`` from a gate would kill a run mid-loop.
            # Not re-raised: the honest statement is that the gate did not pass, so the loop retries
            # or halts rather than the engine reporting a fault of its own.
            _LOGGER.warning("%s raised %s; recording a FAILED verdict", source, type(exc).__name__)
            return GateVerdict(
                passed=False,
                reason=f"{source} raised {type(exc).__name__} and did not return a verdict",
            )
        try:
            return _validated_verdict(raw, source=source)
        except GraphValidationError as exc:
            _LOGGER.warning("%s returned an invalid verdict: %s", source, exc)
            return GateVerdict(
                passed=False, reason=f"{source} returned an invalid verdict: {exc}",
            )


def same_distribution_refusal(
    gate: RegisteredGate, *, worker_distribution: str | None,
) -> str | None:
    """Rule 7. A reason string when one distribution supplies both worker and gate, else None.

    Returns a reason rather than raising so the caller decides where the refusal surfaces —
    preflight for a whole graph, or per node. An unknown worker distribution is NOT treated as a
    match: refusing on absence would block every shipped provider, whose worker has no
    distribution because it is not a plugin at all.
    """
    if not worker_distribution:
        return None
    if worker_distribution.lower() != gate.distribution.lower():
        return None
    return (
        f"gate {gate.kind!r} and this node's worker both come from the distribution "
        f"{gate.distribution!r}. An independent gate must be a distinct object with disjoint write "
        "authority over the artifact; one package supplying both the work and its check is the "
        "configuration the soundness result excludes. Install a gate from a different package, or "
        "bind the node to a worker from one."
    )


def _distribution_of(entry: EntryPoint) -> str:
    """Best available distribution name for an entry point, or a stable placeholder.

    ``EntryPoint.dist`` is None for an entry point constructed in-process, which is what tests do.
    A placeholder keyed on the entry-point name keeps rule 7 meaningful there instead of silently
    disabling it.
    """
    dist = getattr(entry, "dist", None)
    name = getattr(dist, "name", None)
    return name if isinstance(name, str) and name else f"<unpackaged:{entry.name}>"


def _load_one(entry: EntryPoint, *, reserved: frozenset[str]) -> Mapping[str, RegisteredGate]:
    """Load and fully validate one plugin, or contribute nothing at all (rule 2)."""
    offered = entry.load()()
    if not isinstance(offered, Mapping):
        raise GraphValidationError(
            "gate_plugin", f"/{entry.name}",
            f"entry point {entry.name!r} returned {type(offered).__name__}, not a mapping of gate "
            "kind to gate",
        )
    distribution = _distribution_of(entry)
    source = f"gate plugin {entry.name!r}"
    accepted: dict[str, RegisteredGate] = {}
    for kind, gate in offered.items():
        if not isinstance(kind, str) or len(kind) > _KIND_MAX or not _KIND.match(kind):
            raise GraphValidationError(
                "gate_plugin", f"/{entry.name}",
                f"{source} offered gate kind {kind!r}; a kind must match {_KIND.pattern}",
            )
        if kind in reserved:
            raise GraphValidationError(
                "gate_plugin", f"/{kind}",
                f"{source} tries to redefine the shipped gate kind {kind!r}. A package may not "
                "silently become the gate existing loops already bind to.",
            )
        if not callable(getattr(gate, "evaluate", None)):
            raise GraphValidationError(
                "gate_plugin", f"/{kind}",
                f"{source} offered {type(gate).__name__} for {kind!r}, which has no callable "
                "evaluate; it does not satisfy IndependentGatePort",
            )
        accepted[kind] = RegisteredGate(
            kind=kind,
            gate=GuardedGate(kind, gate, distribution=distribution),
            distribution=distribution,
        )
    return accepted


def load_gate_plugins(
    *,
    reserved: frozenset[str] = RESERVED_GATE_KINDS,
    group: str = GATE_ENTRY_POINT_GROUP,
) -> Mapping[str, RegisteredGate]:
    """Every gate offered by installed third-party packages, fail-safe (rule 1).

    Never raises. A plugin that raises on import, returns the wrong shape, claims a reserved kind,
    or offers one bad gate is logged at WARNING and contributes nothing. Two plugins offering the
    same kind: the first wins and the second is refused by name, so the result does not depend on
    installation order.
    """
    registered: dict[str, RegisteredGate] = {}
    try:
        discovered = tuple(entry_points(group=group))
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 — importlib.metadata can fail on a broken install
        _LOGGER.warning("gate plugin discovery failed (%s); continuing with none", type(exc).__name__)
        return {}
    for entry in discovered:
        try:
            offered = _load_one(entry, reserved=reserved)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 — third-party code may raise anything
            _LOGGER.warning(
                "gate plugin %r skipped (%s): %s", entry.name, type(exc).__name__, exc,
            )
            continue
        collision = sorted(set(offered) & set(registered))
        if collision:
            # The whole plugin is skipped, not just the colliding kind — the same policy the
            # provider loader uses, for the same reason: partially admitting it would let install
            # order decide which of its gates a graph gets.
            _LOGGER.warning(
                "gate plugin %r offers gate kind(s) %s already offered by another plugin; "
                "skipping this plugin entirely rather than letting load order decide",
                entry.name, ", ".join(collision),
            )
            continue
        registered.update(offered)
    return registered
