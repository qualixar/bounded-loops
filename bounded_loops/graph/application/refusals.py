"""Every refusal the graph validator can raise, in plain language, with the fix.

Why this module exists: a host model (Claude Code, Codex, Cursor) given a task will happily
author a graph that the compiler then rejects, and an error like
``[port_type_mismatch] /edges/2 — declared types differ`` teaches it nothing about what to write
instead. This is the table that closes that loop — for the MCP capability tool, for the host
skill pack, and for rendering refusals to a non-technical user as an explanation rather than a
validator string.

`REFUSAL_CODES` is asserted against the validator's own source by
`tests/graph/application/test_refusals.py`, which extracts every ``_error("<code>"`` site from
`validate_graph.py`. A new refusal added there without an entry here fails that test — so this
table cannot silently fall behind the code it documents.

Each `fix` is written as an instruction to whoever authored the graph, because that is who reads
it. None of them says "contact support".
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Refusal:
    """One refusal: its code, what it means, and what to change."""

    code: str
    summary: str
    fix: str


def _r(code: str, summary: str, fix: str) -> tuple[str, Refusal]:
    return code, Refusal(code=code, summary=summary, fix=fix)


REFUSALS: Mapping[str, Refusal] = MappingProxyType(
    dict(
        (
            # ── document shape ───────────────────────────────────────────────
            _r(
                "invalid_json",
                "The manifest is not parseable JSON.",
                "Fix the JSON syntax at the reported position.",
            ),
            _r(
                "invalid_yaml",
                "The manifest is not parseable YAML.",
                "Fix the YAML syntax at the reported position.",
            ),
            _r(
                "duplicate_key",
                "The same key appears twice in one mapping.",
                "Delete one of the two. A silent last-wins merge would hide which one applied.",
            ),
            _r(
                "api_version",
                "The manifest does not declare the api_version this engine compiles.",
                "Set api_version to the value the error names.",
            ),
            _r(
                "version",
                "The graph's version is not a pinned semantic version.",
                "Write an exact version like '1.0.0'. Ranges are refused: a graph that can "
                "mean two things is not reproducible.",
            ),
            _r(
                "type",
                "A field has the wrong shape (object, array, or non-empty string expected).",
                "Give the field the shape the message names.",
            ),
            _r(
                "range",
                "A numeric field is outside its permitted range.",
                "Choose a value inside the stated bounds.",
            ),
            _r(
                "enum",
                "A field's value is not one this engine supports.",
                "Use one of the supported values. `bl_capabilities` lists them, including which "
                "are declared but not yet honoured.",
            ),
            _r(
                "unknown_field",
                "A field the schema does not define is present.",
                "Remove it. Unknown fields are refused rather than ignored, because an ignored "
                "field is a policy the author believes is in force and is not.",
            ),
            _r(
                "missing_field",
                "A required field is absent.",
                "Add the field(s) the message names.",
            ),
            _r(
                "identifier",
                "An id is not a stable, portable identifier.",
                "Use a plain name — letters, digits, dash, underscore — with no paths, spaces, "
                "or host-specific characters.",
            ),
            _r(
                "duplicate_value",
                "A list that must hold unique declared values repeats one.",
                "Remove the duplicate.",
            ),
            # ── portability and safety of the document itself ────────────────
            _r(
                "secret_field",
                "The manifest contains a secret-shaped field.",
                "Delete it. Credentials never live in a graph: declare a connection slot and let "
                "the no-secret broker resolve it at run time.",
            ),
            _r(
                "absolute_path",
                "The manifest contains an absolute local path.",
                "Use a workspace-relative path. An absolute path makes the graph run on exactly "
                "one machine.",
            ),
            _r(
                "provider_in_slot",
                "A connection slot names a specific provider.",
                "Declare the capability you need (modality, context, tool use) and let the "
                "resolver pick. Naming a provider pins the graph to one vendor.",
            ),
            # ── nodes ────────────────────────────────────────────────────────
            _r(
                "duplicate_node_id",
                "Two nodes share an id.",
                "Rename one. Node ids address receipts, so a collision makes the log ambiguous.",
            ),
            _r(
                "unknown_node_kind",
                "The node's kind is not one this engine runs.",
                "Use a supported kind — `bl_capabilities` lists all of them with their "
                "kind-specific required fields.",
            ),
            _r(
                "mutable_package_reference",
                "A loop node's loop_package is not a sha256 digest.",
                "Pin the package by content digest. A mutable reference means the thing that "
                "ran is not the thing the receipt names.",
            ),
            _r(
                "incomplete_branches",
                "A router does not cover every outcome.",
                "Declare routes for each branch, and either a 'default' route or an explicit "
                "default_route. An uncovered branch is a run that stops with nowhere to go.",
            ),
            _r(
                "join_mode",
                "The join's mode is not supported.",
                "Use all_selected, all_successful, or any_successful.",
            ),
            _r(
                "impossible_join",
                "A join has no incoming edge.",
                "Give the join the edges it joins, or delete it.",
            ),
            _r(
                "audit_profile",
                "The audit profile is not a string or null.",
                "Name a profile, or set it to null.",
            ),
            # ── edges and dataflow ───────────────────────────────────────────
            _r(
                "unknown_edge_node",
                "An edge references a node that does not exist.",
                "Fix the node id, or add the node.",
            ),
            _r(
                "missing_output_port",
                "An edge reads an output port the source node does not declare.",
                "Declare the output on the source node, or point the edge at one it has.",
            ),
            _r(
                "missing_input_port",
                "An edge writes to an input port the target node does not declare.",
                "Declare the input on the target node, or point the edge at one it has.",
            ),
            _r(
                "port_type_mismatch",
                "An edge connects two ports whose declared types differ.",
                "Make the types agree, or insert a node that converts.",
            ),
            _r(
                "cycle",
                "The graph is not acyclic.",
                "Remove the cycle. Retry is bounded per node and repair is a bounded backward "
                "edge; a cycle in the authoring graph is unbounded work.",
            ),
            _r(
                "edge_condition",
                "An edge guard can never be reached.",
                "Under fail_mode: fail_closed the run stops at the first failure, so a "
                "`when: failed` edge is dead. Either drop the guard or change the fail mode.",
            ),
            _r(
                "unknown_connection_slot",
                "A node names a connection slot the graph does not declare.",
                "Declare the slot, or point the node at one that exists.",
            ),
            _r(
                "duplicate_slot_id",
                "Two connection slots share an id.",
                "Rename one of them. Nodes select a connection by slot id, so a collision makes "
                "it undecidable which connection a node actually got.",
            ),
            # ── failure, repair, retry ───────────────────────────────────────
            _r(
                "fail_mode",
                "The graph's fail_mode is not a declared mode.",
                "Use a supported fail mode.",
            ),
            _r(
                "on_failure",
                "The node's failure policy is malformed or unreachable.",
                "Write a bare string for the simple policies; only repair uses the object form "
                "{mode: repair, target: <ancestor node id>}. Under fail_closed, repair is "
                "unreachable — the run stops at the first failure.",
            ),
            _r(
                "on_failure_unimplemented",
                "The policy is declared by the schema but the runtime does not route it.",
                "Use fail_graph (the default) or repair. `continue` and `await_human` are "
                "refused rather than accepted-and-ignored, because accepting them would hand "
                "back a plan whose declared failure policy is silently discarded.",
            ),
            _r(
                "repair_target",
                "A repair edge names no node, or names one that is not an ancestor.",
                "Point target at an ancestor of the failing node — repair re-executes work that "
                "already ran upstream.",
            ),
            _r(
                "repair_budget",
                "Repair is declared without a bound, or the bound is out of range.",
                "Set policies.repair_budget above 0. It is the GLOBAL bound on repair rounds and "
                "is what makes the loop terminate.",
            ),
            _r(
                "retry_of_effectful_node",
                "A node carrying an external or irreversible effect may retry with no "
                "idempotency key.",
                "Give the effect a per-effect idempotency key, or set the node's max_attempts "
                "to 1. Retrying an irreversible effect is a double-spend.",
            ),
            _r(
                "duplicate_effect",
                "A node declares the same effect twice.",
                "Remove the duplicate.",
            ),
        )
    )
)

REFUSAL_CODES = frozenset(REFUSALS)


def explain(code: str) -> Refusal | None:
    """The plain-language entry for `code`, or None if this table does not know it.

    Returns None rather than raising: a caller rendering an error to a user must still be able
    to show the raw refusal when the table has fallen behind, and a crash inside error handling
    is the worst possible failure mode.
    """
    return REFUSALS.get(code)
