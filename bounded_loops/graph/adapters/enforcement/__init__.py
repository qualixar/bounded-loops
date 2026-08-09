"""Real, fail-closed execution isolation enforcement (E2)."""

from bounded_loops.graph.adapters.enforcement.capabilities import (
    PlatformCapabilities,
    probe_platform,
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
    "ExecutionEnforcer",
    "build_enforcer",
    "SandboxMechanism",
    "wrap_argv",
]
