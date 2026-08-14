"""
bl CLI — run, inspect, diagnose, scaffold, and audit bounded loops.

Implementation: argparse (stdlib), zero extra deps.
Entry point: bounded_loops.cli:main (registered as [project.scripts] bl).

Exit codes:
    bl run:  0=DONE, 1=non-DONE, 2=ManifestError, 3=unexpected
    bl lint: 0=all pass, 1=any fail
    bl list: 0 always

This module imports `wire` directly from `bounded_loops.composition` (the
package __init__ also re-exports it; the module-level name
`bounded_loops.cli.wire` is what the test suite's
`patch("bounded_loops.cli.wire", ...)` targets, so both import styles work).
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
import sys
from pathlib import Path

from bounded_loops.application.manifest import LoopManifest, load as manifest_load
from bounded_loops.application.doctor import diagnose_environment
from bounded_loops.application.loop_audit import audit_contribution, audit_loops
from bounded_loops.application.introspection import list_gates, show_loop
from bounded_loops.application.run_store import (
    begin_run,
    list_runs,
    read_run_receipt,
    write_run_metadata,
)
from bounded_loops.graph.adapters.preflight.runner_preflight import (
    default_runner_profiles,
    preflight_runners,
)
from bounded_loops import __version__
from bounded_loops.composition import wire
from bounded_loops.domain.errors import BoundedLoopsError, ManifestError
from bounded_loops.domain.models import Status
from bounded_loops.trust_store import record_trust, revoke_trust

# fix: a single, non-traversing path-segment shape. Rejects
# "..", "/", absolute paths, and empty strings BEFORE any path is ever built
# from a user-supplied template name — closes a path-traversal defect found
# in audit (a "../../../etc"-style template name previously let a caller
# read arbitrary files relative to the templates root).
_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


# ── Public entry point ────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """
    Entry point called by the `bl` console-script wrapper.
    Returns an int exit code; the wrapper calls sys.exit(main()).

    Parameters
    ----------
    argv:
        If None, uses sys.argv[1:]. Passed explicitly only in tests.

    Returns
    -------
    int
        Exit code.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        # No subcommand given — print help and exit 1.
        parser.print_help(sys.stderr)
        return 1

    return args.func(args)


