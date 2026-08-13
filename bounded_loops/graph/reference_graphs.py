"""Shape definitions for the shipped reference graphs, and the renderer that pins their digests.

These are COMPOSITIONS of already-shipped loop packages, not new examples. Every node's gate is a
real mechanical check that existed and was validated before the graph engine could run it, and 64 of
the 68 packages run on a stub runner, so a reference graph costs nothing to run and is deterministic
in CI. Authoring new example loops instead would have shipped unproven gates in place of proven ones.

The shapes live here rather than inside the generated YAML so that the generator script and the
drift test read ONE definition. Two copies of a graph shape agree by luck until the day they do not.

What each reference graph must contain to be worth publishing, rather than being a Gantt chart of
unit tests:

* at least three ``kind: loop`` nodes whose packages are real and digest-pinned;
* a fan-out and a join, so the graph exercises cross-node causality rather than a chain;
* at least one CONDITIONAL edge, so the four-literal guard grammar is actually used;
* a human ``approval`` before any irreversible effect;
* exactly one ``publish`` node carrying that effect;
* keyless end to end, so CI can run it with no API key and no spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoopNodeSpec:
    """One ``kind: loop`` node: its graph-local id and the loop package it pins."""

    node_id: str
    package: str


@dataclass(frozen=True)
class ReferenceGraph:
    """One shipped reference graph, as a shape rather than as rendered text."""

    slug: str
    graph_id: str
    domain: str
    summary: str
    #: Loop nodes that fan out from the start of the graph, in declaration order.
    parallel_checks: tuple[LoopNodeSpec, ...]
    #: The loop node reached when ``remediation_trigger`` FAILS — the conditional branch.
    remediation: LoopNodeSpec
    #: Which parallel check's failure routes to remediation.
    remediation_trigger: str
    #: Role required at the human checkpoint before the irreversible effect.
    approval_role: str
    #: What the publish node's irreversible effect is, in the domain's own language.
    publish_summary: str


#: `customer` and `personal-projects` have no `role:` tag on any shipped loop, which is why those
#: graphs are assembled from `legal` / `operations` / `engineering` packages. The DOMAIN belongs to
#: the graph — a workflow — not to the individual check. That claim only holds where the irreversible
#: effect is genuinely that domain's effect AND every loop on the critical path is a check that effect
#: requires; a graph that cannot name a missing mechanical check is a label on other people's gates.
REFERENCE_GRAPHS: tuple[ReferenceGraph, ...] = (
    ReferenceGraph(
        slug="finance-payment-assurance",
        graph_id="finance-payment-assurance",
        domain="finance",
        summary=(
            "Three independent accounting checks run in parallel, join, and gate a payment "
            "instruction behind a controller's approval. A failed balance check routes to "
            "reconciliation instead of stopping the run."
        ),
        parallel_checks=(
            LoopNodeSpec("match-invoice", "invoice-3way-match"),
            LoopNodeSpec("balance-journal", "journal-entries-balance"),
            LoopNodeSpec("check-fx-rate", "fx-rate-sanity"),
        ),
        remediation=LoopNodeSpec("reconcile-ledger", "ledger-reconciliation"),
        remediation_trigger="balance-journal",
        approval_role="finance-controller",
        publish_summary="emit an ISO 20022 payment instruction",
    ),
)


def graphs_root(repo_root: Path) -> Path:
    return repo_root / "graphs"


def _node_block(node_id: str, digest: str, *, outputs: str, inputs: str = "") -> str:
    declared = f"      {inputs}: internal\n" if inputs else ""
    return (
        f"  - id: {node_id}\n"
        f"    kind: loop\n"
        f"    loop_package: \"{digest}\"\n"
        f"    inputs:{'' if inputs else ' {}'}\n"
        f"{declared}"
        f"    outputs:\n"
        f"      {outputs}: internal\n"
        f"    budget:\n"
        # A keyless stub loop that already exhausted its own internal bound fails identically on a
        # second graph attempt, so retrying it at graph level would only burn the bound to re-derive
        # the same outcome. Retry belongs INSIDE the loop; the graph's lever is repair.
        f"      max_attempts: 1\n"
        f"      max_wallclock_s: 300\n"
        f"    effects: []\n"
        f"    isolation: workspace_only\n"
    )


def render_reference_graph(definition: ReferenceGraph, repo_root: Path) -> str:
    """Render one reference graph to portable YAML with every loop package digest pinned."""
    from bounded_loops.graph.adapters.workers.loop_packages import qualified_package_digest

    loops = repo_root / "loops"

    def digest_of(package: str) -> str:
        return qualified_package_digest(loops / package)

    lines = [
        f"# {definition.graph_id} — {definition.domain}",
        "#",
        f"# {definition.summary}",
        "#",
        "# GENERATED by scripts/regenerate_reference_graphs.py. Every loop_package below is a CONTENT",
        "# digest of a shipped package under loops/, so editing one of those packages invalidates this",
        "# file. tests/graphs/test_reference_graphs.py fails on that drift and names the script.",
        "#",
        "# Keyless: every loop node runs on a stub runner with its own real mechanical gate, so this",
        "# graph needs no API key and no spend.",
        "api_version: bounded-loops.dev/graph/v1",
        f"graph_id: {definition.graph_id}",
        "version: 1.0.0",
        "connection_slots: []",
        "nodes:",
    ]
    for check in definition.parallel_checks:
        lines.append(_node_block(check.node_id, digest_of(check.package), outputs="verdict").rstrip("\n"))
    lines.append(
        _node_block(
            definition.remediation.node_id, digest_of(definition.remediation.package),
            outputs="reconciliation", inputs="source",
        ).rstrip("\n")
    )
    lines.extend([
        "  - id: join-checks",
        "    kind: join",
        "    mode: all_successful",
        # An edge's ``to_port`` must EXIST in the target's declared inputs -- the graph schema
        # already carries typed ports, and refuses a wire to a port nobody declared. One input per
        # incoming check, named for its source so a reader can tell which arm is missing.
        "    inputs:",
        *(f"      {check.node_id}: internal" for check in definition.parallel_checks),
        "    outputs:",
        "      cleared: internal",
        "    budget:",
        "      max_attempts: 1",
        "      max_wallclock_s: 60",
        "    effects: []",
        "    isolation: workspace_only",
        f"  - id: approve-{definition.domain}",
        "    kind: approval",
        f"    required_role: {definition.approval_role}",
        "    inputs:",
        "      cleared: internal",
        "    outputs:",
        "      decision: internal",
        "    budget:",
        "      max_attempts: 1",
        "      max_wallclock_s: 86400",
        "    effects: []",
        "    isolation: workspace_only",
        "  - id: publish-instruction",
        "    kind: publish",
        # publication_policy is a NAMED POLICY (a non-empty string the deployment resolves), not
        # an inline object. The engine keeps the policy out of the portable graph so the same
        # graph can publish under different rules in different deployments.
        f"    publication_policy: {definition.domain}-instruction-v1",
        "    inputs:",
        "      decision: internal",
        "    outputs:",
        "      receipt: internal",
        "    budget:",
        "      max_attempts: 1",
        "      max_wallclock_s: 300",
        "    effects:",
        "      - external_write",
        "    isolation: container_restricted",
        "edges:",
    ])
    for check in definition.parallel_checks:
        lines.extend([
            f"  - from_node: {check.node_id}",
            "    from_port: verdict",
            "    to_node: join-checks",
            f"    to_port: {check.node_id}",
            "    when: succeeded",
        ])
    lines.extend([
        f"  - from_node: {definition.remediation_trigger}",
        "    from_port: verdict",
        f"    to_node: {definition.remediation.node_id}",
        "    to_port: source",
        "    when: failed",
        "  - from_node: join-checks",
        "    from_port: cleared",
        f"    to_node: approve-{definition.domain}",
        "    to_port: cleared",
        f"  - from_node: approve-{definition.domain}",
        "    from_port: decision",
        "    to_node: publish-instruction",
        "    to_port: decision",
        "policies:",
        # continue_declared is REQUIRED by the `when: failed` edge above: under fail_closed the run
        # stops at the first node failure, so a failed-guarded edge could never be admitted and
        # validation refuses it outright.
        "  fail_mode: continue_declared",
        "  data_class: internal",
        "",
    ])
    return "\n".join(lines)
