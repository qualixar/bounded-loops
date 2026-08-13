"""How ``bl`` prints its results.

Split out of ``cli.py`` in P3 for the 800-line cap. Formatting changes most often and matters
least to correctness, so keeping it beside the argument parsing meant every wording tweak touched
the file that also decides what runs.

One rule these functions all obey: a non-DONE outcome is never printed as a success. HALT, PAUSE,
KILLED and ERROR each say what they are — a loop that stopped without its gate passing has not
succeeded, and dressing that up is the most damaging thing a reporting layer can do.
"""

from __future__ import annotations

import json
import sys

from bounded_loops.domain.models import Outcome


# ── Output formatters ─────────────────────────────────────────────────────────

def _print_outcome(outcome: Outcome, *, as_json: bool) -> None:
    """Print a run Outcome to stdout."""
    if as_json:
        data = {
            "subcommand":   "run",
            "status":       outcome.status.value,
            "reason":       outcome.reason,
            "laps":         outcome.laps,
            "ledger_path":  str(outcome.ledger_path),
        }
        print(json.dumps(data))
    else:
        symbol = "✓" if outcome.status.value == "DONE" else "✗"
        print(
            f"{symbol} [{outcome.status.value}] {outcome.reason} "
            f"(laps: {outcome.laps})  ledger: {outcome.ledger_path}"
        )
        if outcome.status.value == "DONE":
            lap_word = "lap" if outcome.laps == 1 else "laps"
            print(
                "Gate verified: the independent acceptance gate passed "
                f"after {outcome.laps} {lap_word}."
            )
            print(
                "Next: inspect the ledger above; use --keep-workspace "
                "when you need to debug the resulting files."
            )


def _print_lint_results(results: list[dict]) -> None:
    """Print lint results to stdout."""
    for r in results:
        symbol = "PASS" if r["passed"] else "FAIL"
        print(f"[{symbol}] {r['path']}")
        for err in r["errors"]:
            print(f"       {err}", file=sys.stderr)


def _print_list(loops: list[dict]) -> None:
    """Print discovered loops to stdout."""
    if not loops:
        print("No loops found.")
        print(
            "Create one with `bl new --list` and `bl new <template> <dir>`, "
            "or run `git clone https://github.com/qualixar/bounded-loops` "
            "to browse the full loop catalog."
        )
        return
    # Column-aligned table: name | role | rung | gate_kind
    header = f"{'NAME':<30} {'ROLE':<20} {'RUNG':<6} {'GATE':<20}"
    print(header)
    print("-" * len(header))
    for lp in loops:
        role_str = ",".join(lp["role"]) if lp["role"] else "?"
        err_suffix = f"  [ERROR: {lp['error']}]" if lp["error"] else ""
        print(
            f"{lp['name']:<30} {role_str:<20} {lp['rung']:<6} "
            f"{lp['gate_kind']:<20}{err_suffix}"
        )


def _print_show(data: dict) -> None:
    print(f"name: {data['name']}")
    print(f"path: {data['path']}")
    print(f"pattern: {data['pattern']}")
    print(f"role: {', '.join(data['role']) if data['role'] else '?'}")
    print(f"rung: {data['rung']}")
    print(f"runner: {data['runner']['kind']}")
    print(f"gate: {_format_gate(data['gate'])}")
    print(f"approval_required: {data['approval_required']}")
    if data["production_bounds"]:
        print(f"production_bounds: {data['production_bounds']}")
    print(f"risk: {', '.join(data['risk']) if data['risk'] else 'none'}")
    print(f"content_hash: {data['content_hash']}")


def _format_gate(gate: dict) -> str:
    if gate["kind"] == "composite":
        children = ", ".join(_format_gate(child) for child in gate.get("children", []))
        return f"composite({gate.get('mode', 'all')}: {children})"
    if gate.get("run"):
        return f"{gate['kind']} [{gate['run']}]"
    if gate.get("schema"):
        return f"{gate['kind']} [schema={gate['schema']}]"
    return gate["kind"]


