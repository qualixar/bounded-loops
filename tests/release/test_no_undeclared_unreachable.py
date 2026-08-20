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
# CONFIRMED ORPHANED CAPABILITIES: implemented, tested, but no engine caller. NOT in the
# allowlist, because papering them over would hide a real defect. They stay here as a loud
# reminder that wiring is due.
#
# For each symbol the defect is: the test IS the missing caller. A unit test can never catch
# missing wiring, because the test itself is what calls it.
#
# 0.7.0 removed seven of the nine that stood here. Removal, not wiring, was the right
# disposition for all seven: none had a caller, none had a consumer waiting, and a subsystem
# whose only exercise is its own unit tests is a capability the product claims and does not
# deliver. Every one is recoverable from tag `v0.6.10`.
#
#   AuditPlanService + LocalAuditStore     — removed 0.7.0. audit_plan.py deferred its wiring
#       to "a parallel effort" that never existed. `plan_from_mapping` was the only symbol
#       anything imported from audit_store, and it lives in domain/audit_serde.py; cli_arena
#       now imports it from there.
#   register_connection + advance_connection + authorize_route + compiler_connection_snapshot
#       — removed 0.7.0. A four-function credential-negotiating admission lifecycle, which
#       contradicts this engine's stated posture of no-secret connectors. The grant path in
#       the same module HAS callers and stays.
#   resolve_by_repair                      — removed 0.7.0. The repair flow was never wired
#       end to end. `ValidatedRepairIds` stays, because reconcile_audit's signature uses it,
#       with its overclaiming comment corrected.
#
# What remains is deliberate. OutcomeLabel and label_node_outcome record whether a node's
# output was ACTUALLY correct, independent of the gate verdict — the ground truth an
# evaluation needs and currently obtains from hand-scoring operators. Deleting it would throw
# away the instrument; wiring it needs a labeling API surface (CLI, REST or MCP) and is a
# feature, not a removal. It is the one orphan here worth keeping.
#
KNOWN_ORPHANED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # ground-truth labeling — no labeling API surface wired yet; worth wiring, not deleting
        "OutcomeLabel",
        "label_node_outcome",
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


def _count(output: str, label: str) -> int:
    """The integer the audit printed for one of its summary labels, or -1 if it printed none."""
    for line in output.splitlines():
        if line.startswith(label) and ":" in line:
            tail = line.split(":", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
    return -1


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
    exit_code, output = _run_audit()
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


def test_the_detector_itself_reported_rather_than_merely_exiting() -> None:
    """The gate on the gate. Everything below is about the DETECTOR, not about any symbol.

    `test_no_new_undeclared_unreachable_symbols` used to bind the exit code to `_exit_code` and never
    look at it, and it parsed one section out of stdout. `_parse_undeclared("")` is the empty set, and
    the empty set is a subset of the known orphans, so a detector that crashed on import, renamed its
    header, or printed nothing at all made that test PASS. A release gate that cannot fail is the
    defect class this repository exists to name, and it was sitting in the gate itself.
    """
    exit_code, output = _run_audit()

    # 2 is the detector's own "I am broken" code — today, an unreadable allowlist. Never acceptable.
    assert exit_code != 2, f"the audit script could not run:\n{output}"
    assert exit_code in (0, 1), f"unexpected audit exit {exit_code}:\n{output}"

    # It must have actually walked the package. These counts are not assertions about the codebase's
    # size; they are proof the walk happened rather than collapsing to an empty set.
    for label in (
        "defined public symbols",
        "reachable from roots",
        "unreachable, DECLARED",
        "unreachable, UNDECLARED",
        "ambiguous by name, UNDECLARED",
        "string-only, UNDECLARED",
    ):
        assert label in output, f"the audit stopped reporting '{label}':\n{output}"

    defined = _count(output, "defined public symbols")
    reachable = _count(output, "reachable from roots")
    assert defined >= 400, f"symbol discovery collapsed to {defined}; the audit proves nothing"
    assert reachable >= 300, f"reachability collapsed to {reachable}; every symbol would be an orphan"

    # The allowlist must have LOADED. Zero declarations with a non-empty residual is the exact shape
    # of the swallowed ImportError this file's sibling script used to hide.
    assert _count(output, "unreachable, DECLARED") > 0, (
        f"no declarations were read — the allowlist did not load:\n{output}"
    )
    assert _count(output, "ambiguous by name, DECLARED") > 0, (
        f"no ambiguity declarations were read:\n{output}"
    )

    # And the exit code must AGREE with what it printed. A detector that finds undeclared symbols and
    # exits 0 is worse than one that finds none: it reports honestly and gates nothing.
    found_any = (
        _count(output, "unreachable, UNDECLARED")
        + _count(output, "ambiguous by name, UNDECLARED")
        + _count(output, "string-only, UNDECLARED")
    )
    assert (exit_code == 1) == (found_any > 0), (
        f"exit {exit_code} disagrees with {found_any} undeclared symbol(s) reported:\n{output}"
    )


def test_the_two_structural_blind_spots_stay_declared() -> None:
    """Name collisions and string-only reachability are declared, never silently absorbed.

    Both are cases the reference graph CANNOT decide. A name defined in two modules is ONE node, so
    an orphan sharing a name with a live function is invisible — `evaluation.tier2.load` was reported
    reachable purely because `application.manifest.load` exists. And an identifier-shaped string
    literal counts as a reference, which is how dynamic dispatch reaches `main` and the P2 gate table
    reaches `CheckovGate`, and also how a stray `x = "SomeOrphan"` would silence this audit forever.

    Neither can be resolved without real import resolution, so the detector reports them and the
    allowlist declares them. This test is what keeps a future collision from being absorbed.
    """
    exit_code, output = _run_audit()
    assert exit_code != 2, output
    assert "=== UNDECLARED AMBIGUOUS" not in output, (
        "a public name is now defined in more than one module without a declaration saying how EACH "
        f"site is reached. Add it to ALLOWED_AMBIGUOUS in scripts/unreachable_allowlist.py:\n{output}"
    )
    assert "=== UNDECLARED STRING-ONLY" not in output, (
        "a public symbol is reachable ONLY because some literal spells its name. Declare it in "
        f"ALLOWED_STRING_ONLY, or find its real caller:\n{output}"
    )