# ── Parser construction ───────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="bl",
        description=(
            "bounded-loops engine: run and inspect AI agent loops "
            "with nine safety bounds."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="cmd", metavar="SUBCOMMAND")

    # ── bl run ────────────────────────────────────────────────────────────────
    run_parser = subparsers.add_parser(
        "run",
        help="Execute a loop via the bounded-loops engine.",
        description=(
            "Runs RunLoopUseCase against the loop at <loop-dir>. "
            "Exit 0 iff Outcome.status == DONE."
        ),
    )
    run_parser.add_argument(
        "loop_dir",
        metavar="loop-dir",
        type=Path,
        help="Path to a loop folder containing loop.yaml.",
    )
    run_parser.add_argument(
        "--runner",
        choices=["stub", "shell", "claude-code", "codex", "python_callable", "antigravity", "docker", "worktree"],
        default=None,
        help=(
            "Override the runner specified in loop.yaml. "
            "Default: use loop.yaml's runner.default."
        ),
    )
    run_parser.add_argument(
        "--gate-override",
        metavar='"<cmd>"',
        default=None,
        dest="gate_override",
        help=(
            "Replace the loop's gate with a shell command. "
            "Exit 0 = pass. E.g. --gate-override 'npm test'."
        ),
    )
    run_parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        dest="max_iterations",
        help="Override bounds.max_iterations from loop.yaml.",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit Outcome as JSON on stdout instead of human-readable text.",
    )
    run_parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the trust-confirmation prompt and run immediately. "
            "Required in non-interactive contexts (CI) — see security note below."
        ),
    )
    run_parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep the scratch workspace after the run for debugging.",
    )
    run_parser.add_argument(
        "--run-id",
        default=None,
        help="Persist workspace, ledger, and metadata under .bounded-loops/runs/<run-id>.",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing persistent run workspace selected by --run-id.",
    )
    run_parser.set_defaults(func=_cmd_run)

    # ── bl lint ───────────────────────────────────────────────────────────────
    lint_parser = subparsers.add_parser(
        "lint",
        help="Validate loop manifests and bounds.",
        description=(
            "Validates loop.yaml + bounds.yaml for each <loop-dir>. "
            "Checks required keys, supported adapters, bounds, and cross-field "
            "constraints. Exit 0 iff all pass."
        ),
    )
    lint_parser.add_argument(
        "loop_dirs",
        metavar="loop-dir",
        type=Path,
        nargs="+",
        help="One or more loop folder paths to lint.",
    )
    lint_parser.add_argument(
        "--contrib",
        action="store_true",
        help=(
            "Apply the stricter catalog contribution bar: protected anchors, "
            "a real gate, and README evidence that failure becomes DONE."
        ),
    )
    lint_parser.set_defaults(func=_cmd_lint)

    # ── bl list ───────────────────────────────────────────────────────────────
    list_parser = subparsers.add_parser(
        "list",
        help="Discover and list loops in your current project or source checkout.",
        description=(
            "Lists name, role, rung, and gate.kind for each discovered loop. "
            "With no argument, searches ./loops/*/loop.yaml, ./*/loop.yaml, and "
            "the nearest bounded-loops source checkout. Pass a directory to list "
            "the loops under it. For a pip or npx install with no local loops yet, "
            "start with `bl new --list`."
        ),
    )
    list_parser.add_argument(
        "dir", nargs="?", type=Path, default=None,
        help="Directory to search for loops (default: cwd + nearest repo root).",
    )
    # NOTE: no --json here either — same  resolution as `bl lint` above.
    list_parser.set_defaults(func=_cmd_list)

    # ── bl show ──────────────────────────────────────────────────────────────
    show_parser = subparsers.add_parser(
        "show",
        help="Show loop runner, gate, bounds, risk, and dependencies.",
        description="Inspect a loop before running it.",
    )
    show_parser.add_argument("loop_dir", metavar="loop-dir", type=Path)
    show_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    show_parser.set_defaults(func=_cmd_show)

    # ── bl gates ─────────────────────────────────────────────────────────────
    gates_parser = subparsers.add_parser(
        "gates",
        help="List gate capabilities and local dependency availability.",
    )
    gates_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    gates_parser.set_defaults(func=_cmd_gates)

    # ── bl doctor ────────────────────────────────────────────
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check Python, pytest, runners, and optional gate tools.",
    )
    doctor_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    doctor_parser.set_defaults(func=_cmd_doctor)

    # ── bl preflight ─────────────────────────────────────────────────────────
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Observe fixed runner version probes without authentication or routing.",
    )
    preflight_parser.add_argument(
        "--profile",
        choices=[profile.id for profile in default_runner_profiles()],
        default=None,
        help="Observe one known runner profile (default: inventory all profiles).",
    )
    preflight_parser.add_argument("--json", action="store_true", help="Emit the schema-v1 report as JSON.")
    preflight_parser.set_defaults(func=_cmd_preflight)

    # ── bl runs ──────────────────────────────────────────────────────────────
    runs_parser = subparsers.add_parser(
        "runs",
        help="List persisted runs for a loop directory.",
    )
    runs_parser.add_argument("loop_dir", metavar="loop-dir", type=Path)
    runs_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    runs_parser.add_argument(
        "--show",
        metavar="RUN_ID",
        help="Pretty-print the per-lap receipt for one persisted run.",
    )
    runs_parser.set_defaults(func=_cmd_runs)

    # ── bl trust ──────────────────────────────────────────────────────────────
    trust_parser = subparsers.add_parser(
        "trust",
        help="Manage the verify-on-stop trust store.",
        description=(
            "Trust records let the verify-on-stop hook auto-re-run a loop's "
            "gate. `bl trust revoke <loop-dir>` removes a loop's record so the "
            "hook stops auto-executing it."
        ),
    )
    trust_sub = trust_parser.add_subparsers(dest="trust_cmd", metavar="ACTION")
    revoke_parser = trust_sub.add_parser("revoke", help="Revoke a loop's trust record.")
    revoke_parser.add_argument("loop_dir", metavar="loop-dir", type=Path,
                               help="Path to the loop folder to revoke.")
    trust_parser.set_defaults(func=_cmd_trust)

    # ── bl new ────────────────────────────────────────────────────────────────
    new_parser = subparsers.add_parser(
        "new",
        help="Scaffold a new loop from a template.",
        description=(
            "Copies a packaged template to <destination>, substituting "
            "{{LOOP_NAME}}. Templates ship INSIDE the installed package "
            " — this works identically for a pip-installed user "
            "and a source checkout."
        ),
    )
    new_parser.add_argument("template", nargs="?", help="Template name (see --list).")
    new_parser.add_argument(
        "destination", nargs="?", type=Path, help="Where to create the new loop."
    )
    new_parser.add_argument(
        "--name", default=None, help="Loop name (default: destination dir's basename)."
    )
    new_parser.add_argument(
        "--list", action="store_true", help="List available templates and exit."
    )
    new_parser.set_defaults(func=_cmd_new)

    # ── bl audit-loops ───────────────────────────────────────────────────────
    audit_parser = subparsers.add_parser(
        "audit-loops",
        help="Audit loop examples for copy-paste production readiness.",
        description=(
            "Checks loop manifests, required files, README portability, approval "
            "posture, cassettes, and common copy-paste-readiness issues."
        ),
    )
    audit_parser.add_argument(
        "dirs", nargs="*", type=Path, default=[Path(".")],
        help="Repo roots, loops parents, or loop directories (default: cwd).",
    )
    audit_parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    audit_parser.set_defaults(func=_cmd_audit_loops)

    from bounded_loops.graph.cli_graph import register as _register_graph
    from bounded_loops.cli_loop import register as _register_loop
    from bounded_loops.cli_loops import register as _register_loops
    from bounded_loops.cli_workspace import register as _register_workspace
    from bounded_loops.cli_capabilities import register as _register_capabilities
    from bounded_loops.graph.jarvis.cli_jarvis import register as _register_jarvis

    _register_graph(subparsers)
    _register_loop(subparsers)
    _register_loops(subparsers)
    _register_workspace(subparsers)
    _register_capabilities(subparsers)
    _register_jarvis(subparsers)

    return parser


