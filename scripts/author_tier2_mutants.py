#!/usr/bin/env python3
"""Author Tier-2 semantic mutants by asking an agent CLI, with the checker withheld.

    python scripts/author_tier2_mutants.py --cli claude --loops dependency-pinning,a11y

Each CLI sees `bounded_loops.evaluation.tier2.authoring_prompt(loop)` — the loop's stated purpose
and nothing else — plus the CONVERGED text of every artifact its gate judges. It never sees
`seed/check_*.py`, and the digest of what it DID see is recorded on every mutant it produces.

**The loop is converged first, and that is not an optimisation.** Mutants are materialised onto the
converged baseline, so an author shown the pristine `seed/` would be editing a document that no
longer exists — one that fails its own gate by design. Every "meaning-preserving" edit would then
carry the seed's original defect into a file the gate rejects, and the corpus would record a false
reject that is really this script having handed over the wrong text.

**Why the output contract is strict JSON.** A mutant that cannot be parsed cannot be reviewed, and
a lenient parser is how a half-understood reply becomes a corpus entry nobody checked. Anything
that does not parse is dropped and counted, never guessed at.

**Nothing is trusted on arrival.** Every returned mutant is validated before it is written:
it must target a real mutable artifact, it must actually differ from the baseline, and it must name
the requirement it violates. Tier 1's mislabelled mutants all shared one property — nobody had to
justify the label — so here the justification is mandatory and a reviewer reads it.

Results are printed as a corpus document on stdout. Review before committing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bounded_loops.evaluation import corpus, harness, tier2  # noqa: E402
from bounded_loops.graph.adapters.connectors.local_cli_worker import (  # noqa: E402
    CLI_PROFILES,
    build_cli_argv,
)

_OUTPUT_CONTRACT = """

--- THE ARTIFACTS YOU MAY EDIT ---
These are the CURRENT, CORRECT contents. They already satisfy the stated purpose.

{artifacts}

--- WHAT TO RETURN ---
Return ONLY a JSON array. No prose, no code fences. Each element:

  {{
    "label": "incorrect",
    "requirement": "one sentence naming the requirement of the stated purpose this breaks",
    "path": "one of the paths listed above, exactly as written",
    "mutated_text": "the COMPLETE new contents of THAT file"
  }}

Write 2 to 4 edits. Prefer violations that a careless automated check would miss: a clause that
says the opposite of what is required, a value that looks right and is not, a required element
that is present in form but empty of substance.

When several files are shown, the most valuable edits break a requirement that holds BETWEEN them —
a reference to something that no longer exists, a total that no longer agrees with its parts.

