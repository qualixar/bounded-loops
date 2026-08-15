"""The operator registry: which edits apply to which kind of file.

Dispatch is on **file extension only**. That is the constraint that makes the corpus held out —
an operator that could see the loop's gate, its manifest, or its name would be able to avoid
producing the mutants that gate happens to miss, which is the one bias that would invalidate every
number this corpus produces. `tests/evaluation/test_generator_is_blind.py` asserts it at the AST
level rather than trusting this paragraph.

`destroying` operators apply to every text file, because their claim ("an emptied artifact cannot
satisfy any purpose") does not depend on format. `preserving` operators are format-specific,
because proving an edit changed nothing requires a parser.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from bounded_loops.evaluation.mutation import Mutation
from bounded_loops.evaluation.operators import destroying, preserving

#: `(path, text) -> mutations`. Deterministic and side-effect free: no clock, no RNG, no I/O.
Operator = Callable[[str, str], list[Mutation]]

#: Applied to every mutable artifact whatever its type. Each carries a claim that holds regardless
#: of the artifact's structure, which is what lets the label be certain without reading a gate.
#:
#: `destroying.truncate_to_first_line` is deliberately ABSENT. Its claim — incomplete therefore
#: incorrect — fails for homogeneous lists, where every prefix of a valid list is valid. It stayed
#: registered long enough to record one false accept against `conventional-commits` that was
#: really a mislabelled mutant. See its docstring.
UNIVERSAL: tuple[Operator, ...] = (
    destroying.empty_file,
    destroying.whitespace_only,
    destroying.filler_text,
)

#: Applied only where a parser can prove the edit preserving.
BY_EXTENSION: Mapping[str, tuple[Operator, ...]] = {
    ".json": (preserving.json_reindent, preserving.json_reorder_keys),
    ".py": (preserving.python_add_comment, preserving.python_add_blank_lines),
    ".md": (preserving.text_trailing_newline,),
    ".txt": (preserving.text_trailing_newline,),
}

#: Never mutated. Compiled bytecode is not a work product, and a lockfile's whole purpose is to be
#: machine-written — mutating either measures nothing about a gate's judgement.
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".so", ".lock"})

#: Never mutated, whatever the extension: build litter and VCS internals.
EXCLUDED_PARTS = frozenset({"__pycache__", ".git", "node_modules", ".venv"})


def is_mutable_artifact(relative_path: str) -> bool:
    """Whether this file is a work product a gate could reasonably judge."""
    parts = relative_path.split("/")
    if any(part in EXCLUDED_PARTS for part in parts):
        return False
    name = parts[-1]
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def operators_for(relative_path: str) -> Sequence[Operator]:
    """Every operator that applies to this path, universal first.

    Ordered deterministically so a corpus regenerated from the same seed is byte-identical — a
    published corpus a reviewer cannot reproduce is not evidence.
    """
    suffix = "." + relative_path.rsplit(".", 1)[-1] if "." in relative_path.split("/")[-1] else ""
    return (*UNIVERSAL, *BY_EXTENSION.get(suffix, ()))


def mutate(relative_path: str, text: str) -> list[Mutation]:
    """Every mutation available for one file. May be empty — an operator that cannot prove its
    edit, or would produce a no-op, emits nothing rather than a mutant nobody can label."""
    if not is_mutable_artifact(relative_path):
        return []
    found: list[Mutation] = []
    for operator in operators_for(relative_path):
        found.extend(operator(relative_path, text))
    return found