# ── Subcommand implementations ────────────────────────────────────────────────

def _cmd_run(args: argparse.Namespace) -> int:
    """
    bl run <loop-dir> [--runner ...] [--gate-override ...] [--max-iterations N] [--json] [--yes]

    Algorithm:
    1. Resolve loop_dir to an absolute path. If not a directory → stderr + exit 2.
    2. manifest_load(loop_dir) → LoopManifest. ManifestError → stderr + exit 2.
    3. TRUST CONFIRMATION: print the exact gate/runner
       command this loop is about to execute; require an explicit 'y' unless
       --yes was passed or --gate-override was given (the user already typed
       the command themselves in that case). Refuses in a non-interactive
       context (no --yes, stdin not a tty) rather than silently proceeding.
    4. wire(manifest, runner_override, gate_cmd_override, max_iterations_override)
       → RunLoopUseCase. ManifestError (unknown kind) → stderr + exit 2.
    5. use_case.run() → Outcome. Unexpected exception → stderr + exit 3.
    6. Print Outcome (JSON or human). Return 0 if status==DONE, else 1.
    """
    loop_dir: Path = args.loop_dir.resolve()

    if not loop_dir.is_dir():
        _err(f"bl run: '{loop_dir}' is not a directory or does not exist.")
        return 2

    try:
        manifest = manifest_load(loop_dir)
    except ManifestError as e:
        _err(f"bl run: manifest error — {e}")
        return 2

    if args.gate_override is None and not _confirm_trust(
        manifest,
        args.yes,
        runner_override=args.runner,
    ):
        _err("bl run: not confirmed. Pass --yes to skip this prompt (e.g. in CI).")
        return 2

    try:
        use_case = wire(
            manifest,
            runner_override=args.runner,
            gate_cmd_override=args.gate_override,
            max_iterations_override=args.max_iterations,
            keep_workspace=args.keep_workspace,
            run_id=args.run_id,
            resume=args.resume,
        )
    except ManifestError as e:
        _err(f"bl run: wiring error — {e}")
        return 2

    try:
        if args.run_id is not None:
            begin_run(
                loop_dir=manifest.loop_dir,
                run_id=args.run_id,
                workspace=use_case._workspace,
                ledger_path=use_case._deps.ledger.path(),
            )
        outcome = use_case.run()
    except BoundedLoopsError as e:
        _err(f"bl run: engine error — {e}")
        return 3
    except Exception as e:  # noqa: BLE001
        _err(f"bl run: unexpected error — {type(e).__name__}: {e}")
        return 3

    _print_outcome(outcome, as_json=args.json)

    if args.run_id is not None:
        write_run_metadata(
            loop_dir=manifest.loop_dir,
            run_id=args.run_id,
            outcome=outcome,
            workspace=use_case._workspace,
        )

    if outcome.status == Status.DONE:
        return 0
    if outcome.status == Status.ERROR:
        return 3
    return 1


