"""Local-CLI egress policy — where the deployment's resolved egress posture meets a
local_cli connector node's actual runtime capability (Slice 2 wiring).

GROUND TRUTH (verified by reading ``local_cli_worker.py`` in full before writing this):
``LocalCliConnectorWorker`` runs the user's agent CLI UNWRAPPED — no Seatbelt profile, no
loopback egress proxy — and carries its own "defense in depth" guard that refuses any
envelope but ``NetworkMode.OPEN``. It deliberately inherits the operator's REAL environment
(real ``HOME``/``TMPDIR``) so the CLI finds its own subscription-login config; that is in
direct tension with the OS cage's isolated-``HOME`` requirement, so wiring real Seatbelt +
egress-proxy support into this worker is a substantial, separate feature — not something
this module invents.

So for a plan containing a ``local_cli`` node, only ``open`` (today's only, and still the
documented default) posture is honored:

* ``open``      — unaffected; byte-for-byte today's hardcoded behavior.
* ``allowlist`` — refused UNCONDITIONALLY, before host capabilities are even consulted. This
  is deliberately NOT capability-gated: checking Seatbelt/egress-proxy availability first
  would wrongly imply "get a Mac with Seatbelt and this works" — it would not, because the
  worker itself has no cage-wrapping integration yet, on ANY host.
* ``broker``    — refused: a local_cli node's subscription CLI authenticates out-of-band and
  talks to its own vendor over its own TLS; the no-secret ``EgressBroker`` (a lease bound to
  one declared destination / method / effect — see ``egress_broker.py``) has nothing to
  mediate. See ``docs/graph-egress-posture.md`` for the full architectural resolution.

A plan with NO local_cli node is entirely unaffected — including the ALLOWLIST host-
capability check: it is skipped entirely (not merely "not refused"), so an https-only run's
success never depends on a fact (Seatbelt/egress-proxy availability) that has nothing to do
with how https actually works. ``https`` nodes have their own, independent, per-node
``ALLOWLIST`` construction in ``execute_graph.py::_build_policy`` that this module never
touches. Only the syntactic validity of the configured posture value itself (e.g. a garbage
env var) is still checked regardless of transport — that is a deployment-level configuration
error, not a transport-specific capability question.

Every refusal here is a ``GraphValidationError`` raised BEFORE ``controller.run()`` is called
(from ``build_execution_controller``, which every caller already wraps in
``except GraphValidationError`` — see ``execute_graph_run`` and
``LocalGraphRuntimeFacade.resume``/``.approve``), so it always surfaces as a clean, actionable
refusal — never a mid-run traceback, never a silent downgrade to OPEN.
"""

from __future__ import annotations

from typing import Mapping

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.adapters.enforcement.egress_posture import (
    EgressPosture,
    EgressPostureDecision,
    decide_egress_posture,
    resolve_egress_posture,
)
from bounded_loops.graph.application.run_graph import is_egress_node
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan

_LOCAL_CLI_TRANSPORTS = frozenset({"local_cli"})


def resolve_local_cli_egress_decision(
    plan: ExecutionPlan,
    *,
    environ: Mapping[str, str] | None = None,
    capabilities: PlatformCapabilities | None = None,
) -> EgressPostureDecision:
    """Resolve the deployment's egress posture ONCE and apply it to *plan*'s local_cli
    node(s), or raise a clean, actionable ``GraphValidationError`` if this plan's local_cli
    node(s) cannot honor the selected posture. See the module docstring for the exact rules.

    ``environ`` and ``capabilities`` both default to production values (``os.environ`` via
    ``resolve_egress_posture``'s own default; a real platform probe) when omitted, mirroring
    this codebase's "inject or probe" convention everywhere else in the enforcement layer.
    """
    egress_config = resolve_egress_posture(environ=environ)  # always resolved: a garbage config
    # value must fail fast regardless of transport — but nothing else below may.
    has_local_cli = any(is_egress_node(plan, node, _LOCAL_CLI_TRANSPORTS) for node in plan.nodes)

    if not has_local_cli:
        # Nothing in this plan consumes a local_cli egress decision — https (and DENY) nodes
        # have their own independent construction in execute_graph.py::_build_policy. Do NOT
        # proceed to the capability-dependent ALLOWLIST check below: it answers a local_cli-
        # specific question (can THIS host cage a local_cli subprocess), and applying it to a
        # plan that has no local_cli node would make an UNRELATED transport's success depend
        # on a fact that has nothing to do with how it actually works — exactly the "posture
        # leaking into https" failure mode this wiring must not create.
        return EgressPostureDecision(
            posture=egress_config.posture, network_mode=None, network_destinations=(),
            requires_broker=False, rationale="no local_cli node in this plan; posture not applied",
        )

    if egress_config.posture is EgressPosture.BROKER:
        raise GraphValidationError(
            "egress_posture", "/egress/posture",
            "BROKER egress posture cannot mediate a local_cli connector node's egress: a "
            "subscription CLI authenticates out-of-band and talks to its own vendor over its "
            "own TLS, and the no-secret EgressBroker (a lease bound to one declared "
            "destination/method/effect) has nothing to mediate — select OPEN egress posture "
            "for a graph containing a local_cli node, or remove it to use BROKER",
        )
    if egress_config.posture is EgressPosture.ALLOWLIST:
        raise GraphValidationError(
            "egress_posture", "/egress/posture",
            "ALLOWLIST egress posture is not yet implemented for local_cli connector nodes: "
            "the local-CLI worker runs the CLI unsandboxed by design (no Seatbelt/egress-proxy "
            "integration exists yet) and refuses any envelope but OPEN, on any host — select "
            "OPEN egress posture for a graph containing a local_cli node",
        )

    caps = capabilities if capabilities is not None else probe_platform()
    return decide_egress_posture(egress_config, capabilities=caps)
