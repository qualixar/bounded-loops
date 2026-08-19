"""Release guard: every public symbol that is unreachable from the engine must be
declared in ``scripts/unreachable_allowlist.py`` with an explicit reason.

The audit script (``scripts/audit_unreachable.py``) exits 1 whenever it finds
undeclared unreachable symbols. That exit code is the first gate. This test adds a
SECOND gate: it asserts that the only undeclared symbols are the ones named in
``KNOWN_ORPHANED_CAPABILITIES`` below — confirmed orphaned capabilities that have
been researched and loudly reported. Any symbol outside that set is a NEW defect
that must be either declared (with a reason) or wired before release.

Why two gates? The audit script exit code catches "undeclared exists" at the
command line. This test catches "a new undeclared appeared" inside the full test
suite, so it shows up in CI alongside ordinary test failures rather than requiring
a separate manual step.

``KNOWN_ORPHANED_CAPABILITIES`` is NOT a free pass list: every symbol in it is a
confirmed defect. Adding a symbol here without wiring it up or writing a declaration
is wrong. Removing a symbol (because it was wired) is always correct and the test
will still pass.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── KNOWN_ORPHANED_CAPABILITIES ─────────────────────────────────────────────
#
# These nine symbols are CONFIRMED ORPHANED CAPABILITIES: implemented, tested,
# but no engine caller. They are NOT in the allowlist because papering them over
# would hide a real defect. They stay here as a loud reminder that wiring is due.
#
# For each symbol the defect is: test IS the missing caller. A unit test can
# never catch the missing wiring because the test itself is what calls it.
#
#   AuditPlanService   + LocalAuditStore
#       The audit plan service and its concrete store adapter. audit_plan.py
#       docstring says: "Controller/arena wiring (out of scope for this task —
#       a parallel effort owns those files)." Wiring to a graph controller or
#       Arena projection is the deferred work.
#
#   OutcomeLabel + label_node_outcome
#       Ground-truth labeling: recording whether a node's output was actually
#       correct, independent of the gate verdict. The labeling event type and
#       function exist and are tested, but no graph controller appends a label.
#       Wiring requires a labeling API surface (REST, CLI, MCP) to be added.
#
#   register_connection + advance_connection + authorize_route
#   + compiler_connection_snapshot
#       The connection admission and routing subsystem. The four public functions
#       implement the full connection lifecycle (discover → admit → route →
#       compile) but no graph controller or composition entry point calls them.
#       The graph BYOK subsystem (test_execute_graph_byok.py) tests them in
#       isolation; wiring requires a controller that negotiates connections before
#       dispatching nodes.
#
#   resolve_by_repair
#       The repair-path companion to reconcile_audit. reconcile_audit accepts a
#       ValidatedRepairIds argument whose type guarantees it came from
#       resolve_by_repair; audit_projection.py calls reconcile_audit with the
#       default (no repairs). Wiring requires the repair flow (RepairAttempt →
#       resolve_by_repair → reconcile_audit) to be implemented end-to-end.
#
KNOWN_ORPHANED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # audit plan subsystem — wiring deferred to parallel effort
        "AuditPlanService",
        "LocalAuditStore",
        # ground-truth labeling — no labeling API surface wired yet
        "OutcomeLabel",
        "label_node_outcome",
        # connection admission / routing subsystem — no controller wired yet
        "register_connection",
        "advance_connection",
        "authorize_route",
        "compiler_connection_snapshot",
        # repair flow — reconcile_audit called without repairs in production
        "resolve_by_repair",
    }
)


def _run_audit() -> tuple[int, str]:
    """Run the audit script and return (exit_code, combined_output)."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_unreachable.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.returncode, result.stdout + result.stderr


def _parse_undeclared(output: str) -> frozenset[str]:
    """Extract the symbol names from the UNDECLARED UNREACHABLE section."""
    symbols: set[str] = set()
    in_section = False
    for line in output.splitlines():
        if "UNDECLARED UNREACHABLE" in line:
            in_section = True
            continue
        if in_section and line.startswith("==="):
            # Start of the next section — stop.
            break
        if in_section and line.strip():
            # Lines look like: "  bounded_loops/...:42  SymbolName"
            match = re.search(r"\s+(\w+)\s*$", line)
            if match:
                symbols.add(match.group(1))
    return frozenset(symbols)


def test_no_new_undeclared_unreachable_symbols() -> None:
    """No public symbol may be unreachable AND undeclared, except the known orphans.

    Passing = the undeclared set is a subset of KNOWN_ORPHANED_CAPABILITIES.
    Failing = a new undeclared symbol appeared that has not been declared AND is
              not a known orphan. The fix is to either add it to the allowlist
              (scripts/unreachable_allowlist.py) with a reason, wire it up so it
              becomes reachable, or — only if it is a confirmed orphaned capability
              — add it to KNOWN_ORPHANED_CAPABILITIES here with a clear comment.

    The test deliberately does NOT fail when a known orphan disappears from the
    undeclared list: that means someone wired it up, which is always a good thing.
    """
    _exit_code, output = _run_audit()
    undeclared = _parse_undeclared(output)

    # Symbols that are undeclared AND not in the known-orphan registry are defects.
    new_defects = undeclared - KNOWN_ORPHANED_CAPABILITIES
    assert not new_defects, (
        "New undeclared unreachable symbol(s) found — each must be either declared "
        "in scripts/unreachable_allowlist.py with a reason, wired into the engine, "
        "or added to KNOWN_ORPHANED_CAPABILITIES with a clear explanation:\n  "
        + "\n  ".join(sorted(new_defects))
        + "\n\nFull audit output:\n"
        + output
    )
