"""bounded-loops — runnable, CLI-agent-portable reference library of bounded AI-agent loops.

Public surface
--------------
Everything listed in ``__all__`` is stable across semver-compatible releases.
Everything NOT listed here is internal.  Internal paths can change in any release —
including patch releases — without notice.  Do not import from them.

Loop engine (embed a bounded loop in your process):
    load_loop      — load a loop directory into a LoopManifest
    LoopManifest   — the validated, frozen manifest type (type-hint / inspection only)
    wire           — wire a LoopManifest into a runnable use-case
    Bounds         — immutable loop-bounds configuration type
    Outcome        — terminal result of a loop run
    Status         — DONE / HALT / PAUSE / KILLED / ERROR

Graph engine (implement a custom node worker or gate):
    NodeWorkerPort     — Protocol: implement to plug in a custom node worker
    WorkerResult       — what your worker's ``execute()`` must return
    IndependentGatePort — Protocol: implement to plug in a custom gate evaluator
    GateVerdict        — what your gate's ``evaluate()`` must return

MCP front door:
    Start the MCP server with ``bounded-loops-mcp`` (requires the ``[mcp]`` extra).
    See docs/EMBEDDING.md for a full integration walkthrough.
"""

from __future__ import annotations

# ── Loop engine ──────────────────────────────────────────────────────────────
from bounded_loops.composition import wire
from bounded_loops.application.manifest import load as load_loop, LoopManifest
from bounded_loops.domain.models import Bounds, Outcome, Status

# ── Graph engine ports ───────────────────────────────────────────────────────
from bounded_loops.graph.application.node_contracts import (
    IndependentGatePort,
    GateVerdict,
    NodeWorkerPort,
    WorkerResult,
)

__version__ = "0.6.9"

__all__ = [
    "__version__",
    # Loop engine
    "load_loop",
    "LoopManifest",
    "wire",
    "Bounds",
    "Outcome",
    "Status",
    # Graph engine
    "NodeWorkerPort",
    "WorkerResult",
    "IndependentGatePort",
    "GateVerdict",
]