def _confirm_trust(
    manifest: LoopManifest,
    skip_prompt: bool,
    *,
    runner_override: str | None = None,
) -> bool:
    """
    Security fix: a
    loop.yaml's gate.run (or runner.agent_cmd for shell) is arbitrary shell
    code, sourced from a folder bounded-loops explicitly invites as a
    community PR. Print exactly what will run before running it — a
    direnv-style trust gate — rather than silently executing an unfamiliar
    loop's command.

    Fails CLOSED: if stdin is not a TTY and --yes was not passed (the CI
    case), this returns False rather than guessing "probably fine."

    Trust recording: a genuine interactive 'y' answer is a
    real human review event, so it records a trust entry that the
    verify-on-stop hook will later recognize for this exact loop_dir + gate
    command. --yes (skip_prompt) is a CI bypass, NOT a human review event —
    it must never record trust on its own.
    """
    gate_cmd = manifest.gate_config.get("run", f"<{manifest.gate_kind} gate>")
    effective_runner = runner_override or manifest.runner_kind
    print(f"[bounded-loops] About to run loop '{manifest.name}':")
    print(f"  runner : {effective_runner}")
    print(f"  gate   : {gate_cmd}")
    if skip_prompt:
        return True   # --yes: CI bypass, NOT a human review — no trust recorded
    if not sys.stdin.isatty():
        return False   # non-interactive + no --yes → fail closed, never fail open
    answer = input("Proceed? [y/N] ").strip().lower()
    confirmed = answer in ("y", "yes")
    if confirmed:
        record_trust(manifest.loop_dir, gate_cmd)   # NEW — the only line added
    return confirmed


def _cmd_lint(args: argparse.Namespace) -> int:
    """
    bl lint <loop-dir>...

    Algorithm:
    1. For each loop_dir in args.loop_dirs:
       a. Resolve to absolute path. If not a directory → record failure.
       b. manifest_load(loop_dir) → pass/fail. ManifestError → record failure + message.
    2. Print all results (JSON or human).
    3. Return 0 iff all passed, 1 otherwise.

    Lint enforces (via manifest_load):
      - runner.default must be stub|shell (ManifestError if not).
      - gate.kind must not be a Qualixar product kind (ManifestError if so).
      - All required keys present, all values type-correct.
    """
    results: list[dict] = []
    any_failure = False

    for loop_dir_arg in args.loop_dirs:
        loop_dir = loop_dir_arg.resolve()
        entry: dict = {"path": str(loop_dir), "passed": False, "errors": []}

        if not loop_dir.is_dir():
            entry["errors"].append(f"'{loop_dir}' is not a directory")
            any_failure = True
            results.append(entry)
            continue

        try:
            manifest_load(loop_dir)
            if args.contrib:
                entry["errors"].extend(audit_contribution(loop_dir))
            entry["passed"] = not entry["errors"]
            any_failure = any_failure or not entry["passed"]
        except ManifestError as e:
            entry["errors"].append(str(e))
            any_failure = True

        results.append(entry)

    _print_lint_results(results)

    return 1 if any_failure else 0


def _find_repo_root(start: Path) -> Path:
    """Walk UP from start looking for pyproject.toml (the repo root)."""
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise ManifestError(
        f"bl list: no pyproject.toml found walking up from {start} — "
        "run this from inside a bounded-loops repo."
    )


