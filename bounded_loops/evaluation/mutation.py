"""One deliberate, labelled edit — the unit the corpus is built from.

**The label is decided BEFORE the edit is made.** That is the whole methodological point, and it is
what lets this corpus report a false-accept rate without an oracle: nothing inspects the mutated
artifact and rules on whether it is correct, because the operation that produced it already says so.

The equivalent-mutant problem does not arise for the same reason. Classical mutation testing has to
detect mutants that change nothing observable, and the standard treatment — run the test suite and
exclude the ones it cannot distinguish — is circular here, because for a `pytest` loop the suite IS
the gate. Every mutant the gate missed would be reclassified as equivalent and excluded, forcing the
false-accept rate to zero by construction. So the label never comes from behaviour.

Two families, and the asymmetry between them is deliberate:

    PRESERVING   label CORRECT     gate should pass   a rejection is a FALSE REJECT
    DESTROYING   label INCORRECT   gate should reject a pass      is a FALSE ACCEPT

`PRESERVING` is *verified*, not merely asserted: a JSON reformat must parse to an equal document, a
Python edit must produce an identical AST. An operator that cannot prove its own edit preserving
refuses to emit it. This matters because a mislabelled `CORRECT` mutant inflates the false-reject
rate against a gate that was right to say no.

`DESTROYING` is certain by construction rather than verified, because there is nothing to compare
against: an artifact emptied of content cannot satisfy any loop's stated purpose, whatever that
purpose is. That claim is deliberately weak enough to be true of every shipped loop without reading a
single gate — which is what "held out" means here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

#: Ground-truth labels. These are the strings `gate_metrics` reads from `node.outcome.labeled`,
#: so the corpus and the metrics module cannot drift into different vocabularies.
LABEL_CORRECT = "correct"
LABEL_INCORRECT = "incorrect"

#: What the operator did to the artifact, and therefore why its label is what it is.
FAMILY_PRESERVING = "preserving"
FAMILY_DESTROYING = "destroying"

_LABEL_FOR_FAMILY = {
    FAMILY_PRESERVING: LABEL_CORRECT,
    FAMILY_DESTROYING: LABEL_INCORRECT,
}


@dataclass(frozen=True)
class Mutation:
    """A single edit to one file, with the label its family fixes.

    Carries the mutated TEXT rather than a patch: the harness writes it into a throwaway copy of
    the loop, and a patch would need a hunk applier whose failure modes are a second source of
    mutants nobody labelled.
    """

    #: Stable identifier for the operator that produced this, e.g. `json.empty_object`.
    operator: str
    #: `PRESERVING` or `DESTROYING`. The label follows from this and nothing else.
    family: str
    #: Path relative to the loop directory, e.g. `seed/requirements.txt`.
    path: str
    #: The full replacement text for that file.
    mutated_text: str
    #: One sentence a reader can check the label against.
    rationale: str

    def __post_init__(self) -> None:
        if self.family not in _LABEL_FOR_FAMILY:
            raise ValueError(f"unknown mutation family: {self.family!r}")
        if not self.path or self.path.startswith("/") or ".." in self.path:
            # The path reaches the filesystem when the harness materialises a mutant.
            raise ValueError(f"mutation path must be a safe relative path: {self.path!r}")
        if not self.rationale.strip():
            # A label nobody can argue with is a label nobody can check.
            raise ValueError(f"{self.operator}: a mutation must state why its label holds")

    @property
    def label(self) -> str:
        """The ground-truth label, derived from the family — never set independently.

        A settable label would eventually be set to whatever made a result look better. Deriving it
        means the only way to change a mutant's label is to change what the edit DOES.
        """
        return _LABEL_FOR_FAMILY[self.family]

    @property
    def digest(self) -> str:
        """Content address of this mutant, so a published corpus is reproducible and checkable."""
        payload = f"{self.operator}\0{self.path}\0{self.mutated_text}".encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()
