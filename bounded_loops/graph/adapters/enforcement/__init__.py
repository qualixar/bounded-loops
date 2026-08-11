"""Real, fail-closed execution isolation enforcement (E2)."""

from bounded_loops.graph.adapters.enforcement.capabilities import (
    PlatformCapabilities,
    probe_platform,
)
from bounded_loops.graph.adapters.enforcement.egress_posture import (
    EgressPosture,
    EgressPostureConfig,
    EgressPostureDecision,
    decide_egress_posture,
    resolve_egress_posture,
)
from bounded_loops.graph.adapters.enforcement.enforcer import (
    ExecutionEnforcer,
    build_enforcer,
)
from bounded_loops.graph.adapters.enforcement.sandbox import (
    SandboxMechanism,
    wrap_argv,
)

__all__ = [
    "PlatformCapabilities",
    "probe_platform",
    "EgressPosture",
    "EgressPostureConfig",
    "EgressPostureDecision",
    "decide_egress_posture",
    "resolve_egress_posture",
    "ExecutionEnforcer",
    "build_enforcer",
    "SandboxMechanism",
    "wrap_argv",
]