def _discover_loop_yamls(explicit_dir: Path | None) -> list[Path]:
    """
    Collect loop.yaml paths to list, deduplicated + sorted.

    Hardening: the old behavior walked UP for pyproject.toml and
    globbed ONLY `<that-root>/loops/*/loop.yaml`. For a pip-installed user
    running `bl list` from their own project, that resolves to THEIR
    pyproject.toml and THEIR (absent/unrelated) loops dir — it finds nothing.
    Now:
      - `bl list <dir>`  → search `<dir>/*/loop.yaml` and `<dir>/loops/*/loop.yaml`
        (a pip user points bl straight at their loops folder).
      - `bl list`        → search cwd's `./loops/*/loop.yaml` and `./*/loop.yaml`,
        AND the nearest bounded-loops repo root's `loops/` if one is found —
        union of all, so it works from a source checkout AND from a user's own
        project that keeps a `loops/` dir. Discovery stays bounded (one level
        under a loops-parent), never an unbounded rglob.
    """
    search_roots: list[Path] = []
    if explicit_dir is not None:
        d = explicit_dir.resolve()
        search_roots += [d, d / "loops"]
    else:
        cwd = Path.cwd()
        search_roots += [cwd, cwd / "loops"]
        try:
            root = _find_repo_root(cwd)
            search_roots.append(root / "loops")
        except ManifestError:
            pass   # no repo root — cwd search alone is fine for a pip user

    found: dict[str, Path] = {}
    for base in search_roots:
        if not base.is_dir():
            continue
        # hardening: a directory that IS the loop (has loop.yaml
        # at its own level) must be discovered too — the old code only globbed
        # `*/loop.yaml` (children), so `bl list loops/bug-fix-red-green` and
        # `bl list .` from inside a loop dir found nothing.
        own = base / "loop.yaml"
        if own.is_file():
            found.setdefault(str(base.resolve()), own)
        for yaml_file in base.glob("*/loop.yaml"):
            found.setdefault(str(yaml_file.parent.resolve()), yaml_file)
    return [found[k] for k in sorted(found)]


def _cmd_list(args: argparse.Namespace) -> int:
    """
    bl list [dir]

    Discovers loops (see _discover_loop_yamls) and prints a table of
    (name, role, rung, gate.kind). Returns 0 always — a lint failure surfaces
    via `bl lint`, not `bl list`.
    """
    loops: list[dict] = []
    for yaml_file in _discover_loop_yamls(getattr(args, "dir", None)):
        loop_dir = yaml_file.parent
        try:
            manifest = manifest_load(loop_dir)
            loops.append({
                "name":      manifest.name,
                "role":      manifest.raw.get("role", []),
                "rung":      manifest.rung.value,
                "gate_kind": manifest.gate_kind,
                "path":      str(loop_dir),
                "error":     None,
            })
        except ManifestError as e:
            loops.append({
                "name":      loop_dir.name,
                "role":      [],
                "rung":      "?",
                "gate_kind": "?",
                "path":      str(loop_dir),
                "error":     str(e),
            })

    _print_list(loops)
    return 0


def _cmd_trust(args: argparse.Namespace) -> int:
    """
    bl trust revoke <loop-dir>

    Removes the loop's verify-on-stop trust record so the hook stops
    auto-executing it. Reads the loop's own gate command
    from its manifest to compute the record key. Returns 0 on success (or if
    there was nothing to revoke), 2 on a bad path/manifest.
    """
    if getattr(args, "trust_cmd", None) != "revoke":
        _err("bl trust: the only action is `revoke <loop-dir>`.")
        return 2
    loop_dir: Path = args.loop_dir.resolve()
    if not loop_dir.is_dir():
        _err(f"bl trust revoke: '{loop_dir}' is not a directory.")
        return 2
    try:
        manifest = manifest_load(loop_dir)
    except ManifestError as e:
        _err(f"bl trust revoke: manifest error — {e}")
        return 2
    gate_cmd = manifest.gate_config.get("run", f"<{manifest.gate_kind} gate>")
    removed = revoke_trust(manifest.loop_dir, gate_cmd)
    print(f"[bounded-loops] {'revoked' if removed else 'no trust record for'} "
          f"loop '{manifest.name}'.")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        data = show_loop(args.loop_dir)
    except ManifestError as e:
        _err(f"bl show: manifest error — {e}")
        return 2
    if args.json:
        print(json.dumps(data))
    else:
        _print_show(data)
    return 0


