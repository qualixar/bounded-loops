"""Edits that remove the artifact's content entirely. Label: INCORRECT.

This is where the false-accept rate — α — comes from, and the claim behind the label is
deliberately the weakest one that is still decisive:

    **An artifact emptied of its content cannot satisfy any loop's stated purpose.**

That is true of a requirements file, a privacy policy, a runbook, a test suite and a Terraform
plan alike, so it needs no knowledge of what any particular gate checks. "Held out" is not a
promise about who wrote these — it is a property of a claim narrow enough to hold across all 68
loops without reading one of them.

**What this family is and is not.** It is not subtle. A gate that passes an empty file is not
failing an edge case; it is not checking. That is exactly the defect class already found by hand
across this catalog — a heading present with no content beneath it, an empty collection satisfying
a requirement vacuously, a presence check that a comment or a negation walks straight through. Six
such defects were confirmed in one afternoon before any of this existed. This family is the machine
that asks the same question of every gate at once.

The subtle violations — a clause that says the opposite of what it must, a citation to a case that
does not exist — are Tier 2, authored per loop from the stated purpose with the checker withheld.
They are a different family and are labelled the same way: by what the operation did.

**Why `empty` and `whitespace` are separate operators.** They differ by exactly one property: a
whitespace-only file is non-empty on disk and has a non-zero size. A gate that tests `if not
content` passes one and rejects the other, and knowing WHICH is the difference between a one-line
fix and a rewrite.
"""

from __future__ import annotations

from bounded_loops.evaluation.mutation import FAMILY_DESTROYING, Mutation

#: Non-empty, syntactically unremarkable, and unrelated to any loop in the catalog. Deliberately
#: not valid JSON/Python/YAML: this operator asks whether the gate looks at CONTENT, and a filler
#: that happened to parse would confound "does not check content" with "checks only syntax".
_FILLER = "lorem ipsum dolor sit amet\n"


def _mutation(operator: str, path: str, text: str, rationale: str) -> Mutation:
    return Mutation(
        operator=operator, family=FAMILY_DESTROYING, path=path,
        mutated_text=text, rationale=rationale,
    )


def empty_file(path: str, text: str) -> list[Mutation]:
    """Replace the artifact with nothing at all.

    The simplest question a gate can be asked, and one every gate in this catalog should answer
    with a rejection. Skipped when the artifact is ALREADY empty, since the mutation would be a
    no-op and a no-op carrying an INCORRECT label would count an unchanged file as a defect.
    """
    if not text:
        return []
    return [_mutation(
        "destroy.empty_file", path, "",
        "the artifact is empty; no loop's stated purpose can be satisfied by an empty file",
    )]


def whitespace_only(path: str, text: str) -> list[Mutation]:
    """Replace the content with blank lines — non-empty on disk, empty of meaning.

    Separates `if not content` from a real content check. A gate reading file SIZE, or testing
    truthiness of the raw string, passes this and rejects `empty_file`.
    """
    if not text.strip():
        return []
    return [_mutation(
        "destroy.whitespace_only", path, "\n\n   \n\n",
        "content replaced by whitespace; the file is non-empty on disk and empty of meaning",
    )]


def filler_text(path: str, text: str) -> list[Mutation]:
    """Replace the content with unrelated prose.

    Non-empty, well-formed as bytes, and about nothing. A gate that checks only that a file exists
    and is non-trivial passes this; one that checks what the file SAYS does not.
    """
    if not text.strip() or text == _FILLER:
        return []
    return [_mutation(
        "destroy.filler_text", path, _FILLER,
        "content replaced by unrelated prose; the artifact no longer addresses its purpose",
    )]


def truncate_to_first_line(path: str, text: str) -> list[Mutation]:
    """Keep only the first line.

    The partial case, and the most realistic of the four: a truncated write, an interrupted
    generation, a file cut off mid-document. Requires at least two non-blank lines, or it is not a
    truncation.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    candidate = lines[0] + "\n"
    if candidate == text:
        return []
    return [_mutation(
        "destroy.truncate", path, candidate,
        f"truncated from {len(lines)} non-blank lines to 1; the artifact is incomplete",
    )]