Use "label": "correct" only for an edit you are certain preserves every requirement.
"""


def _ask(cli: str, prompt: str, timeout_s: int) -> str:
    profile = CLI_PROFILES[cli]
    import shutil

    binary = shutil.which(profile.binary)
    if binary is None:
        raise RuntimeError(f"{profile.binary} is not on PATH")

    # Deliberately WITHOUT the profile's usage_args. Those ask the CLI for a JSON envelope carrying
    # usage and cost, which is what the engine wants at run time and exactly wrong here: the reply
    # would arrive wrapped, and the array-scanner below would find an unrelated empty array
    # (`permission_denials: []`) before reaching the authored mutants. Metering is not the job on
    # this path; a readable reply is.
    from dataclasses import replace

    plain = replace(profile, usage_args=(), envelope=None)
    argv, stdin_text = build_cli_argv(plain, prompt, binary=binary)

    import os

    env = {k: v for k, v in os.environ.items()}
    for name in profile.unset_env:
        env.pop(name, None)

    result = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, timeout=timeout_s, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{cli} exited {result.returncode}: {result.stderr[:200]}")
    return result.stdout


def _extract_json_array(raw: str) -> list[dict]:
    """The first top-level JSON array in a reply. Returns [] rather than guessing.

    Models wrap JSON in fences or prose despite instructions. Scanning for a balanced array is
    tolerant of that without being tolerant of malformed content: anything that does not parse is
    dropped, because a corpus entry nobody could read is worse than one that is missing.
    """
    start = raw.find("[")
    while start != -1:
        depth = 0
        for index in range(start, len(raw)):
            if raw[index] == "[":
                depth += 1
            elif raw[index] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:index + 1])
                    except json.JSONDecodeError:
                        break
                    return parsed if isinstance(parsed, list) else []
        start = raw.find("[", start + 1)
    return []


def judged_artifacts(loop_dir: Path, baseline: Path) -> dict[str, str]:
    """`{relative path: CONVERGED contents}` for every artifact this loop's gate actually reads.

    **Converged, not seeded, and that distinction is the whole validity of the tier.** A mutant is
    materialised onto the converged baseline, so an author shown the pristine `seed/` would be
    editing text that no longer exists — and every edit they called meaning-preserving would carry
    the seed's original defect into a file the gate then rejects, recording a false reject that is
    really the harness handing over the wrong document. The seed fails its own gate by design; that
    is what makes a loop demonstrate something, and it is what makes it useless as authoring input.

    **Every judged artifact, not the largest one.** Tier 1's stated limit is that it cannot express
    a violation of the relation BETWEEN artifacts, which is exactly why the multi-artifact loops
    fall to Tier 2. Showing one file reproduces the limitation the tier exists to lift.

    Restricting to what the gate reads is harness-side scoping, not a leak: it decides which file
    the author is handed, never what any checker looks for inside it, and the digest still records
    the prompt they saw.
    """
    found: dict[str, str] = {}
    for relative in corpus.mutable_artifacts(loop_dir, content_root=baseline):
        if not harness.judges_artifact(loop_dir, relative):
            continue
        try:
            found[relative] = (baseline / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return found


def author_for_loop(loop_dir: Path, cli: str, *, timeout_s: int) -> list[tier2.Tier2Mutant]:
    """Converge the loop, then ask one CLI for mutants against what the gate actually judges."""
    with tempfile.TemporaryDirectory(prefix="bl-t2-author-") as scratch:
        baseline = harness.establish_baseline(loop_dir, into=Path(scratch) / loop_dir.name)
        if baseline is None:
            raise RuntimeError(
                "did not converge, or its gate rejects its own converged artifact — there is no "
                "correct document to author a violation against"
            )
        artifacts = judged_artifacts(loop_dir, baseline)

    if not artifacts:
        raise RuntimeError("gate judges no mutable artifact")

    rendered = "\n\n".join(
        f"--- path: {path} ---\n{content}" for path, content in sorted(artifacts.items())
    )
    prompt = tier2.authoring_prompt(loop_dir) + _OUTPUT_CONTRACT.format(artifacts=rendered)
    digest = tier2.authoring_prompt_digest(loop_dir)

    reply = _ask(cli, prompt, timeout_s)
    mutants: list[tier2.Tier2Mutant] = []
    for record in _extract_json_array(reply):
        if not isinstance(record, dict):
            continue
        mutated = record.get("mutated_text")
        requirement = record.get("requirement", "")
        label = record.get("label", "")
        path = record.get("path") or (next(iter(artifacts)) if len(artifacts) == 1 else None)

        if not isinstance(mutated, str) or not isinstance(requirement, str):
            continue
        if path not in artifacts:
            # A path nobody was shown cannot be checked against what the author saw, and would be
            # written into the workspace on trust.
            continue
        if mutated == artifacts[path]:
            # A no-op carrying an INCORRECT label is a guaranteed false accept and a fabricated
            # result. Dropped rather than recorded.
            continue
        try:
            mutants.append(tier2.Tier2Mutant(
                loop=loop_dir.name, path=path, mutated_text=mutated,
                label=label, requirement=requirement,
                authored_by=cli, prompt_digest=digest,
            ))
        except ValueError:
            continue  # failed its own validation; not written
    return mutants


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, choices=sorted(CLI_PROFILES))
    parser.add_argument("--loops", required=True, help="comma-separated loop names")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    catalog = REPO / "loops"
    authored: list[tier2.Tier2Mutant] = []
    for name in [n.strip() for n in args.loops.split(",") if n.strip()]:
        loop_dir = catalog / name
        if not (loop_dir / "loop.yaml").is_file():
            print(f"skip {name}: no such loop", file=sys.stderr)
            continue
        try:
            found = author_for_loop(loop_dir, args.cli, timeout_s=args.timeout)
        except Exception as exc:  # noqa: BLE001 - one loop failing must not lose the rest
            print(f"skip {name}: {exc}", file=sys.stderr)
            continue
        print(f"{name}: {len(found)} mutant(s) from {args.cli}", file=sys.stderr)
        authored.extend(found)

    print(json.dumps(tier2.dump(authored), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
