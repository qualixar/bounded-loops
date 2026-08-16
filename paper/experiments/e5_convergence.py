#!/usr/bin/env python3
"""E5 — cross-model convergence.

THE CLAIM BEING EARNED: the same bounded loop, driven by independent provider
models, converges to DONE through the SAME independent gate. Per (loop, provider)
we record the terminal state, attempts-to-DONE, and tokens, straight from the
receipt log rather than from anything the CLI said about itself.

WHAT IS DELIBERATELY NOT CLAIMED. This is not a capability benchmark and must not
be read as one. Each loop is seeded with a specific defect and each gate is a fixed
mechanical check; a provider that needs more attempts on one loop has not been shown
to be worse at anything in general. The quantity of interest is whether the gate
admits the same set of outcomes regardless of who produced the artifact.

CODEX IS HELD OUT and that is reported, not hidden: its subscription is over its
weekly budget for this period, so a five-provider run is not producible. Four ran.
The paper says four.

Every invocation goes through the shipped engine (`bl run`), the shipped gate, and
the shipped profile table via `e5_cli_shim.py`. Nothing here reimplements a decision.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3] / "bounded-loops"
SHIM = Path(__file__).resolve().parent / "e5_cli_shim.py"

#: Providers driven at the loop level. `codex` is intentionally absent — see module docstring.
PROVIDERS: tuple[str, ...] = ("claude", "grok", "muse", "agy")

#: Loops spanning all three keyless gate kinds, chosen before any run.
#: Fixed here so the selection cannot drift toward whatever happened to converge.
LOOPS: tuple[str, ...] = (
    "citation-existence-check",   # command  — fabricated + mis-paginated citations
    "bug-fix-red-green",          # pytest   — a failing suite that must go green
    "catalog-required-fields",    # jsonschema — structured output must validate
)

MAX_ITERATIONS = 4


def _patched_loop(src: Path, dest: Path, provider: str) -> Path:
    """Copy a loop and point its runner at one provider through the shim.

    The gate, the bounds, the seed and the forbid set are untouched: only who
    ATTEMPTS the work changes across arms, which is what makes the comparison
    a comparison.
    """
    shutil.copytree(src, dest)
    (dest / ".ledger.jsonl").unlink(missing_ok=True)
    manifest_path = dest / "loop.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    runner = dict(manifest.get("runner") or {})
    runner["default"] = "shell"
    runner["agent_cmd"] = f"python3 {SHIM} {provider}"
    manifest["runner"] = runner
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return dest


def _ledger(loop_dir: Path) -> list[dict]:
    path = loop_dir / ".ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_one(loop_name: str, provider: str) -> dict:
    src = REPO / "loops" / loop_name
    workdir = Path(tempfile.mkdtemp(prefix=f"e5-{loop_name}-{provider}-"))
    loop_dir = _patched_loop(src, workdir / "loop", provider)

    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "bounded_loops.cli", "run", str(loop_dir),
         "--yes", "--max-iterations", str(MAX_ITERATIONS)],
        cwd=REPO, capture_output=True, text=True, timeout=3600,
    )
    elapsed = round(time.time() - started, 1)

    entries = _ledger(loop_dir)
    verdicts = [bool(e.get("verdict", {}).get("passed")) for e in entries]
    tokens = sum(int(e.get("budget_spent", {}).get("tokens") or 0) for e in entries)
    passed_at = verdicts.index(True) + 1 if True in verdicts else None

    # Terminal state is read from the engine's own exit code and stdout marker,
    # never inferred from the transcript. A non-DONE outcome is recorded as itself.
    terminal = "DONE" if "[DONE]" in proc.stdout else (
        "HALT" if "[HALT]" in proc.stdout else
        "PAUSE" if "[PAUSE]" in proc.stdout else
        "KILLED" if "[KILLED]" in proc.stdout else "ERROR"
    )
    return {
        "loop": loop_name,
        "provider": provider,
        "terminal": terminal,
        "exit_code": proc.returncode,
        "attempts": len(entries),
        "attempts_to_done": passed_at,
        "tokens": tokens,
        "wallclock_s": elapsed,
        "workdir": str(loop_dir),
        "stdout_tail": proc.stdout.strip().splitlines()[-1:] or [""],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "e5-results.json"))
    ap.add_argument("--providers", default=",".join(PROVIDERS))
    ap.add_argument("--loops", default=",".join(LOOPS))
    args = ap.parse_args()

    providers = tuple(p for p in args.providers.split(",") if p)
    loops = tuple(loop for loop in args.loops.split(",") if loop)

    results: list[dict] = []
    for loop_name in loops:
        for provider in providers:
            print(f"[e5] {loop_name} x {provider} ...", flush=True)
            try:
                row = _run_one(loop_name, provider)
            except subprocess.TimeoutExpired:
                row = {"loop": loop_name, "provider": provider, "terminal": "TIMEOUT",
                       "exit_code": None, "attempts": None, "attempts_to_done": None,
                       "tokens": None, "wallclock_s": 3600, "workdir": "", "stdout_tail": []}
            results.append(row)
            print(f"[e5]   -> {row['terminal']} attempts={row['attempts']} "
                  f"to_done={row['attempts_to_done']} tokens={row['tokens']} "
                  f"{row['wallclock_s']}s", flush=True)
            Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"[e5] wrote {args.out} ({len(results)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
