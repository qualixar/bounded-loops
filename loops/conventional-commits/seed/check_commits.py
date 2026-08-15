#!/usr/bin/env python3
"""
check_commits.py — a keyless "are these commit subjects Conventional
Commits-compliant?" gate.

Verifies that every line of a commit-subject list matches the Conventional
Commits format: `type(optional-scope): description`. A malformed subject
breaks changelog generation and semantic-release tooling that parses commit
history to decide the next version bump — the failure this gate exists to
catch.

Pure Python standard library: no network, no API key, no external tool. It
runs anywhere Python does.

Allowed types: feat, fix, docs, style, refactor, perf, test, build, ci, chore,
revert — the type list of Conventional Commits v1.0.0.
Scope, if present, is lowercase alphanumeric plus hyphens, in parentheses.
An optional `!` before the colon marks a breaking change (`feat!:`,
`feat(api)!:`), which the spec defines and which this gate must accept: it is
the single most important subject a release tool reads, and rejecting it made
this gate refuse valid input it advertised support for.
A description must follow the colon-space.

Exit code: 0 = every line conforms (gate passes), 1 = one or more lines
violate the format (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SUBJECT_RE = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9-]+\))?"
    r"!?"  # breaking-change marker, per Conventional Commits v1.0.0
    r": .+$"
)


#: How many commit subjects the file shipped with. PROMPT.md: "Do not delete a malformed line to
#: dodge the check; rewrite it to conform while keeping its original meaning." Nothing checked that,
#: so the cheapest way to make every subject conform was to delete the ones that did not — found by
#: a held-out mutant that removed the "WIP fix login bug" line and passed.
#:
#: A count, not the subjects themselves: the subjects are SUPPOSED to change, that is the task.
SEEDED_SUBJECT_COUNT = 7


#: The subjects that ALREADY CONFORMED when the file shipped. PROMPT.md scopes the task to the
#: malformed ones: "For each line it flags as malformed, rewrite that line". A conforming subject
#: is not the worker's to touch, and rewriting one is how `chore(deps): bump requests to 2.32.0`
#: became `... to 3.0.0` — still a valid conventional commit, now describing a bump that did not
#: happen. Verbatim, because these lines have no reason to change at all.
SEEDED_CONFORMING = (
    "feat(auth): add refresh-token rotation",
    "fix(api): correct off-by-one in pagination cursor",
    "docs(readme): document the new deploy pipeline",
    "refactor(core): extract retry policy into its own module",
    "chore(deps): bump requests to 2.32.0",
)

#: The malformed subjects, and the content words a faithful rewrite has to keep. PROMPT.md:
#: "rewrite it to conform while keeping its original meaning ... don't just relabel it arbitrarily".
#:
#: Meaning is not mechanically decidable, and that was the reason this was first filed as
#: unverifiable. It was the wrong conclusion, reached from the SHAPE of the sentence rather than by
#: trying: a rewrite that preserves meaning keeps the subject's content words, and one that
#: replaces the line with an unrelated change keeps none. `fix(api): correct pagination behavior`
#: standing in for "Updated the readme with new setup instructions" shares nothing with it.
#:
#: This does not verify meaning. It verifies that the rewrite is ABOUT THE SAME THING, which is the
#: part of the requirement a checker can reach, and it is enough to catch substitution.
SEEDED_MALFORMED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Updated the readme with new setup instructions", ("readme", "setup", "instructions")),
    ("WIP fix login bug", ("login", "bug")),
)

#: Documentation artifacts. Conventional Commits defines `docs` as "documentation only changes", so
#: a subject about a readme typed as anything else contradicts the spec it claims to follow —
#: `build(readme): update setup instructions` was accepted because nothing compared the type to
#: what the subject describes.
_DOC_ARTIFACTS = ("readme", "docs", "documentation", "changelog", "guide")


def _content_words(subject: str) -> set[str]:
    """Lowercased words of a subject, minus conventional-commit syntax and filler."""
    body = subject.split(":", 1)[-1]
    words = re.findall(r"[a-z0-9]+", body.lower())
    return {w for w in words if len(w) > 2 and w not in {"the", "and", "for", "with", "new", "its"}}


def _meaning_regressions(lines: list[str]) -> list[str]:
    """Subjects that were replaced rather than rewritten, or typed against their own content."""
    problems: list[str] = []

    for original in SEEDED_CONFORMING:
        if original not in lines:
            problems.append(
                f"{original!r} already conformed and is gone; only malformed subjects were to be "
                "rewritten"
            )

    # The rewrite has to be found among the lines that are NOT already-conforming originals.
    # Searching every line let `docs(readme): document the new deploy pipeline` — a DIFFERENT
    # seeded subject that also mentions a readme — stand in for the rewrite of "Updated the readme
    # with new setup instructions", so replacing that line with an unrelated API fix went unnoticed.
    candidates = [line for line in lines if line not in SEEDED_CONFORMING]

    for original, required in SEEDED_MALFORMED:
        if original in lines:
            continue  # still malformed; the format check below reports it
        kept = [
            line for line in candidates
            if len(_content_words(original) & _content_words(line)) >= 2
            or any(word in line.lower() for word in required)
        ]
        if not kept:
            problems.append(
                f"nothing left describes {original!r} — a rewrite must keep its meaning, not "
                "replace it with an unrelated subject"
            )
            continue
        for line in kept:
            type_name = line.split("(", 1)[0].split(":", 1)[0].strip().rstrip("!")
            if any(a in line.lower() for a in _DOC_ARTIFACTS) and type_name not in {"docs", "chore"}:
                problems.append(
                    f"{line!r} is typed {type_name!r} but describes a documentation change; "
                    "Conventional Commits reserves `docs` for documentation-only changes"
                )
    return problems


#: This check belongs to the LOOP, not to the general checker, and the two are the same file.
#: `check_commits.py` is a reusable validator that the 0.6.2 regression pins hand small synthetic
#: inputs; the "nothing was deleted" claim is about the one artifact this loop asks a worker to
#: repair. Applying it to every input made two of those pins fail as false rejects — a fix trading
#: a false accept for a false reject, which is the exact regression the pins exist to catch.
#:
#: Scoped by the artifact's own location, `seed/<name>`, which is where the loop keeps it and
#: nowhere a caller passing an arbitrary file would land.
def _is_the_loops_own_artifact(path: Path, name: str) -> bool:
    return path.name == name and path.parent.name == "seed"


def check(commits_path: str) -> int:
    try:
        text = Path(commits_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_commits: cannot run: {exc}", file=sys.stderr)
        return 1  # the worker owns this artifact: a REJECT, not an inability to run

    lines = [line for line in text.splitlines() if line.strip()]
    if _is_the_loops_own_artifact(Path(commits_path), "commits.txt") \
            and len(lines) < SEEDED_SUBJECT_COUNT:
        print(
            f"check_commits: {len(lines)} subject(s) remain of {SEEDED_SUBJECT_COUNT} — a "
            "malformed subject was deleted rather than rewritten",
            file=sys.stderr,
        )
        return 1

    if not lines:
        print("check_commits: no commit subjects found", file=sys.stderr)
        return 1  # the worker owns this artifact: a REJECT, not an inability to run

    violations: list[tuple[int, str]] = []
    for lineno, line in enumerate(lines, start=1):
        if not _SUBJECT_RE.match(line):
            violations.append((lineno, line))

    if _is_the_loops_own_artifact(Path(commits_path), "commits.txt"):
        regressions = _meaning_regressions([line.strip() for line in lines])
        if regressions:
            print(f"check_commits: {len(regressions)} subject(s) rewritten beyond the task:")
            for problem in regressions:
                print(f"  - {problem}")
            return 1

    if violations:
        print(f"check_commits: {len(violations)} malformed subject(s):")
        for lineno, line in violations:
            print(f"  - line {lineno}: {line!r}")
        return 1

    print(f"check_commits: all {len(lines)} commit subject(s) conform")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_commits.py <commits.txt>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
