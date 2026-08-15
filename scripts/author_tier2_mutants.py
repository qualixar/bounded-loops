#!/usr/bin/env python3
"""Author Tier-2 semantic mutants by asking an agent CLI, with the checker withheld.

    python scripts/author_tier2_mutants.py --cli claude --loops dependency-pinning,a11y

Each CLI sees `bounded_loops.evaluation.tier2.authoring_prompt(loop)` — the loop's stated purpose
and nothing else — plus the current text of the artifact it may edit. It never sees
`seed/check_*.py`, and the digest of what it DID see is recorded on every mutant it produces.

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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bounded_loops.evaluation import corpus, tier2  # noqa: E402
from bounded_loops.graph.adapters.connectors.local_cli_worker import (  # noqa: E402
    CLI_PROFILES,
    build_cli_argv,
)

_OUTPUT_CONTRACT = """

--- THE ARTIFACT YOU MAY EDIT ---
path: {path}

{content}

--- WHAT TO RETURN ---
Return ONLY a JSON array. No prose, no code fences. Each element:

  {{
    "label": "incorrect",
    "requirement": "one sentence naming the requirement of the stated purpose this breaks",
    "mutated_text": "the COMPLETE new contents of the file"
  }}

Write 2 to 4 edits. Prefer violations that a careless automated check would miss: a clause that
says the opposite of what is required, a value that looks right and is not, a required element
that is present in form but empty of substance.

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


def author_for_loop(loop_dir: Path, cli: str, *, timeout_s: int) -> list[tier2.Tier2Mutant]:
    """Ask one CLI for mutants against one loop's largest mutable artifact."""
    artifacts = corpus.mutable_artifacts(loop_dir)
    if not artifacts:
        return []
    artifact = max(artifacts, key=lambda p: p.stat().st_size)
    relative = artifact.relative_to(loop_dir).as_posix()
    try:
        content = artifact.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    prompt = tier2.authoring_prompt(loop_dir) + _OUTPUT_CONTRACT.format(
        path=relative, content=content,
    )
    digest = tier2.authoring_prompt_digest(loop_dir)

    reply = _ask(cli, prompt, timeout_s)
    mutants: list[tier2.Tier2Mutant] = []
    for record in _extract_json_array(reply):
        if not isinstance(record, dict):
            continue
        mutated = record.get("mutated_text")
        requirement = record.get("requirement", "")
        label = record.get("label", "")
        if not isinstance(mutated, str) or not isinstance(requirement, str):
            continue
        if mutated == content:
            # A no-op carrying an INCORRECT label is a guaranteed false accept and a fabricated
            # result. Dropped rather than recorded.
            continue
        try:
            mutants.append(tier2.Tier2Mutant(
                loop=loop_dir.name, path=relative, mutated_text=mutated,
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
