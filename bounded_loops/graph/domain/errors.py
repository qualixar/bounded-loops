"""Stable typed failures for graph contracts.

Hierarchy note (ARCH-08): two additional exception types exist that are NOT subclasses
of ``GraphError`` by design — ``GraphInitError`` (``graph/init/errors.py``) and
``ConsoleOpenError`` (``graph/console/server.py``).  Both are deliberate clean-break
types for their own subsystem's CLI boundary: ``cmd_graph_init`` and
``cmd_graph_console`` each catch exactly their own type and turn it into a one-line
``error: ...`` message plus exit code 2.  Making them inherit ``GraphError`` would cause
``except GraphError:`` catch-alls in the application layer (e.g. ``mcp_graph.py``) to
swallow init and console failures silently, changing observable behaviour.  They are
documented here as a navigation aid for reviewers who notice the gap.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base error for graph-engineering contracts."""


class GraphValidationError(GraphError):
    """A closed authoring-contract violation with a machine-readable code."""

    def __init__(self, code: str, pointer: str, message: str) -> None:
        super().__init__(f"{code} at {pointer}: {message}")
        self.code = code
        self.pointer = pointer
        self.message = message


class GraphIntegrityError(GraphError):
    """A controller event/artifact stream is corrupt or inconsistent."""


class WorkerContractError(GraphIntegrityError):
    """A worker cannot honour its contract, and a retry would change nothing.

    Distinct from a transient worker fault because the two deserve opposite responses. A fault
    is worth retrying; a broken contract — a CLI whose JSON envelope this version cannot read, a
    provider whose usage block is unusable — will fail identically on every attempt while paying
    the provider each time. Raised as its own type so the controller can end the node instead of
    spending the whole retry budget proving the wiring is still wrong.
    """
