"""Local-CLI egress policy — where the deployment's resolved egress posture meets a
local_cli connector node's actual runtime capability (Slice 2 wiring).

DECISION (Varun): ``LocalCliConnectorWorker`` now has a real caged path for ALLOWLIST — it
reuses the SAME Seatbelt loopback-proxy cage ``SandboxedNodeWorker``/the ``https`` transport
already use (see ``local_cli_worker.py``: filesystem writes confined to the workdir + the
operator's real HOME + TMPDIR, so the subscription login still works; network confined to the
loopback egress proxy, which admits only the configured allowlist). So for a plan containing a
``local_cli`` node:

* ``open``      — unaffected; byte-for-byte today's hardcoded behavior.
* ``allowlist`` — HONORED: ``decide_egress_posture``'s generic, capability-aware decision
  applies directly. FAILS CLOSED if this host cannot deliver the cage (no Seatbelt / no
  egress proxy) — never silently downgrades to ``open``. That check now actually matters for
  local_cli (previously it was unconditionally refused before reaching it).
* ``broker``    — still refused: a local_cli node's subscription CLI authenticates
  out-of-band and talks to its own vendor over its own TLS; the no-secret ``EgressBroker``
  (a lease bound to one declared destination / method / effect — see ``egress_broker.py``)
  has nothing to mediate. This is a genuine architectural mismatch, not a missing feature —
  it did not change with the ALLOWLIST decision. See ``docs/graph-egress-posture.md``.

A plan with NO local_cli node is entirely unaffected — including the ALLOWLIST host-
capability check: it is skipped entirely (not merely "not applied"), so an https-only run's
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

    # BROKER stays refused — architecturally incoherent for this transport (see module
    # docstring). ALLOWLIST now proceeds to the generic, capability-aware decision below,
    # which fails closed on its own if this host cannot deliver the cage.
    if egress_config.posture is EgressPosture.BROKER:
        raise GraphValidationError(
            "egress_posture", "/egress/posture",
            "BROKER egress posture cannot mediate a local_cli connector node's egress: a "
            "subscription CLI authenticates out-of-band and talks to its own vendor over its "
            "own TLS, and the no-secret EgressBroker (a lease bound to one declared "
            "destination/method/effect) has nothing to mediate — select OPEN or ALLOWLIST "
            "egress posture for a graph containing a local_cli node, or remove it to use BROKER",
        )

    caps = capabilities if capabilities is not None else probe_platform()
    return decide_egress_posture(egress_config, capabilities=caps)
