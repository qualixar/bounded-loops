"""MCP discovery tools: what this engine can do, what ships with it, and what fits a task.

These three tools are what let a host — Claude Code, Codex, Cursor, anything speaking MCP —
*orchestrate* bounded loops rather than guess at them. The order matters:

* `bl_capabilities` — the contract. Read once; reason from it instead of from priors.
* `bl_catalog` — what already exists, so work is reused rather than reinvented.
* `bl_search_loops` — which existing loops fit a described task.

All three are READ-ONLY. Nothing here starts a run, records an approval, writes a file, or takes
a secret. The two side-effecting tools on the whole MCP surface are `bl_run` (gated by a
server-side confirm) and `graph_approve_tool` (whose subject comes from the authenticated MCP
session, never from a model argument).

A deliberate honesty note about `bl_search_loops`: the ranking is **lexical**. It matches words,
it does not understand meaning, and the response says so. A host model that treats a lexical
score as semantic judgement will pick the wrong loop and blame the engine.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from bounded_loops.domain.errors import ManifestError

from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
from bounded_loops.graph.application.capability_report import capability_report

# Layering note, recorded rather than hidden: the catalog discovery lives in `cli_loops` because
# `bl loops list` was its first caller. Importing it here is a layer inversion (an MCP adapter
# reaching into a CLI adapter). It is still the right call: the alternative is a second
# implementation of "find every loop package on this machine", and two answers to that question
# is the defect class this project keeps paying for. Moving it into `application/` means moving
# the seams the existing `cli_loops` tests patch, which belongs in its own change.
from bounded_loops.cli_loops import _collect_loop_entries, _matches_filters
from bounded_loops.graph.application.slm_bridge import CONTRACT_ID

# Words too common in a task description to carry ranking signal. Kept tiny and explicit rather
# than pulling in a stopword corpus — every entry here is a word that appeared in a real query
# and matched nearly every loop.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does", "for",
        "from", "get", "has", "have", "how", "i", "if", "in", "is", "it", "its", "make", "me",
        "my", "need", "of", "on", "or", "our", "should", "that", "the", "then", "there",
        "these", "this", "to", "up", "use", "want", "was", "we", "what", "when", "which",
        "who", "will", "with", "would", "you", "your",
    }
)

_WORD = re.compile(r"[a-z0-9]+")

# Where a match was found -> how much it counts. A name match is the strongest signal a lexical
# ranker has; a description match is the weakest because descriptions are prose.
_FIELD_WEIGHTS: Mapping[str, int] = {
    "name": 5,
    "roles": 3,
    "gate_kind": 3,
    "description": 1,
}


def register(mcp: object) -> None:
    """Wire the discovery tools onto an MCPServer instance. Thin glue; the logic is pure below."""
    tool: Callable[..., Any] = mcp.tool  # type: ignore[attr-defined]

    @tool()
    def bl_capabilities() -> dict:
        """What bounded-loops can actually do — READ THIS BEFORE AUTHORING A GRAPH.

        Returns node kinds with their kind-specific fields, gate kinds and what each
        MECHANICALLY checks, isolation tiers and what each enforces ON THIS HOST, which failure
        policies are declared versus honoured, the repair contract and its bound, the effect
        vocabulary, every budget field and where it is enforced, the terminal statuses and which
        of them are NOT success, and all 37 refusals with the fix for each.

        Read-only. Takes no arguments and no secret."""
        return {
            "status": "ok",
            "capabilities": dict(capability_report(platform=platform_snapshot())),
        }

    @tool()
    def bl_catalog(
        role: str | None = None,
        gate_kind: str | None = None,
        keyless: bool | None = None,
    ) -> dict:
        """The loop packages available on this machine, with optional filters.

        Each entry reports name, roles, gate kind, whether it is keyless (needs no API key),
        runner, rung, one-line description, and path. Prefer reusing one of these over authoring
        a new loop: a shipped package already has an independent gate, which is the part that is
        hard to get right.

        Read-only."""
        return {"status": "ok", **catalog(role=role, gate_kind=gate_kind, keyless=keyless)}

    @tool()
    def bl_search_loops(task_description: str, limit: int = 8) -> dict:
        """Rank the loop catalog against a described task. LEXICAL matching, not semantic.

        Scores word overlap against each loop's name, roles, gate kind, and description. It does
        NOT understand meaning — treat the result as candidates to read, never as a decision. An
        empty result means no shipped loop shares vocabulary with the description, which is a
        signal to author a new loop (and a ticket), not that nothing is possible.

        Read-only."""
        return {"status": "ok", **search_loops(task_description, limit=limit)}

    @tool()
    def bl_graph_terminal_runs(limit: int = 100) -> dict:
        """List the FINISHED graph runs in this workspace, newest first. Read-only.

        The discovery half of the `bounded-loops.dev/slm-bridge/v1` evidence contract: poll
        this, diff it against what you have already observed, then call `bl_graph_evidence`
        for the ones that are new. Runs still in flight are omitted, not reported with a
        placeholder state.

        Each entry is `{run_id, run_state, terminal_at}` and nothing else. No paths."""
        from bounded_loops.graph.slm_evidence import terminal_runs
        from bounded_loops.workspace import discover

        return {
            "status": "ok",
            "contract": CONTRACT_ID,
            "runs": terminal_runs(discover(), limit=limit),
        }

    @tool()
    def bl_graph_evidence(run_ref: str) -> dict:
        """Evidence for ONE finished graph run, as `bounded-loops.dev/slm-bridge/v1`. Read-only.

        For a memory or analytics system that wants to observe what this engine did without
        importing it, parsing its receipt files, or pinning its package version. Branch on
        `contract`, never on `engine.version` — the version says which build produced the
        document, the contract says what you may rely on.

        `run_ref` is the ADDRESS of a run in this workspace — the value `bl_graph_terminal_runs`
        returns under that same key. It is never a path: it is validated against the same
        allow-list the run store uses, so `../` is refused rather than resolved.

        Do not pass `run_id`. That is the run's own immutable IDENTITY, recorded inside its
        receipts and returned inside the evidence document; a run frequently lives in a
        directory named something else, so the identity will not resolve.

        Refuses a run that has not finished. Carries digests, states, outcome, attempt counts
        and receipt head — never gate prose, artifact bytes, paths, commands or environment
        values.

        Read this before acting on it: `demonstration: true` means a cassette replay that
        proves the wiring and nothing about the work, and `eligible_for_learning` is always
        false. This is observation. It does not authorize learning, ranking or routing."""
        from bounded_loops.graph.application.slm_bridge import EvidenceUnavailable
        from bounded_loops.graph.slm_evidence import evidence_for_run
        from bounded_loops.workspace import discover

        try:
            return {"status": "ok", "evidence": evidence_for_run(discover(), run_ref)}
        except EvidenceUnavailable as exc:
            # A refusal, not a crash. The consumer polls; "not finished yet" is an ordinary
            # answer and must not look like a broken tool.
            #
            # `public_reason`, NEVER str(exc). The underlying exceptions name files, so
            # returning the full text put the operator's workspace path on the consumer's
            # message bus every time an incomplete run was polled — while the success
            # document was being sanitized field by field. The failure path is the frequent
            # one, and it was the unguarded one.
            return {
                "status": "unavailable",
                "contract": CONTRACT_ID,
                "reason": exc.public_reason,
            }


# ── pure logic, reusable by the CLI and the UI ────────────────────────────────


def catalog(
    *,
    role: str | None = None,
    gate_kind: str | None = None,
    keyless: bool | None = None,
) -> dict[str, Any]:
    """Filtered catalog entries plus the counts a caller needs to interpret them."""
    entries = _collect_loop_entries()
    selected = [
        entry
        for entry in entries
        if _matches_filters(entry, role, gate_kind, bool(keyless))
        # `_matches_filters` takes a two-state `keyless_only` flag, so it cannot express
        # "only loops that DO need a key" — `False` and `None` both arrive as False and mean
        # "no filter". Rather than change that shared CLI helper's contract, the third state is
        # applied here: an explicit `keyless=False` means show me the ones needing credentials.
        and (keyless is not False or not entry["keyless"])
    ]
    return {
        "total_discovered": len(entries),
        "returned": len(selected),
        "filters": {"role": role, "gate_kind": gate_kind, "keyless": keyless},
        "loops": [_with_package_digest(entry) for entry in selected],
        "unreadable": [entry["name"] for entry in selected if entry["error"] is not None],
    }


def _with_package_digest(entry: dict[str, Any]) -> dict[str, Any]:
    """Add `loop_package`, the value a graph's `kind: loop` node must carry.

    Why the catalog and not a separate tool: composing a graph is `bl_catalog` → pick loops →
    write the manifest, and a required field that needs a second, differently-named call is a
    field that gets guessed. Before this, nothing on any surface returned a digest — the engine
    computed them internally and the reference-graph regeneration script had its own copy — so
    an orchestrator writing a loop node had exactly two moves, invent a hex string or leave a
    placeholder. `bl graph digest <dir>` is the same value for the CLI path.

    Deliberately NOT cached. A digest is a claim about current content; serving a remembered one
    after the package changed would defeat the check the compiler performs with it. Digesting all
    68 shipped packages measures ~150ms total, which is not worth trading correctness for.
    """
    if entry.get("error") is not None or not entry.get("path"):
        # Nothing to digest, and an entry that failed to load must not gain a field that makes
        # it look usable.
        return entry

    from bounded_loops.graph.adapters.workers.loop_packages import qualified_package_digest

    try:
        digest: str | None = qualified_package_digest(Path(entry["path"]))
        reason = None
    except (OSError, ValueError, ManifestError) as exc:
        # Report the failure in the field itself. A silently absent digest reads as "this loop
        # has none", which is not a thing that exists.
        digest, reason = None, f"cannot digest this package — {exc}"

    return {**entry, "loop_package": digest, "loop_package_error": reason}


def search_loops(task_description: str, *, limit: int = 8) -> dict[str, Any]:
    """Lexically rank catalog entries against `task_description`.

    Returns only entries that matched at least one meaningful word. Scoring every loop and
    returning the top N regardless would hand back a confident ranking of things that share
    nothing with the query — the failure mode where a host model picks loop #1 of 68 because it
    was first alphabetically among the zeroes.
    """
    terms = _terms(task_description)
    entries = _collect_loop_entries()

    scored: list[dict[str, Any]] = []
    for entry in entries:
        if entry["error"] is not None:
            continue
        score, matched = _score(entry, terms)
        if score > 0:
            scored.append({**entry, "score": score, "matched_terms": sorted(matched)})

    scored.sort(key=lambda item: (-item["score"], item["name"]))
    capped = max(1, min(int(limit), 50))

    return {
        "query_terms": sorted(terms),
        "ranking": "lexical",
        "ranking_caveat": (
            "Word overlap only. This does not understand the task — read the candidates and "
            "check that each one's gate actually verifies what you need."
        ),
        "total_scored": len(scored),
        "returned": len(scored[:capped]),
        "candidates": scored[:capped],
        "no_match_means": (
            "No shipped loop shares vocabulary with this description. Author a new loop with a "
            "mechanical gate, and record what it must check as a ticket."
        )
        if not scored
        else None,
    }


def _terms(task_description: str) -> frozenset[str]:
    """Meaningful lowercase words in a query: no stopwords, nothing shorter than three chars."""
    words = _WORD.findall(task_description.lower())
    return frozenset(word for word in words if len(word) >= 3 and word not in _STOPWORDS)


def _score(entry: Mapping[str, Any], terms: frozenset[str]) -> tuple[int, set[str]]:
    """Weighted word-overlap score for one entry, and which query terms hit."""
    score = 0
    matched: set[str] = set()
    for field, weight in _FIELD_WEIGHTS.items():
        haystack = _haystack(entry.get(field))
        for term in terms:
            if term in haystack:
                score += weight
                matched.add(term)
    return score, matched


def _haystack(value: object) -> frozenset[str]:
    """The searchable words of one field, whether it holds a string or a list of them."""
    if isinstance(value, str):
        return frozenset(_WORD.findall(value.lower()))
    if isinstance(value, (list, tuple)):
        words: set[str] = set()
        for item in value:
            if isinstance(item, str):
                words.update(_WORD.findall(item.lower()))
        return frozenset(words)
    return frozenset()
