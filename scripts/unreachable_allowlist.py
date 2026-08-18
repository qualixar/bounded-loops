"""Symbols with no reachable caller inside the engine, and WHY that is correct.

Read by ``audit_unreachable.py``. A symbol here is not a defect; a symbol NOT here with no reachable
caller is. Writing the reason down is the whole mechanism: the absence of a caller becomes something
a person had to justify and a reviewer can disagree with, instead of something nobody noticed. That
is the lesson from shipping an unreachable gate-plugin surface in a 0.6.8 changelog.

**Every reason in this file was checked, not assumed.** An entry whose justification was inferred
from a name rather than read from the code does not belong here — leaving a symbol in the failing
residual is strictly better than declaring it on a guess, because the residual gets looked at again.

Categories:
  PORT      a ``Protocol``. Nothing calls it; adapters satisfy it structurally.
  HARNESS   the evaluation apparatus. Driven by tests and the paper's experiment scripts, never by
            the engine. Making the engine depend on it would be the defect.
  CONTROL   exists so a measurement can be compared against it; deliberately unused in production.
  COMPAT    a migration boundary kept deliberately.
  API       a supported surface an embedder or deployment wires in.
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
}
