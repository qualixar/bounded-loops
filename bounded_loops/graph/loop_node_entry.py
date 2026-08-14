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
* **Whether any OS sandbox applies AT ALL depends on the node's declared isolation, and this module
  cannot assume one.** ``workspace_only`` maps to ``SandboxMechanism.NONE``, which returns UNWRAPPED
  argv — no Seatbelt, no bubblewrap, network and host filesystem writes unrestricted. Only
  ``process_restricted`` and above select a real mechanism. An earlier version of this docstring
  described the Seatbelt profile as though it were always the environment; the shipped reference
  graphs were pinned at ``workspace_only`` at the time, so every loop node in them ran with
  ``fs_write`` and ``net`` reported as ``not_enforced`` in its own receipt. Read the receipt, not
  this comment, for what was enforced on a given run.
* WHEN Seatbelt is selected, its profile is ``(allow default)`` plus
  ``(deny file-write* (subpath "/"))`` with an allowlist, so the package is readable and unwritable.
  Note reads are NOT confined even then — ``sandbox.py`` says so — so an unwrapped or read-open gate
  command can read anything the user can.
* Independently of any sandbox, ``wire_loop_for_graph`` refuses a controller root inside the package,
  so that one protection does not depend on the isolation tier at all.
* Exit status is NOT the verdict. This process exits 0 whenever it managed to run the loop and
  write an outcome, including when the loop's own gate REJECTED the work. The graph's independent
  gate reads the outcome artifact and decides. A non-zero exit here means this process could not
  produce a trustworthy outcome at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from bounded_loops.application.loop_bridge import LoopExecutionRequest, wire_loop_for_graph
from bounded_loops.application.manifest import LoopManifest, load as load_manifest
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
    manifest = load_manifest(package)
    wired = wire_loop_for_graph(manifest, request)
    _overlay_inputs(manifest, wired.workspace)
    outcome = wired.run()
    _copy_loop_outputs(manifest, wired.workspace, Path.cwd())

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


def _overlay_inputs(manifest: LoopManifest, workspace: Path) -> None:
    """Copy declared input artifacts from BL_GRAPH_INPUTS into the loop workspace.

    Each input port declares a POSIX-relative target path inside the workspace.
    The artifact was materialized at BL_GRAPH_INPUTS/<port.name> by the sandboxed worker.
    We copy it to workspace/<port.path> so the loop's runner and gate find it there.

    Security: the destination is re-validated with a resolve + is_relative_to check so a
    traversal in port.path (already caught at manifest load) cannot escape even if the
    manifest validator were somehow bypassed at runtime.
    """
    if not manifest.inputs:
        return
    inputs_dir_str = os.environ.get("BL_GRAPH_INPUTS", "")
    if not inputs_dir_str:
        for port in manifest.inputs.values():
            if port.required:
                raise SystemExit(
                    f"input port {port.name!r} is required but BL_GRAPH_INPUTS is not set"
                )
        return
    inputs_dir = Path(inputs_dir_str)
    workspace_abs = workspace.resolve()
    for port in manifest.inputs.values():
        source = inputs_dir / port.name
        if source.is_symlink():
            # Symlinks from an upstream artifact are UNTRUSTED: a hostile loop could point a
            # symlink at an arbitrary host path. Reject unconditionally.
            raise SystemExit(
                f"input port {port.name!r}: source {source} is a symlink; "
                "upstream artifacts must be regular files (quarantine_inputs: true enforced)"
            )
        if not source.exists():
            if port.required:
                raise SystemExit(
                    f"input port {port.name!r} is required but {source} was not materialized"
                )
            continue
        dest = (workspace_abs / port.path).resolve()
        # Re-check escape even though _validate_port_path already blocked traversals — defence
        # in depth, consistent with the "fail CLOSED" contract in the task spec.
        if not str(dest).startswith(str(workspace_abs) + os.sep) and dest != workspace_abs:
            raise SystemExit(
                f"input port {port.name!r}: resolved path {dest} escapes workspace "
                f"{workspace_abs} — rejected"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(dest))


def _copy_loop_outputs(manifest: LoopManifest, workspace: Path, cwd: Path) -> None:
    """Collect declared output files from the workspace into cwd/outputs/<port_name>.

    The sandboxed worker's promote step requires EXACTLY the declared outputs to appear in cwd
    (BL_GRAPH_OUTPUTS).  Named port files live under cwd/outputs/ so they sort AFTER the primary
    loop-outcome.json, preserving the invariant that LoopReceiptGate reads digests[0].
    """
    if not manifest.outputs:
        return
    outputs_dir = cwd / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    for port in manifest.outputs.values():
        source = workspace / port.path
        if source.is_symlink():
            raise SystemExit(
                f"output port {port.name!r}: {source} is a symlink; "
                "workspace outputs must be regular files"
            )
        if not source.exists():
            raise SystemExit(
                f"output port {port.name!r}: expected file {source} does not exist after loop run"
            )
        shutil.copy2(str(source), str(outputs_dir / port.name))


def _digest_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
