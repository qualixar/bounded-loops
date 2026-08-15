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


def _forbidden_patterns(manifest: dict) -> tuple[str, ...]:
    """The loop's own `forbid` list, as path globs. Read as paths, never interpreted."""
    return tuple(str(entry) for entry in (manifest.get("forbid") or []))


def _is_forbidden(relative_path: str, patterns: tuple[str, ...]) -> bool:
    """Whether the loop forbids editing this path, decided by the SHIPPED matcher.

    `anchor_guard.matches_forbid` is what the runtime enforces at run time — case-insensitive
    `fnmatch` against the relative path or its basename. This module used exact string equality,
    which silently disagreed for the five loops that declare a glob (`seed/test_*.py`): the corpus
    considered a protected gate anchor mutable, because `"seed/test_ledger.py" != "seed/test_*.py"`.

    Importing the matcher is not a peek at any gate. It resolves PATHS against PATTERNS and would
    behave identically with every gate in the catalog deleted — the same test applied to the engine
    bookkeeping exclusions. What it buys is that "mutable" means here exactly what "editable" means
    to the runtime, rather than two definitions that agree until one is changed.
    """
    from bounded_loops.adapters.runners.anchor_guard import matches_forbid

    return matches_forbid(relative_path, patterns)


def mutable_artifacts(loop_dir: Path, *, content_root: Path | None = None) -> list[str]:
    """Relative paths of the work products a worker is allowed to change.

    **Which tree is enumerated matters, and getting it wrong silently loses whole gate kinds.**

    Without `content_root` this reads the catalog's `seed/`, which is what a loop SHIPS. With one,
    it reads the converged workspace, which is what a worker PRODUCED — and those are not the same
    set. A `jsonschema` loop seeds `seed/output.json` and converges by writing `output.json` at the
    workspace root; that root file is the artifact its gate reads, and a corpus enumerating only
    `seed/` never generates a single mutant for it. Ten loops measured nothing for exactly that
    reason, and nothing failed, because "no mutants" and "no false accepts" look identical in a
    count.

    Engine bookkeeping — the scratch marker, the runner transcript, `.git/`, `__pycache__/` — is
    excluded by `operators.is_mutable_artifact`. That exclusion is blind: it names files the RUNTIME
    writes, and would be unchanged if every gate in the catalog were deleted.

    `forbid` is still read from the catalog manifest as a path list, and still the only thing taken
    from `loop.yaml`. Returns relative POSIX paths, sorted, so a regenerated corpus is byte-identical.
    """
    manifest_path = loop_dir / "loop.yaml"
    if not manifest_path.is_file():
        return []
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    forbidden = _forbidden_patterns(manifest)

    root = content_root if content_root is not None else loop_dir / "seed"
    base = loop_dir if content_root is None else content_root
    if not root.is_dir():
        return []

    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        if _is_forbidden(relative, forbidden) or not operators.is_mutable_artifact(relative):
            continue
        found.append(relative)
    return found


def generate_for_loop(loop_dir: Path, *, content_root: Path | None = None) -> list[Mutant]:
    """Every mutant for one loop, in a deterministic order.

    `content_root` is the converged run workspace, and it decides BOTH which files are mutable and
    what bytes they hold. The loop directory holds the pristine seed, which fails its own gate by
    design, so mutating it produces mutants whose labels are wrong — and it is also missing every
    artifact the worker created rather than edited.

    Splitting the two roots does not widen what the generator can see: it reads `forbid` (a path
    list) and file bytes, exactly as before. It reads MORE FILES, not more about any gate.
    """
    loop = loop_dir.name
    root = content_root if content_root is not None else loop_dir
    mutants: list[Mutant] = []
    for relative in mutable_artifacts(loop_dir, content_root=content_root):
        source = root / relative
        if not source.is_file():
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
