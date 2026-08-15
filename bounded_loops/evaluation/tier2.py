"""Tier 2 — semantic mutants, authored from the STATED PURPOSE with the checker withheld.

Tier 1 asks one question of every gate mechanically: does it notice when the artifact is gone? That
claim is certain without reading anything, which is what makes it blind — and also what makes it
shallow. It cannot express "this clause now says the opposite of what it must", "this citation is
to a case that does not exist", or "this test file was deleted so the module it covered is bare".

Those need someone who knows what the loop is FOR. Tier 2 gets that from each loop's `PROMPT.md`
and `README.md` — its stated purpose — with `seed/check_*.py` withheld from the author.

**Why these are committed data and not generated code.** A language model's output is not
reproducible from a seed, so a Tier 2 corpus that regenerated itself would differ every run and no
reviewer could check the number against the mutants that produced it. They are therefore authored
once, written down, and reviewed like any other fixture.

**What makes "held out" auditable rather than promised.** Every mutant records the digest of the
exact prompt it was authored from. `tests/evaluation/test_tier2_authoring_was_blind.py` re-derives
that digest from the loop's own files and fails if it does not match — so a prompt that had been
quietly widened to include the checker cannot pass unnoticed. The claim is not "we did not look";
it is "here is what we looked at, recompute it yourself".

**The label still comes from the operation.** An author states which requirement of the stated
purpose the edit violates, and that sentence is checkable by a human reading `PROMPT.md` alone.
Nothing is labelled by running a gate, so the equivalent-mutant problem does not arise here either.

**Two failure modes this format is shaped against**, both learned in Tier 1:

* An author cannot mark a mutant INCORRECT without naming the requirement it breaks. Tier 1
  recorded false accepts against `conventional-commits`, `test-presence-per-module` and
  `broken-internal-links` that were really mislabelled mutants, because its claim was asserted
  once for the whole family instead of per artifact.
* A mutant must differ from the baseline it was authored against. A no-op carrying an INCORRECT
  label is a guaranteed false accept and a fabricated result.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from bounded_loops.evaluation.mutation import (
    LABEL_CORRECT,
    LABEL_INCORRECT,
    Mutation,
)

#: Where authored mutants live. Committed, reviewable, and readable without running anything.
CORPUS_FILENAME = "tier2-corpus.json"

#: Bumped when the record shape changes, so a stale corpus is detectable rather than half-read.
TIER2_VERSION = "1"

#: The files an author is allowed to see. `seed/check_*.py` is deliberately absent: it is the
#: implementation under measurement, and an author who has read it writes mutants shaped around
#: what it happens to catch.
AUTHORING_SOURCES = ("PROMPT.md", "README.md")


def authoring_prompt_digest(loop_dir: Path) -> str:
    """Content address of exactly what an author was shown for this loop.

    Recomputable by anyone with the repository, which is the point: it turns "the checker was
    withheld" from an assurance into an arithmetic fact a reviewer can verify. If a later author
    widened their inputs, this digest changes and the guard test fails.
    """
    parts: list[str] = []
    for name in AUTHORING_SOURCES:
        path = loop_dir / name
        parts.append(f"--- {name} ---")
        parts.append(path.read_text(encoding="utf-8") if path.is_file() else "(absent)")
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def authoring_prompt(loop_dir: Path) -> str:
    """The verbatim prompt an author is given. Never includes a checker.

    Built here rather than typed into a chat window so that what was shown is reproducible, and so
    the digest above describes something real.
    """
    sections = [
        "You are writing test material for an evaluation of an automated quality gate.",
        "",
        "You will be shown ONLY the stated purpose of a task. You will NOT be shown the checker",
        "that judges it, and you must not guess at its implementation. Write edits that violate",
        "the PURPOSE as stated. Whether any particular checker notices is the thing being",
        "measured, and is none of your concern.",
        "",
        "For each edit, state which requirement of the stated purpose it violates, in one",
        "sentence a reader can check against the text below.",
        "",
    ]
    for name in AUTHORING_SOURCES:
        path = loop_dir / name
        sections.append(f"--- {name} ---")
        sections.append(path.read_text(encoding="utf-8") if path.is_file() else "(absent)")
    return "\n".join(sections)


@dataclass(frozen=True)
class Tier2Mutant:
    """One authored semantic mutant, with the provenance that makes it auditable."""

    loop: str
    path: str
    mutated_text: str
    label: str
    #: Which requirement of the STATED PURPOSE this violates (or preserves). Checkable by a human
    #: against `PROMPT.md` without running anything.
    requirement: str
    #: Which model or person authored it. Recorded so a corpus dominated by one author's blind
    #: spots is visible rather than inferred.
    authored_by: str
    #: `authoring_prompt_digest` at the time of authoring.
    prompt_digest: str

    def __post_init__(self) -> None:
        if self.label not in (LABEL_CORRECT, LABEL_INCORRECT):
            raise ValueError(f"{self.loop}: unknown label {self.label!r}")
        if not self.requirement.strip():
            raise ValueError(
                f"{self.loop}: a mutant must name the requirement it violates — an unexplained "
                "label cannot be checked by a reviewer and cannot be argued with"
            )
        if not self.path or self.path.startswith("/") or ".." in self.path:
            raise ValueError(f"{self.loop}: unsafe path {self.path!r}")

    @property
    def mutant_id(self) -> str:
        return f"{self.loop}::tier2::{self.path.replace('/', '_')}::{self.digest[7:19]}"

    @property
    def digest(self) -> str:
        payload = f"{self.loop}\0{self.path}\0{self.mutated_text}".encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def as_mutation(self) -> Mutation:
        """Adapt to the Tier-1 `Mutation` so one harness runs both tiers.

        The family is derived from the label rather than carried, for the same reason Tier 1
        derives the label from the family: the only way to change what a mutant asserts should be
        to change what it does.
        """
        from bounded_loops.evaluation.mutation import FAMILY_DESTROYING, FAMILY_PRESERVING

        return Mutation(
            operator=f"tier2.{self.authored_by}",
            family=FAMILY_DESTROYING if self.label == LABEL_INCORRECT else FAMILY_PRESERVING,
            path=self.path,
            mutated_text=self.mutated_text,
            rationale=self.requirement,
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "loop": self.loop,
            "path": self.path,
            "label": self.label,
            "requirement": self.requirement,
            "authored_by": self.authored_by,
            "prompt_digest": self.prompt_digest,
            "digest": self.digest,
            "mutated_text": self.mutated_text,
        }


def load(path: Path) -> list[Tier2Mutant]:
    """Read a committed Tier-2 corpus. Raises on a shape this version cannot read."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if str(document.get("tier2_version")) != TIER2_VERSION:
        raise ValueError(
            f"tier2 corpus version {document.get('tier2_version')!r} != {TIER2_VERSION!r}; "
            "refusing to read a shape this build may misinterpret"
        )
    return [
        Tier2Mutant(
            loop=record["loop"], path=record["path"], mutated_text=record["mutated_text"],
            label=record["label"], requirement=record["requirement"],
            authored_by=record["authored_by"], prompt_digest=record["prompt_digest"],
        )
        for record in document.get("mutants", [])
    ]


def dump(mutants: list[Tier2Mutant]) -> dict[str, Any]:
    """The committed document, with counts a reviewer can scan without recomputing."""
    by_author: dict[str, int] = {}
    by_label: dict[str, int] = {}
    for mutant in mutants:
        by_author[mutant.authored_by] = by_author.get(mutant.authored_by, 0) + 1
        by_label[mutant.label] = by_label.get(mutant.label, 0) + 1

    return {
        "tier2_version": TIER2_VERSION,
        "total": len(mutants),
        "loops": len({m.loop for m in mutants}),
        "by_author": dict(sorted(by_author.items())),
        "by_label": dict(sorted(by_label.items())),
        "authoring_sources": list(AUTHORING_SOURCES),
        "held_out": (
            "Authors were shown only the files listed in authoring_sources. seed/check_*.py was "
            "withheld. Each mutant records the digest of exactly what its author saw; "
            "tests/evaluation/test_tier2_authoring_was_blind.py recomputes it from the repository."
        ),
        "mutants": [m.as_record() for m in sorted(mutants, key=lambda m: m.mutant_id)],
    }
