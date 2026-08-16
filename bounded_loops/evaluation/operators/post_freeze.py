"""Post-freeze operators: the artifact keeps its SHAPE and loses its SUBSTANCE.

Why a second operator set exists at all
---------------------------------------
The shipped Tier-1 set has three destroying operators — `empty_file`, `whitespace_only`,
`filler_text` — and all three destroy the whole artifact. That bluntness is deliberate: it is what
makes "this cannot satisfy any stated purpose" certain without consulting a gate. It is also a
single probe wearing three coats, and it can only find one class of defect: a gate satisfied by the
ABSENCE of the thing it checks.

It found that class, 47 times. Then the gates were repaired and the same corpus stopped finding
anything — which establishes that these operators are exhausted against these gates, and nothing
more. A rate measured by the operators that drove the repairs is a saturation figure.

This module is the fresh probe. Its claim is different and its blind spot is different:

    An artifact that retains its structure and has had its CONTENT emptied cannot serve a stated
    purpose either.

That is certain by construction for the same reason `empty_file` is — a document of empty values
asserts nothing, a module of empty functions computes nothing — while being invisible to any gate
that checks shape and not substance. A gate asserting "the runbook has all seven required
headings" passes a runbook of seven headings and no content. A gate asserting "every required key
is present" passes a payload whose every value is empty. Those gates survive the shipped
operators untouched, because the shipped operators delete the headings and the keys too.

Discipline this module inherits and must not break
--------------------------------------------------
* **Dispatch on file extension only.** No operator here may read a gate, a manifest, a loop name,
  or a `check_*.py`. `tests/evaluation/test_generator_is_blind.py` asserts it over this module's
  syntax tree, same as the others.
* **Deterministic, no I/O, no clock, no RNG.** A corpus a reviewer cannot regenerate byte for byte
  is not evidence.
* **An operator that cannot prove its claim emits nothing.** A mutant nobody can label is worse
  than a missing mutant, because it enters a denominator.
* **PRESERVING is verified, never asserted** — the edit must parse to an equal document or an
  identical syntax tree, checked here, or the operator yields nothing.
"""

from __future__ import annotations

import ast
import json
from typing import Any

from bounded_loops.evaluation.mutation import FAMILY_DESTROYING, FAMILY_PRESERVING, Mutation


# ── the precondition, derived from adjudicating this family's first run ───────────────────────
#
# E7 produced 21 apparent false accepts and a spec review found 7 of them mislabelled. They were
# not 7 accidents; they were two failures of one assumption:
#
#     A content-removal operator's DESTROYING claim is certain only for a requirement universally
#     quantified over content the operator removes. It is FALSE for a requirement that is negative
#     ("must not contain X") — removing content removes X — and FALSE for a requirement satisfied
#     by structure the operator preserves: a name, a signature, a key, a heading.
#
# `no-hardcoded-sleep` asks that no test contain `time.sleep`; stubbing bodies deletes the sleeps
# and satisfies it. `type-annotations-present` asks that signatures be annotated; stubbing bodies
# keeps every signature. In both the gate was right and the operator was wrong.
#
# The rule is enforced by REFUSING TO EMIT. A mutant nobody can label is worse than a missing
# mutant, because it enters a denominator and moves a published rate. This is the same discipline
# `tier1_claim_holds` applies to multi-artifact loops, generalised.
#
# The requirement is read from the loop's stated purpose — the same material a Tier-2 author is
# shown — and never from a gate. `is_content_quantified` takes the text, so no operator here
# performs I/O and the AST blindness assertion is unaffected.

_NEGATIVE_MARKERS = (
    "no test contains", "never pair", "never contain", "must not", "does not contain",
    "no hardcoded", "no credential", "not be present", "free of", "without any",
)

_STRUCTURAL_MARKERS = (
    "is named", "are named", "naming", "annotated", "annotation", "signature",
    "spelled", "cased", "casing",
)