def _cmd_gates(args: argparse.Namespace) -> int:
    gates = list_gates()
    if args.json:
        print(json.dumps({"gates": gates}))
    else:
        for gate in gates:
            status = "available" if gate["available"] else "missing"
            deps = ", ".join(gate["dependencies"]) if gate["dependencies"] else "none"
            print(f"{gate['kind']:<20} {status:<10} deps={deps}  {gate['description']}")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    """Observe known runner binaries without authenticating or routing them."""
    report = preflight_runners(default_runner_profiles(), profile_id=args.profile)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runners": [asdict(runner) for runner in report.runners],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        for runner in report.runners:
            status = "observed" if runner.available and runner.version else "unavailable"
            print(f"{runner.id:<20} {status:<12} admission={runner.admission} adapter={runner.adapter_status}")
            if runner.failure_reason:
                print(f"  {runner.failure_reason}")
    return 1 if args.profile and not report.runners[0].available else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = diagnose_environment()
    if args.json:
        print(json.dumps(report))
        return 0 if report["ok"] else 1

    print(f"{'CHECK':<28} {'STATUS':<9} DETAIL")
    print("-" * 78)
    checks = [report["python"], report["pytest"], *report["runners"]]
    for item in checks:
        status = "OK" if item["available"] else "MISSING"
        print(f"{item['name']:<28} {status:<9} {item['detail']}")
    for gate in report["gates"]:
        status = "OK" if gate["available"] else "MISSING"
        dependencies = ", ".join(gate["dependencies"]) or "built in"
        print(f"{'gate:' + gate['kind']:<28} {status:<9} {dependencies}")
    print("Core harness ready." if report["ok"] else "Core harness is not ready.")
    return 0 if report["ok"] else 1


def _cmd_runs(args: argparse.Namespace) -> int:
    loop_dir = args.loop_dir.resolve()
    if not loop_dir.is_dir():
        _err(f"bl runs: '{loop_dir}' is not a directory or does not exist.")
        return 2
    if args.show:
        try:
            receipt = read_run_receipt(loop_dir, args.show)
        except ManifestError as exc:
            _err(f"bl runs: {exc}")
            return 2
        if args.json:
            print(json.dumps({"run": receipt}))
        else:
            _print_run_receipt(receipt)
        return 0

    runs = list_runs(loop_dir)
    if args.json:
        print(json.dumps({"runs": runs}))
    else:
        if not runs:
            print("No persisted runs found.")
        for run in runs:
            if "error" in run:
                print(f"{run.get('run_id', '?')}: ERROR {run['error']}")
            else:
                print(
                    f"{run['run_id']}: {run['status']} laps={run['laps']} "
                    f"ledger={run['ledger_path']}"
                )
    return 0


def _print_run_receipt(receipt: dict) -> None:
    metadata = receipt["metadata"]
    run_id = metadata.get("run_id", "?")
    status = metadata.get("status", "UNKNOWN")
    reason = metadata.get("reason", "unknown")
    print(f"Run {run_id}: {status} ({reason})")
    print(f"Workspace: {metadata.get('workspace', 'unknown')}")
    print(f"Ledger: {metadata.get('ledger_path', 'unknown')}")
    print()
    for entry in receipt["entries"]:
        verdict = entry.get("verdict", {})
        passed = verdict.get("passed") is True
        state = "PASS" if passed else "FAIL"
        budget = entry.get("budget_spent", {})
        print(
            f"Lap {entry.get('lap', '?')}: {state}  "
            f"decision={entry.get('decision', '?')}  "
            f"tokens={budget.get('tokens', '?')}  "
            f"wallclock={budget.get('wallclock_s', '?')}s"
        )
        detail = verdict.get("detail")
        if detail:
            print(f"  {detail}")


def _cmd_audit_loops(args: argparse.Namespace) -> int:
    results = [result for directory in args.dirs for result in audit_loops(directory)]
    if args.json:
        print(json.dumps({"results": [r.__dict__ for r in results]}))
    else:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            warn = f" warnings={len(result.warnings)}" if result.warnings else ""
            print(f"[{status}] {result.name} {result.path}{warn}")
            for error in result.errors:
                print(f"  ERROR: {error}")
            for warning in result.warnings:
                print(f"  WARN: {warning}")
    return 1 if any(not result.passed for result in results) else 0


from bounded_loops.cli_new import _cmd_new  # noqa: E402  (registered by _build_parser above)
from bounded_loops.cli_output import (  # noqa: E402
    _print_lint_results,
    _print_list,
    _print_outcome,
    _print_show,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(msg: str) -> None:
    """Print an error message to stderr."""
    print(f"error: {msg}", file=sys.stderr)


# hardening: a PATH-independent invocation. `bl` may not be on
# PATH after `pip install -e .` in every shell, but `python -m bounded_loops.cli`
# always works — the tests use exactly this hermetic form.
if __name__ == "__main__":
    sys.exit(main())
