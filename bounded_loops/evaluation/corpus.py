"""Build the mutant corpus from the loop catalog — without ever reading a gate.

The generator sees three things about a loop: its directory, which files under `seed/` are mutable,
and the bytes in them. It does not read `loop.yaml`'s `gate:` block, does not import a checker, and
does not branch on a loop's name. That is not a convention — `tests/evaluation/test_generator_is_blind.py`
asserts it against the AST of every module in this package.

**Mutable means: in `seed/`, and not listed in the loop's `forbid`.** `forbid` is the loop's own
declaration of what the worker may not touch, which is invariably the checker itself. Reading it is
the one thing this module takes from `loop.yaml`, and it is read as a *path list*, never as a gate
description — a corpus that mutated the checker would be measuring whether a gate can detect edits
to itself, which is a different and far less interesting question.

The manifest is the reproducibility artifact. Every mutant carries the operator that made it, the
file it touched, its label, and a content digest, so a reviewer can regenerate the corpus and
compare digests rather than taking a table on trust.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

from bounded_loops.evaluation import operators
from bounded_loops.evaluation.mutation import Mutation

#: Bumped when the operator set or the manifest shape changes, so a stale corpus is detectable
#: rather than silently mixed with a fresh one.
CORPUS_VERSION = "1"


@dataclass(frozen=True)
class Mutant:
    """One mutation, bound to the loop it was generated for."""

    loop: str
    mutation: Mutation

    @property
    def mutant_id(self) -> str:
        """Stable, human-readable, and unique within a corpus."""
        safe_path = self.mutation.path.replace("/", "_")
        return f"{self.loop}::{self.mutation.operator}::{safe_path}"

    def as_manifest_entry(self) -> dict[str, str]:
        return {
            "mutant_id": self.mutant_id,
            "loop": self.loop,
            "operator": self.mutation.operator,
            "family": self.mutation.family,
            "path": self.mutation.path,
            "label": self.mutation.label,
            "rationale": self.mutation.rationale,
            "digest": self.mutation.digest,
        }


def _forbidden_paths(manifest: dict) -> set[str]:
    """The loop's own `forbid` list, as relative paths. Read as paths, never interpreted."""
    return {str(entry) for entry in (manifest.get("forbid") or [])}


def mutable_artifacts(loop_dir: Path) -> list[Path]:
    """Files under `seed/` that a worker is allowed to change.

    Returns paths sorted, so generation order is deterministic and a regenerated corpus is
    byte-identical.
    """
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return []
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    forbidden = _forbidden_paths(manifest)

    seed = loop_dir / "seed"
    if not seed.is_dir():
        return []

    found: list[Path] = []
    for path in sorted(seed.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(loop_dir).as_posix()
        if relative in forbidden or not operators.is_mutable_artifact(relative):
            continue
        found.append(path)
    return found


def generate_for_loop(loop_dir: Path, *, content_root: Path | None = None) -> list[Mutant]:
    """Every mutant for one loop, in a deterministic order.

    `content_root` is where the artifact BYTES are read from, defaulting to the loop directory. The
    harness passes the converged run workspace instead: the loop directory holds the pristine seed,
    which fails its own gate by design, so mutating it produces mutants whose labels are wrong.
    Which files are mutable still comes from the catalog manifest, because the workspace has no
    `loop.yaml`.

    Splitting the two roots does not widen what the generator can see: it reads `forbid` (a path
    list) and file bytes, exactly as before.
    """
    loop = loop_dir.name
    root = content_root if content_root is not None else loop_dir
    mutants: list[Mutant] = []
    for artifact in mutable_artifacts(loop_dir):
        relative = artifact.relative_to(loop_dir).as_posix()
        source = root / relative
        if not source.is_file():
            # Present in the catalog, absent from the converged workspace — nothing to mutate.
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # A binary or unreadable artifact is skipped rather than mutated as bytes: this corpus
            # measures judgement about work products, and a gate's opinion of corrupted binary is
            # not a result anyone can interpret.
            continue
        for mutation in operators.mutate(relative, text):
            mutants.append(Mutant(loop=loop, mutation=mutation))
    return mutants


def generate(catalog_root: Path) -> list[Mutant]:
    """The whole corpus, across every loop in `catalog_root`."""
    mutants: list[Mutant] = []
    for manifest_path in sorted(catalog_root.glob("*/loop.yaml")):
        mutants.extend(generate_for_loop(manifest_path.parent))
    return mutants


def manifest_document(mutants: list[Mutant], *, catalog_root: Path) -> dict:
    """The publishable corpus manifest.

    Counts are included per label so a reader can see the balance without recomputing it, and so a
    corpus that has silently lost one whole family is obvious at a glance rather than after the
    numbers look strange.
    """
    by_label: dict[str, int] = {}
    by_operator: dict[str, int] = {}
    for mutant in mutants:
        by_label[mutant.mutation.label] = by_label.get(mutant.mutation.label, 0) + 1
        by_operator[mutant.mutation.operator] = by_operator.get(mutant.mutation.operator, 0) + 1

    return {
        "corpus_version": CORPUS_VERSION,
        "catalog": catalog_root.name,
        "total": len(mutants),
        "loops": len({m.loop for m in mutants}),
        "by_label": dict(sorted(by_label.items())),
        "by_operator": dict(sorted(by_operator.items())),
        "ground_truth": (
            "Labels come from the OPERATION, decided before the edit was made — never from "
            "observing a gate. No equivalent-mutant exclusion is performed, and none is needed: "
            "nothing here is labelled by behaviour."
        ),
        "mutants": [mutant.as_manifest_entry() for mutant in mutants],
    }


def iter_loop_dirs(catalog_root: Path) -> Iterator[Path]:
    """Every loop directory in the catalog, sorted."""
    for manifest_path in sorted(catalog_root.glob("*/loop.yaml")):
        yield manifest_path.parent
