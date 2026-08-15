"""Edits that provably change nothing a correct artifact asserts. Label: CORRECT.

These measure the number gate evaluations almost never publish — the **false-reject rate**. A gate
can drive its false-accept rate to zero by rejecting everything, and only this direction makes that
visible.

**Every operator here verifies its own edit before emitting it.** A JSON reformat must parse back to
an equal document; a Python edit must produce a byte-identical AST dump. If the check fails the
mutation is not emitted at all, because a mislabelled `CORRECT` mutant inflates the false-reject
rate against a gate that was right to say no — an error that makes the product look worse and the
measurement look better, which is the direction nobody catches.

That verification is also why these operators are format-aware while claiming nothing about the
loop: `json.loads` and `ast.parse` know about JSON and Python, not about what any gate wants.
"""

from __future__ import annotations

import ast
import json

from bounded_loops.evaluation.mutation import FAMILY_PRESERVING, Mutation


def _mutation(operator: str, path: str, text: str, rationale: str) -> Mutation:
    return Mutation(
        operator=operator, family=FAMILY_PRESERVING, path=path,
        mutated_text=text, rationale=rationale,
    )


# ── JSON ─────────────────────────────────────────────────────────────────────


def json_reindent(path: str, text: str) -> list[Mutation]:
    """Re-serialise with a different indent. The parsed document is unchanged.

    A gate that rejects this is reading the file as TEXT — matching a literal substring, or a regex
    over the raw bytes — while claiming to validate a document. That is a real defect and one this
    corpus can find mechanically.
    """
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    for indent in (4, None):
        candidate = json.dumps(document, indent=indent)
        if candidate == text:
            continue
        try:
            if json.loads(candidate) != document:
                continue  # cannot happen through json.dumps, but the label depends on it
        except (json.JSONDecodeError, ValueError):
            continue
        return [_mutation(
            "json.reindent", path, candidate,
            f"re-serialised with indent={indent}; parses to a document equal to the original",
        )]
    return []


def json_reorder_keys(path: str, text: str) -> list[Mutation]:
    """Sort object keys. JSON objects are unordered, so the document is the same document.

    Rejecting this means the gate depends on key ORDER, which nothing in the JSON data model
    guarantees — the same class of defect as depending on whitespace.
    """
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(document, dict) or len(document) < 2:
        return []

    candidate = json.dumps(document, indent=2, sort_keys=True)
    if candidate == text:
        return []
    if json.loads(candidate) != document:
        return []
    return [_mutation(
        "json.reorder_keys", path, candidate,
        "object keys sorted; JSON objects are unordered so the document is unchanged",
    )]


# ── Python ───────────────────────────────────────────────────────────────────


def _same_ast(original: str, candidate: str) -> bool:
    """Identical parse trees, ignoring position. The strongest available preservation proof."""
    try:
        return ast.dump(ast.parse(original)) == ast.dump(ast.parse(candidate))
    except SyntaxError:
        return False


def python_add_comment(path: str, text: str) -> list[Mutation]:
    """Prepend a comment line. Comments are not in the AST, so the module is unchanged.

    Verified by comparing `ast.dump` rather than assumed, because a file that fails to parse would
    otherwise be emitted as a `CORRECT` mutant and be counted against any gate that rejects it.
    """
    if not text.strip():
        return []
    candidate = "# corpus: semantics-preserving comment\n" + text
    if not _same_ast(text, candidate):
        return []
    return [_mutation(
        "python.add_comment", path, candidate,
        "a leading comment line; comments do not appear in the AST, verified by ast.dump equality",
    )]


def python_add_blank_lines(path: str, text: str) -> list[Mutation]:
    """Insert blank lines between top-level statements. Whitespace outside a block is not syntax.

    Deliberately does NOT touch indentation: in Python indentation IS syntax, so a "harmless
    whitespace" operator that re-indented would be emitting genuinely broken code under a CORRECT
    label. The AST check would catch it, and the operator would silently emit nothing — a family
    that quietly produces no mutants is worse than one that never existed.
    """
    lines = text.splitlines(keepends=True)
    if len(lines) < 2:
        return []
    candidate = "".join(line + ("\n" if not line.endswith("\n") else "") for line in lines)
    candidate = "\n".join(candidate.splitlines())
    candidate = candidate.replace("\n\n", "\n\n\n", 1)
    if candidate == text or not _same_ast(text, candidate):
        return []
    return [_mutation(
        "python.add_blank_line", path, candidate,
        "one extra blank line between statements; verified by ast.dump equality",
    )]


# ── text ─────────────────────────────────────────────────────────────────────


def text_trailing_newline(path: str, text: str) -> list[Mutation]:
    """Ensure exactly one trailing newline. POSIX says a text file should end with one.

    The weakest claim in this module, and stated as such: this is *conventionally* preserving rather
    than provably so, since no parser defines the meaning of a prose file. It stays because a gate
    that flips its verdict on a trailing newline is depending on something no author controls
    deliberately — but a reader weighing the false-reject rate should know this family is
    convention, not proof.
    """
    if not text.strip():
        return []
    candidate = text.rstrip("\n") + "\n"
    if candidate == text:
        return []
    return [_mutation(
        "text.trailing_newline", path, candidate,
        "normalised to a single trailing newline; conventionally meaning-preserving for prose",
    )]