def is_content_quantified(stated_purpose: str) -> bool:
    """Whether a content-removal operator may claim *destroying* for this requirement.

    Fails CLOSED: an unrecognised requirement yields ``False`` and no mutant, because the honest
    output of an unmeasurable question is a refusal rather than a guess that reaches a denominator.
    """
    text = " ".join(stated_purpose.lower().split())
    if any(marker in text for marker in _NEGATIVE_MARKERS):
        return False
    if any(marker in text for marker in _STRUCTURAL_MARKERS):
        return False
    return any(marker in text for marker in ("every ", "each ", "all ", "no fewer than"))


def _destroying(operator: str, path: str, text: str, rationale: str) -> Mutation:
    return Mutation(
        operator=operator, family=FAMILY_DESTROYING, path=path,
        mutated_text=text, rationale=rationale,
    )


def _preserving(operator: str, path: str, text: str, rationale: str) -> Mutation:
    return Mutation(
        operator=operator, family=FAMILY_PRESERVING, path=path,
        mutated_text=text, rationale=rationale,
    )


# ── destroying: structure kept, substance removed ─────────────────────────────────────────────


def _hollow(value: Any) -> Any:
    """Replace every leaf with an empty value of its own type, keeping every key and shape."""
    if isinstance(value, dict):
        return {key: _hollow(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return []
    if isinstance(value, str):
        return ""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    return None


def json_values_emptied(path: str, text: str) -> list[Mutation]:
    """Every key survives; every value becomes empty.

    The claim: a payload whose every field is present and empty asserts nothing, so it cannot
    satisfy a requirement about what the document says. A schema demanding only that required keys
    EXIST still passes it, which is the point — that gate is checking shape, and shape is exactly
    what this operator preserves.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, (dict, list)) or not parsed:
        return []
    hollowed = _hollow(parsed)
    if hollowed == parsed:
        return []  # already empty: the edit is a no-op and would be an unlabellable mutant
    return [_destroying(
        "json_values_emptied", path, json.dumps(hollowed, indent=2) + "\n",
        "every key retained, every value emptied: the document has its shape and no content",
    )]


def json_arrays_emptied(path: str, text: str) -> list[Mutation]:
    """Every array becomes empty; scalars are untouched.

    Separated from `json_values_emptied` because it is the narrower and more realistic edit: a
    document that still carries its identifying scalars and has lost its collections. Any gate
    quantifying over a collection ("every transaction is categorised") passes it vacuously, which
    is the shape of the ledger defect stated in the paper.
    """
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip(inner) for key, inner in value.items()}
        if isinstance(value, list):
            return []
        return value

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    stripped = strip(parsed)
    if stripped == parsed:
        return []  # no arrays to empty
    return [_destroying(
        "json_arrays_emptied", path, json.dumps(stripped, indent=2) + "\n",
        "every collection emptied, scalars kept: a universal claim over any of them is vacuous",
    )]


def markdown_headings_only(path: str, text: str) -> list[Mutation]:
    """Every heading survives; everything under it is deleted.

    The claim: a document of headings with nothing beneath them does not state anything, so it
    cannot satisfy a requirement about its content. A gate checking that the required section
    titles are present passes it.
    """
    lines = text.splitlines()
    headings = [line for line in lines if line.lstrip().startswith("#")]
    if not headings or len(headings) == len(lines):
        return []
    return [_destroying(
        "markdown_headings_only", path, "\n".join(headings) + "\n",
        "every heading kept, every body line removed: the section titles claim content that is absent",
    )]


def csv_header_only(path: str, text: str) -> list[Mutation]:
    """The header row survives; every data row is deleted."""
    lines = text.splitlines()
    if len(lines) < 2:
        return []
    return [_destroying(
        "csv_header_only", path, lines[0] + "\n",
        "columns declared, no rows: any per-row obligation holds vacuously",
    )]


def python_bodies_stubbed(path: str, text: str) -> list[Mutation]:
    """Every function and method keeps its signature and loses its body.

    The claim: a module whose functions all do nothing computes nothing. For a test module this is
    the sharpest form — the tests still collect, still run, still pass, and assert nothing. A gate
    that runs a suite and checks the exit code cannot tell the difference.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    class _Stub(ast.NodeTransformer):
        touched = False

        def _blank(self, node: ast.AST) -> ast.AST:
            body = getattr(node, "body", [])
            # A body that is already a single pass/docstring is not worth emitting as a mutant.
            if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Expr)):
                return node
            self.touched = True
            node.body = [ast.Pass()]  # type: ignore[attr-defined]
            return node

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            return self._blank(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
            return self._blank(node)

    stubber = _Stub()
    stubbed = stubber.visit(tree)
    if not stubber.touched:
        return []
    ast.fix_missing_locations(stubbed)
    try:
        rendered = ast.unparse(stubbed)
    except (AttributeError, ValueError):
        return []
    return [_destroying(
        "python_bodies_stubbed", path, rendered + "\n",
        "every signature kept, every body replaced by pass: it imports, it runs, it asserts nothing",
    )]


# ── preserving: verified equal, so a rejection is a false reject ───────────────────────────────


def json_unicode_escaped(path: str, text: str) -> list[Mutation]:
    """Re-encode with `ensure_ascii=True`. Verified to parse back to an equal document."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    candidate = json.dumps(parsed, ensure_ascii=True, indent=2) + "\n"
    if candidate == text:
        return []
    try:
        if json.loads(candidate) != parsed:
            return []  # could not prove it preserving, so emit nothing
    except (ValueError, TypeError):
        return []
    return [_preserving(
        "json_unicode_escaped", path, candidate,
        "non-ASCII escaped; parses to an equal document, so a rejection is a false reject",
    )]


def text_crlf_line_endings(path: str, text: str) -> list[Mutation]:
    """LF to CRLF. Verified to normalise back to the original."""
    if "\r\n" in text or "\n" not in text:
        return []
    candidate = text.replace("\n", "\r\n")
    if candidate.replace("\r\n", "\n") != text:
        return []
    return [_preserving(
        "text_crlf_line_endings", path, candidate,
        "line endings only; the content is identical once normalised",
    )]


def python_return_parenthesised(path: str, text: str) -> list[Mutation]:
    """Wrap every returned expression in parentheses. Verified to produce an identical AST."""
    try:
        original = ast.parse(text)
    except SyntaxError:
        return []
    returns = [node for node in ast.walk(original) if isinstance(node, ast.Return) and node.value]
    if not returns:
        return []
    try:
        candidate = ast.unparse(original)
    except (AttributeError, ValueError):
        return []
    candidate = candidate.replace("return ", "return (", 1)
    if "return (" not in candidate:
        return []
    # Close the parenthesis at the end of that logical line.
    lines = candidate.splitlines()
    for index, line in enumerate(lines):
        if "return (" in line and not line.rstrip().endswith(")"):
            lines[index] = line.rstrip() + ")"
            break
    candidate = "\n".join(lines) + "\n"
    try:
        if ast.dump(ast.parse(candidate)) != ast.dump(ast.parse(text)):
            return []  # could not prove it preserving
    except SyntaxError:
        return []
    return [_preserving(
        "python_return_parenthesised", path, candidate,
        "a returned expression parenthesised; the syntax tree is identical",
    )]


#: Applied to every mutable artifact, like the shipped universal set. Empty here on purpose: every
#: operator in this module needs a parser to prove its claim, so each is dispatched by extension.
UNIVERSAL: tuple = ()

#: Extension dispatch. The only input any operator receives is the path suffix and the bytes.
BY_EXTENSION = {
    ".json": (json_values_emptied, json_arrays_emptied, json_unicode_escaped),
    ".md": (markdown_headings_only, text_crlf_line_endings),
    ".txt": (text_crlf_line_endings,),
    ".csv": (csv_header_only,),
    ".py": (python_bodies_stubbed, python_return_parenthesised),
}
