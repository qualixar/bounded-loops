"""Symbols with no reachable caller inside the engine, and WHY that is correct.

Read by ``audit_unreachable.py``. A symbol here is not a defect; a symbol NOT here with no reachable
caller is. Writing the reason down is the whole mechanism: the absence of a caller becomes something
a person had to justify and a reviewer can disagree with, instead of something nobody noticed. That
is the lesson from shipping an unreachable gate-plugin surface in a 0.6.8 changelog.

**Every reason in this file was checked, not assumed.** An entry whose justification was inferred
from a name rather than read from the code does not belong here — leaving a symbol in the failing
residual is strictly better than declaring it on a guess, because the residual gets looked at again.

Categories:
  PORT       a ``Protocol``. Nothing calls it; adapters satisfy it structurally.
  PORT_IMPL  a concrete implementation of a port (Protocol) used in tests and/or deployment wiring.
             The engine does not hard-call it; the deployment or test selects the implementation.
  HARNESS    the evaluation apparatus. Driven by tests and the paper's experiment scripts, never by
             the engine. Making the engine depend on it would be the defect.
  CONTROL    exists so a measurement can be compared against it; deliberately unused in production.
  COMPAT     a migration boundary kept deliberately.
  COMPAT_DEFUNCT  a deliberately non-functional stub kept as executable documentation of a rejected
             design. Tests verify it raises the expected error; the error message IS the design doc.
  API        a supported surface an embedder or deployment wires in.
  ALIAS_MASKED  imported under a private alias (``as _name``) by its caller; the audit script
             tracks the public name, not the alias, so the reference is invisible to it.
  SCRIPT_UTIL  used by a script in ``scripts/`` which lives outside the bounded_loops package;
             the audit's root set covers only the package, so these callers are outside scope.
"""

from __future__ import annotations

