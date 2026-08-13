"""Subprocess entry point that runs ONE bounded loop as ONE graph node attempt.

This module is what a ``kind: loop`` node's argv actually launches. It exists as a separate
process rather than an in-process call for one reason, and it is the reason ``LegacyLoopWorker``
refused to run at all: the loop engine spawns its own runner and gate subprocesses, and the graph's
isolation is applied by WRAPPING argv. A child of a wrapped process inherits the sandbox profile,
so running the loop engine here — inside the wrapper — is what puts the loop's own runner and gate
under the graph node's isolation, network mode and write confinement. Calling the engine in-process
from the controller would have left them outside it, which is exactly the policy hole the stub
refused to open.

Contract with the worker side:

* cwd is the node's promoted-output directory, so ``--outcome`` is written where
  ``declared_outputs`` will find it.
* The loop package is READ-ONLY here. Seatbelt's profile is ``(allow default)`` plus
  ``(deny file-write* (subpath "/"))`` with an allowlist, so the package is readable and
  unwritable — and ``wire_loop_for_graph`` independently refuses a controller root inside the
  package, so neither layer can be the only thing standing between a run and its own inputs.
* Exit status is NOT the verdict. This process exits 0 whenever it managed to run the loop and
  write an outcome, including when the loop's own gate REJECTED the work. The graph's independent
  gate reads the outcome artifact and decides. A non-zero exit here means this process could not
  produce a trustworthy outcome at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from bounded_loops.application.loop_bridge import LoopExecutionRequest, wire_loop_for_graph
from bounded_loops.application.manifest import load as load_manifest
from bounded_loops.graph.adapters.workers.loop_packages import (
    CONTROLLER_SUBDIR,
    DEFAULT_OUTCOME_FILENAME,
    loop_package_digest,
    normalise_package_digest,
)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bounded_loops.graph.loop_node_entry",
        description="Run one bounded loop as one graph node attempt.",
    )
    parser.add_argument("--package", required=True, help="Loop package directory (read-only).")
    parser.add_argument(
        "--package-digest", required=True,
        help="Expected content digest of the package. Re-verified here, in the process that "
             "actually runs the loop, not only where the node was resolved.",
    )
    parser.add_argument("--run-id", required=True, help="The GRAPH run id.")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--repair-round", default=0, type=int)
    parser.add_argument("--outcome", default=DEFAULT_OUTCOME_FILENAME)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    package = Path(args.package).resolve()

    # Re-hash before running anything. The resolver already checked this, but a check that only
    # happens where the spec was built cannot see a package swapped between resolution and launch,
    # and this process is the one that will execute the package's own gate command.
    observed = loop_package_digest(package)
    if observed != normalise_package_digest(args.package_digest):
        raise SystemExit(
            f"loop package digest mismatch for {package}: "
            f"declared {args.package_digest}, found {observed}"
        )

    # The loop engine's own run storage goes under TMPDIR, NOT under cwd.
    #
    # cwd is the node's promoted-output directory and ``promote_workspace_outputs`` requires it to
    # contain EXACTLY the declared outputs — it refuses an undeclared file rather than ignoring it.
    # Writing the loop's controller tree there therefore failed the whole attempt with
    # "workspace contains undeclared output: .controller/runs.sqlite", which is the promotion path
    # being correctly strict, not a bug in it.
    #
    # The sandboxed worker exports TMPDIR to a per-node writable directory beside the outputs, so
    # this stays inside the node's sandbox and is discarded with it. Run standalone, it lands in the
    # system temp directory — still outside the loop package, which is all ``wire_loop_for_graph``
    # requires. Reading TMPDIR rather than reaching for ``cwd.parent`` keeps this independent of the
    # worker's directory layout.
    controller_root = Path(tempfile.gettempdir()) / CONTROLLER_SUBDIR
    request = LoopExecutionRequest(
        run_id=args.run_id,
        node_id=args.node_id,
        attempt=args.attempt,
        controller_root=controller_root,
        repair_round=args.repair_round,
    )
    wired = wire_loop_for_graph(load_manifest(package), request)
    outcome = wired.run()

    # The inner ledger is nested BY REFERENCE: its digest travels in the outcome the graph
    # promotes, so the inner chain cannot be rewritten without breaking the node's receipt, and
    # the inner log never becomes a second scheduling authority.
    payload = {
        "status": outcome.status.value,
        "reason": outcome.reason,
        "package_digest": observed,
        "inner_run_id": wired.inner_run_id,
        "inner_ledger_digest": _digest_of(wired.event_path),
        "node_id": args.node_id,
        "attempt": args.attempt,
        "repair_round": args.repair_round,
    }
    Path(args.outcome).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
    )
    return 0


def _digest_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