ALLOWED: dict[str, str] = {
    # ── PORT — verified by AST: both subclass typing.Protocol ────────────────
    "GraphMemoryStorePort": "PORT: a Protocol. Adapters satisfy it structurally; nothing calls it.",
    "DurableKeyValuePort": "PORT: a Protocol. Adapters satisfy it structurally; nothing calls it.",

    # ── CONTROL — verified from each function's own docstring ────────────────
    "empirical_bernstein_interval": (
        "CONTROL: its own docstring says 'tests, as the control' — a fixed-time interval kept so a "
        "test can compare the anytime-valid sequence against it. Production must not use it."
    ),
    "naive_compounded_false_accept": (
        "CONTROL: its docstring calls it 'a baseline to FALSIFY, not a budget recommender' and warns "
        "that deriving max_attempts from it would be the most dangerous use of the module. It exists "
        "to be tested against, so having no caller is the correct state."
    ),

    # ── COMPAT — verified from the docstring ─────────────────────────────────
    "LegacyRunnerV2Adapter": (
        "COMPAT: its docstring states it is 'intentionally a compatibility boundary, not a claim of "
        "asynchronous cancellation', exposing synchronous runners through RunnerPortV2 during "
        "migration. New subprocess-backed runners use ProcessTurn instead."
    ),

    # ── HARNESS — verified: tests/evaluation/ exists and 4 test modules import
    #    bounded_loops.evaluation. The corpus is run as a test suite by design
    #    (see the paper's reproduction appendix), so the engine must not call it.
    "build_gate": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "establish_baseline": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "excluded_reason": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "judges_artifact": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "materialise": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "run_mutant": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "runner_needs_an_unshipped_package": "HARNESS: corpus scoping, driven by tests/evaluation.",
    "states_a_negative_requirement": "HARNESS: corpus scoping, driven by tests/evaluation.",
    "summarise": "HARNESS: mutation-corpus apparatus, driven by tests/evaluation.",
    "tier1_claim_holds": "HARNESS: corpus label checking, driven by tests/evaluation.",
    "uses_an_external_tool": "HARNESS: corpus scoping, driven by tests/evaluation.",
    "generate": "HARNESS: corpus generation, driven by tests/evaluation.",
    "generate_for_loop": "HARNESS: corpus generation, driven by tests/evaluation.",
    "iter_loop_dirs": "HARNESS: corpus generation, driven by tests/evaluation.",
    "manifest_document": "HARNESS: corpus generation, driven by tests/evaluation.",
    "mutable_artifacts": "HARNESS: corpus generation, driven by tests/evaluation.",
    "mutate": "HARNESS: mutation operators, driven by tests/evaluation.",
    "operators_for": "HARNESS: mutation operators, driven by tests/evaluation.",
    "is_mutable_artifact": "HARNESS: mutation operators, driven by tests/evaluation.",
    "is_content_quantified": "HARNESS: post-freeze operator family, driven by tests/evaluation.",
    "truncate_to_first_line": "HARNESS: destroying operator, driven by tests/evaluation.",
    "authoring_prompt": "HARNESS: Tier-2 authoring prompt, driven by tests/evaluation.",
    "authoring_prompt_digest": (
        "HARNESS: Tier-2 prompt-integrity digest, recomputed by a guard test so a widened prompt is "
        "a failing test rather than a silent change."
    ),

    # ── COMPAT_DEFUNCT — verified from loop_node_entry.py docstring ──────────
    # loop_node_entry.py explains that the subprocess design exists BECAUSE LegacyLoopWorker (the
    # in-process approach) raised. Its execute() raises GraphIntegrityError immediately; tests
    # verify it raises the expected message. It is executable documentation of a rejected design,
    # not an orphaned capability — the refusal IS the documented behaviour.
    "LegacyLoopWorker": (
        "COMPAT_DEFUNCT: deliberately non-functional. execute() raises GraphIntegrityError "
        "('legacy loop worker cannot enforce a graph execution envelope; use a sandboxed graph "
        "runner'). loop_node_entry.py explains why: subprocess isolation is what puts the loop's "
        "own runner and gate under the graph node's isolation; in-process would have left them "
        "outside it. Tests verify it raises; the error message is the design documentation."
    ),

    # ── ALIAS_MASKED — verified in composition.py ─────────────────────────────
    # composition.py imports these under private aliases so the name that appears in module-body
    # code (_register_plugin_gate_kinds, _resolve_env_passthrough) differs from the definition
    # name. The audit script counts references to the public name; an aliased import is invisible
    # to it.
    "register_plugin_gate_kinds": (
        "ALIAS_MASKED: imported in bounded_loops/composition.py line 50 as "
        "'_register_plugin_gate_kinds' and called at line 215 (_register_plugin_gate_kinds("
        "PLUGIN_GATE_KINDS)). The audit script tracks the public name; a private alias hides the "
        "reference."
    ),
    "resolve_env_passthrough": (
        "ALIAS_MASKED: imported in bounded_loops/composition.py line 98 as "
        "'_resolve_env_passthrough' and called at line 274 (resolved_env_passthrough = "
        "_resolve_env_passthrough(manifest)). Same alias-masking mechanism as register_plugin_gate_kinds."
    ),

    # ── ALIAS_MASKED — verified in bounded_loops/graph/adapters/persistence/audit_store.py ──
    # LocalAuditStore imports every serde helper under a private alias (e.g. plan_to_dict as
    # _plan_to_dict). These functions were extracted from the adapter to the domain layer in P3
    # precisely so the Arena projection (an application module) can deserialise without importing
    # from the adapters layer; the Arena projection is not yet wired, so the current referrer
    # is LocalAuditStore's aliased import. The public names are invisible to the audit script.
    "plan_to_dict": (
        "ALIAS_MASKED: imported in audit_store.py line 28 as '_plan_to_dict'. Extracted to the "
        "domain layer in P3 so the Arena projection can deserialise AuditPlan without importing "
        "from the adapters layer."
    ),
    "result_to_dict": (
        "ALIAS_MASKED: imported in audit_store.py line 32 as '_result_to_dict'. Same extraction "
        "rationale as plan_to_dict."
    ),
    "artifact_to_dict": (
        "ALIAS_MASKED: imported in audit_store.py line 26 as '_artifact_to_dict'. Same extraction "
        "rationale as plan_to_dict."
    ),
    "repair_to_dict": (
        "ALIAS_MASKED: imported in audit_store.py line 30 as '_repair_to_dict'. Same extraction "
        "rationale as plan_to_dict."
    ),
    "artifact_from_mapping": (
        "ALIAS_MASKED: imported in audit_store.py line 25 as '_artifact_from_dict'. Same extraction "
        "rationale as plan_to_dict."
    ),
    "repair_from_mapping": (
        "ALIAS_MASKED: imported in audit_store.py line 29 as '_repair_from_dict'. Same extraction "
        "rationale as plan_to_dict."
    ),

    # ── SCRIPT_UTIL — verified in scripts/regenerate_reference_graphs.py ─────
    # Both functions are imported and called from scripts/regenerate_reference_graphs.py
    # (lines 29-40). That script lives in scripts/, which is outside bounded_loops/; the audit
    # root set covers only the package, so this real caller is invisible to it.
    "graphs_root": (
        "SCRIPT_UTIL: imported and called from scripts/regenerate_reference_graphs.py "
        "(line 29, 35). The audit root set covers bounded_loops/ only; scripts/ callers are "
        "outside scope."
    ),
    "render_reference_graph": (
        "SCRIPT_UTIL: imported and called from scripts/regenerate_reference_graphs.py "
        "(line 30, 40). Same caller as graphs_root."
    ),

    # ── API — verified by reading each symbol's docstring ────────────────────
    "KillSwitchTripped": (
        "API/COMPAT: per its own docstring: 'Kept rather than deleted because it is imported by "
        "tests/domain/test_errors.py and named in docs/ARCHITECTURE.md and the ports-and-adapters "
        "diagram, so removal would break an embedder that imports it.' The class is an explicitly "
        "documented public exception type."
    ),
    "quarantine_ignore": (
        "API: docstring says 'Stateless form, for callers that do not need the report.' An "
        "explicitly documented public alternative to the stateful _QuarantineFilter form, for "
        "callers passing a one-shot ignore= callback to shutil.copytree."
    ),
    "predecessors_admit": (
        "API: docstring says 'Boolean face of predecessors_admission for callers that only ask"
        " is it ready. A public convenience wrapper over the reachable predecessors_admission"
        " function, exposing a simpler bool interface for embedders."
    ),
    "offerable_values": (
        "API: docstring says 'The pickable values of one named field — the helper a form generator "
        "actually wants.' A public utility function in the monitor's schema_form module for "
        "external form-rendering code."
    ),
    "migrate_authoring_graph": (
        "API: 'Pure, explicit migrations for portable graph-authoring documents.' A public "
        "migration function for tools that load bounded-loops.dev/graph/v0 documents and need "
        "to upgrade them to v1 before processing."
    ),
    "validate_audit_coverage": (
        "API: 'Fail release coverage on missing, self-only, or open S0/S1 cells.' A public "
        "domain validation gate for release tooling; referenced in audit_reconciliation.py as "
        "the baseline coverage gate that reconcile_audit's cell-verdict logic mirrors."
    ),
    "validate_publication_evidence": (
        "API: 'Reject unsupported or contradicted factual claims before publication.' A public "
        "domain validation gate for publication pipelines; no current engine path invokes it "
        "directly, but it is a first-class part of the research-evidence domain contract."
    ),
    "verdict_is_wellformed": (
        "API: 'Delegates so there is exactly ONE validation implementation.' A public helper for "
        "gate implementors; its docstring explicitly says 'A second copy would drift' and this "
        "is the one the controller should call. node_receipts.py test_run_graph.py line 735 "
        "notes that verdict_is_wellformed reads each field and the controller reads them again."
    ),
    "write_state_document": (
        "API: 'Atomically write markdown to path (UTF-8), overwriting any existing file.' A public "
        "adapter function for graph controllers to write STATE.md projections; no current "
        "graph controller calls it, but it is the intended write path for the STATE.md surface "
        "(ADR-12 D4)."
    ),
    "storage_root_for_loop": (
        "API: 'The storage_root a loop run should hand to run_store, or None for the default.' "
        "A public utility for embedders integrating loop runs with a project workspace; "
        "workspace.py line 29 cross-references it in the module docstring."
    ),
    "assets_dir": (
        "HARNESS: docstring says 'The packaged assets directory, for the tests that assert what "
        "ships.' An explicit test helper in monitor/server.py; never called from a user-visible "
        "code path."
    ),

    # ── PORT_IMPL — verified from each class's docstring ─────────────────────
    # These are concrete implementations of ports (Protocols). The engine does not hard-call any
    # of them; deployment wiring or test fixtures select the implementation. They are reachable
    # only from tests right now because the graph memory and exec-transport subsystems have not
    # been wired into a production composition entry point yet.
    "InMemoryGraphMemoryStore": (
        "PORT_IMPL: docstring says 'reference used in tests and single-process runs. A durable "
        "SLM-backed adapter satisfies the same port and is a deployment binding.' Implements "
        "GraphMemoryStorePort; wired by deployment or test fixtures, not by the engine directly."
    ),
    "InMemorySemanticMemory": (
        "PORT_IMPL: docstring says 'Reference SemanticMemoryPort: tenant-bound, deterministic "
        "keyword-overlap ranking... so the port CONTRACT — namespace scoping, score ordering, "
        "limit, fail-closed validation — is testable without a real semantic backend.' Implements "
        "SemanticMemoryPort for tests."
    ),
    "KeyValueBackedMemoryStore": (
        "PORT_IMPL: docstring says 'A durable GraphMemoryStorePort over any DurableKeyValuePort "
        "(SLM in production).' The production deployment binding for the memory spine; not yet "
        "wired into a composition entry point."
    ),
    "SqliteDurableKeyValue": (
        "PORT_IMPL: docstring says 'A DurableKeyValuePort backed by a single SQLite file.' The "
        "production deployment binding for KeyValueBackedMemoryStore; not yet wired into a "
        "composition entry point."
    ),
    "SlmSemanticMemory": (
        "PORT_IMPL: docstring says 'SemanticMemoryPort over an injected SlmClientPort, "
        "tenant-scoped by a single injection-safe scope tag.' The SLM-backed deployment adapter "
        "for semantic memory; not yet wired into a composition entry point."
    ),
    "SuperlocalMemorySlmClient": (
        "PORT_IMPL: docstring says 'Concrete SlmClientPort backed by the real superlocalmemory "
        "package.' The production backend for SlmSemanticMemory; not yet wired into a composition "
        "entry point."
    ),
    "MappingCredentialResolver": (
        "PORT_IMPL: docstring says 'Resolve from a static binding_id -> ProviderCredential map "
        "supplied by the deployment.' A deployment binding for CredentialResolverPort; wired by "
        "deployment configuration, not by the engine directly."
    ),
    "LoopbackExecTransport": (
        "PORT_IMPL: docstring says 'Reference RemoteExecTransport: a self-hosted exec sidecar on "
        "loopback.' A reference implementation and test seam for RemoteExecTransport; 'opener' is "
        "an explicit test seam documented in the class's docstring."
    ),
    "OpenSandboxTransport": (
        "PORT_IMPL: docstring says 'A RemoteExecTransport backed by an OpenSandbox execd "
        "endpoint.' A production deployment adapter; 'allow_offhost=True' is its operator-opt-in "
        "for non-loopback enterprise deployments. Not yet wired into a composition entry point."
    ),
}
