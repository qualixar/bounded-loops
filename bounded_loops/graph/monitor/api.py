"""The monitor's JSON API — a thin shim over the SAME functions the MCP tools call.

Not a parallel implementation. Every route below dispatches to `mcp_authoring`,
`mcp_discovery`, `capability_report`, or `schema_form`, so the UI and a host agent driving the
engine over MCP cannot disagree about what the engine did — there is nothing for them to disagree
about. If a route ever needs logic of its own, that logic belongs in the application layer where
both surfaces can reach it.

Pure dispatch: `handle()` takes a route and a payload and returns a dict. No HTTP, no sockets, no
globals. The server is then a transport with no behaviour of its own, and every route is testable
without opening a port.

**There is no route that takes an instruction, and that is the architecture.** The monitor holds
no keys and makes no model calls. Work is described to your ORCHESTRATOR — Claude Code, Codex,
Cursor, a CLI — which composes graphs over MCP using the shipped skill. This surface watches what
that produced, configures any authorable field on it through schema-driven forms, approves human
gates, and starts runs. A text box here would have needed a model behind it to be useful and would
have been a search box pretending otherwise; `handoff` exists instead, and returns the exact
command to continue in the agent that can actually compose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from bounded_loops.domain.errors import ManifestError
from bounded_loops.graph.adapters.enforcement.snapshot import platform_snapshot
from bounded_loops.graph.application.capability_report import capability_report
from bounded_loops.graph.monitor import schema_form
from bounded_loops.workspace import Workspace, discover, ensure

#: Longest manifest or graph name the API will accept. A UI field is not a reason to allow an
#: unbounded write.
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_NAME_LENGTH = 64

_CAPABILITY_CACHE: dict[str, Mapping[str, Any]] = {}


def routes() -> tuple[str, ...]:
    """Every route this API answers. Used by the server and by a test that pins the surface."""
    return tuple(sorted(_ROUTES))


def handle(route: str, payload: Mapping[str, Any] | None = None) -> dict:
    """Dispatch one API call. Returns `{"ok": bool, ...}` and never raises for user input."""
    handler = _ROUTES.get(route)
    if handler is None:
        return {"ok": False, "error": f"no such route: {route}"}
    body = payload or {}
    try:
        return handler(body)
    except ManifestError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a UI must get an error, never a dropped connection
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── read-only routes ─────────────────────────────────────────────────────────


def _capabilities(_payload: Mapping[str, Any]) -> dict:
    """The capability contract. Cached: probing shells out to check for a container runtime."""
    if "document" not in _CAPABILITY_CACHE:
        _CAPABILITY_CACHE["document"] = capability_report(platform=platform_snapshot())
    return {"ok": True, "capabilities": dict(_CAPABILITY_CACHE["document"])}


def _forms(_payload: Mapping[str, Any]) -> dict:
    """Form descriptors generated from the compiler's schema, so the UI cannot drift from it."""
    return {"ok": True, "forms": dict(schema_form.form_document())}


def _catalog(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_discovery

    keyless = payload.get("keyless")
    return {
        "ok": True,
        **mcp_discovery.catalog(
            role=_optional_str(payload.get("role")),
            gate_kind=_optional_str(payload.get("gate_kind")),
            keyless=keyless if isinstance(keyless, bool) else None,
        ),
    }


def _search(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_discovery

    description = payload.get("task_description")
    if not isinstance(description, str) or not description.strip():
        return {"ok": False, "error": "task_description must be a non-empty string"}
    return {"ok": True, **mcp_discovery.search_loops(description, limit=8)}


def _lint(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    manifest = _manifest_of(payload)
    if isinstance(manifest, dict):
        return manifest
    return mcp_authoring._lint(manifest)


def _plan(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    manifest = _manifest_of(payload)
    if isinstance(manifest, dict):
        return manifest
    return mcp_authoring._plan(manifest)


def _compose(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    graph_id = payload.get("graph_id")
    nodes = payload.get("nodes")
    if not isinstance(graph_id, str) or not graph_id:
        return {"ok": False, "error": "graph_id must be a non-empty string"}
    if not isinstance(nodes, list):
        return {"ok": False, "error": "nodes must be a list"}
    return mcp_authoring.compose(
        graph_id=graph_id,
        nodes=nodes,
        edges=payload.get("edges") if isinstance(payload.get("edges"), list) else None,
        version=str(payload.get("version") or "1.0.0"),
        policies=payload.get("policies") if isinstance(payload.get("policies"), dict) else None,
        connection_slots=(
            payload.get("connection_slots")
            if isinstance(payload.get("connection_slots"), list)
            else None
        ),
    )


def _runs(_payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    return mcp_authoring.runs()


def _run_status(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    name = payload.get("run")
    if not isinstance(name, str):
        return {"ok": False, "error": "run must be a string"}
    return mcp_authoring._with_run(name, mcp_authoring._status_payload)


def _run_metrics(payload: Mapping[str, Any]) -> dict:
    from bounded_loops import mcp_authoring

    name = payload.get("run")
    if not isinstance(name, str):
        return {"ok": False, "error": "run must be a string"}
    return mcp_authoring._with_run(name, mcp_authoring._metrics_payload)


def _workspace_info(_payload: Mapping[str, Any]) -> dict:
    workspace = discover()
    return {
        "ok": True,
        "root": str(workspace.root),
        "project_root": str(workspace.project_root),
        "origin": workspace.origin,
        "reason": workspace.reason,
        "exists": workspace.exists(),
        "graphs": _graph_names(workspace),
    }


def _graph_read(payload: Mapping[str, Any]) -> dict:
    workspace = discover()
    name = _safe_name(payload.get("name"))
    if isinstance(name, dict):
        return name
    path = workspace.graphs_dir / f"{name}.yaml"
    if not path.is_file() or path.is_symlink():
        return {"ok": False, "error": f"no saved graph named {name!r}"}
    return {"ok": True, "name": name, "manifest": path.read_text(encoding="utf-8")}


# ── the two writing routes ───────────────────────────────────────────────────


def _graph_save(payload: Mapping[str, Any]) -> dict:
    """Save a manifest into `.bounded-loops/graphs/<name>.yaml`, and nowhere else.

    The name is validated to a single safe path segment, the target must stay inside the
    workspace's graphs directory after resolution, and an existing symlink is refused rather than
    written through. A UI text field is not authority to write anywhere on the disk.

    The manifest is linted first: saving something the compiler would refuse just moves the
    failure later, when the person has stopped looking at the form that caused it.
    """
    from bounded_loops import mcp_authoring

    name = _safe_name(payload.get("name"))
    if isinstance(name, dict):
        return name
    manifest = _manifest_of(payload)
    if isinstance(manifest, dict):
        return manifest

    linted = mcp_authoring._lint(manifest)
    if not linted.get("ok"):
        return {"ok": False, "refusal": linted.get("refusal"), "saved": False}

    workspace = discover()
    ensure(workspace)
    target = (workspace.graphs_dir / f"{name}.yaml").resolve()
    if not target.is_relative_to(workspace.graphs_dir.resolve()):
        return {"ok": False, "error": "refusing to write outside the workspace"}
    if target.is_symlink():
        return {"ok": False, "error": f"{target.name} is a symlink; refusing to write through it"}
    target.write_text(manifest, encoding="utf-8")
    return {"ok": True, "saved": True, "path": str(target), "name": name}


def _approve(payload: Mapping[str, Any]) -> dict:
    """Record a human decision. MUTATING, and gated by the same confirm the MCP tool uses.

    The subject is NOT taken from the request. It comes from the account running this server, so a
    decision recorded here names the person who is sitting at the machine.
    """
    from bounded_loops import mcp_authoring

    run = payload.get("run")
    node_id = payload.get("node_id")
    decision = payload.get("decision")
    if not all(isinstance(value, str) and value for value in (run, node_id, decision)):
        return {"ok": False, "error": "run, node_id and decision are all required strings"}
    return mcp_authoring._mutate(
        str(run),
        lambda facade, request: mcp_authoring._graph_approve_handler(
            facade,
            request,
            node_id=str(node_id),
            decision=str(decision),
            confirm=bool(payload.get("confirm", False)),
        ),
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _manifest_of(payload: Mapping[str, Any]) -> str | dict:
    manifest = payload.get("manifest")
    if not isinstance(manifest, str) or not manifest.strip():
        return {"ok": False, "error": "manifest must be a non-empty string"}
    if len(manifest.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        return {"ok": False, "error": f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes"}
    return manifest


def _safe_name(raw: object) -> str | dict:
    """One path segment: letters, digits, dash, underscore. Anything else is refused."""
    if not isinstance(raw, str) or not raw:
        return {"ok": False, "error": "name must be a non-empty string"}
    if len(raw) > _MAX_NAME_LENGTH:
        return {"ok": False, "error": f"name must be at most {_MAX_NAME_LENGTH} characters"}
    if not raw.replace("-", "").replace("_", "").isalnum():
        return {
            "ok": False,
            "error": "name may hold only letters, digits, '-' and '_' — no paths or dots",
        }
    return raw


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _graph_names(workspace: Workspace) -> list[str]:
    if not workspace.graphs_dir.is_dir():
        return []
    return sorted(
        path.stem
        for path in workspace.graphs_dir.glob("*.yaml")
        if path.is_file() and not path.is_symlink()
    )



# ── running a graph, and handing it back to your agent ───────────────────────


def _agents(_payload: Mapping[str, Any]) -> dict:
    """Which orchestrator CLIs this host can run. INFORMATIONAL — it gates nothing.

    The monitor takes no instructions, so this is not a permission check: it is context. Seeing
    that `claude` and `codex` are on PATH tells you which agent could be driving the graphs you are
    looking at, and seeing that none are tells you why nothing is appearing on its own.
    """
    from bounded_loops.graph.adapters.preflight.runner_preflight import (
        default_runner_profiles,
        preflight_runners,
    )

    report = preflight_runners(default_runner_profiles())
    admitted = [
        {
            "id": runner.id,
            "available": bool(runner.available),
            "version": runner.version,
            "admission": runner.admission,
            # Reported because it is the honest part: a binary on PATH proves the binary is on
            # PATH. It does not prove the CLI is logged in, entitled, or safe headless — the
            # preflight says so explicitly and so does this.
            "not_proven": list(runner.claims_not_proven),
        }
        for runner in report.runners
    ]
    return {
        "ok": True,
        "admitted": admitted,
        "any_available": any(entry["available"] for entry in admitted),
        "note": (
            "Informational. This console never sends an instruction to any of these — you do that "
            "in the agent itself, which composes graphs over MCP."
        ),
    }


def _execute(payload: Mapping[str, Any]) -> dict:
    """Preview, then actually run a graph. MUTATING when confirmed.

    `confirm=False` returns what the graph DECLARES it will do — its effects, its ceilings, where
    it pauses for a human — without touching anything. That preview is not a formality: a browser
    button that starts real work on someone's machine should show them the irreversible effects
    first, and "it published something" is a bad way to learn a graph had a publish node.

    `confirm=True` starts the run in a background thread and returns immediately with the run's
    name. It does not wait: a run takes minutes, an HTTP request should not, and the receipt log is
    the progress report — the caller opens the event stream on the returned name and watches.

    Unlike the MCP surface, executing here is fine: that server speaks JSON-RPC over stdout, where
    the execution path's progress output would corrupt the framing. This one speaks HTTP, so the
    same output simply lands in the terminal where the operator started the monitor.
    """
    manifest = _manifest_of(payload)
    if isinstance(manifest, dict):
        return manifest

    from bounded_loops import mcp_authoring

    planned = mcp_authoring._plan(manifest)
    if not planned.get("ok"):
        return {"ok": False, "refusal": planned.get("refusal"), "started": False}

    effects = sorted({effect for node in planned["nodes"] for effect in node["effects"]})
    ceilings = [
        {
            "node_id": node["node_id"],
            "max_attempts": node["max_attempts"],
            "deadline_s": (node["hard_deadline_ms"] or 0) // 1000,
        }
        for node in planned["nodes"]
    ]

    if not payload.get("confirm"):
        return {
            "ok": True,
            "started": False,
            "plan_id": planned["plan_id"],
            "effects": effects,
            "ceilings": ceilings,
            "pauses_at": planned["pauses_at"],
            "irreversible": [
                effect for effect in effects if effect in {"irreversible", "financial"}
            ],
            "what_confirming_does": (
                "Starts this graph on this machine, in the sandbox each node declares. Nodes with "
                "an irreversible or financial effect cannot be undone by stopping the run."
            ),
        }

    from bounded_loops.workspace import discover, ensure, mint_run_directory_name

    workspace = discover()
    ensure(workspace)
    run_name = mint_run_directory_name()
    out_dir = workspace.run_dir(run_name)

    started = _start_run_thread(manifest_text=manifest, out_dir=out_dir)
    if started is not None:
        return {"ok": False, "started": False, "error": started}
    return {
        "ok": True,
        "started": True,
        "run": run_name,
        "watch": f"/events?run={run_name}",
        "note": (
            "Started. Follow it on the event stream — the receipt log is the progress report, and "
            "only a SUCCEEDED terminal state means the work was verified."
        ),
    }


def _start_run_thread(*, manifest_text: str, out_dir: Path) -> str | None:
    """Launch the run in a daemon thread. Returns an error string, or None when it started.

    Setup failures come back synchronously because the caller can still act on them. Failures
    DURING the run do not: they are recorded in the run's own receipt log, which is the only place
    a run's outcome is allowed to live.
    """
    import threading

    from bounded_loops.graph.graph_composition import execute_graph_run

    def _run() -> None:
        try:
            execute_graph_run(
                manifest_text=manifest_text,
                manifest_suffix=".yaml",
                connections_raw=(),
                node_prompts={},
                out_dir=out_dir,
            )
        except Exception:  # noqa: BLE001 - a thread that raises silently kills the run's record
            import traceback

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "monitor-error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8",
                )
            except OSError:
                pass

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create {out_dir}: {exc}"
    threading.Thread(target=_run, name=f"bl-run-{out_dir.name}", daemon=True).start()
    return None


def _handoff(payload: Mapping[str, Any]) -> dict:
    """The exact thing to paste into your orchestrator to continue this graph there.

    This console deliberately takes no instructions, so anyone who wants to restructure a graph has
    to go back to the agent that composes. Leaving them to work out how would be a dead end, so the
    command is generated rather than described.
    """
    name = _safe_name(payload.get("name"))
    if isinstance(name, dict):
        return name
    workspace = discover()
    path = workspace.graphs_dir / f"{name}.yaml"
    if not path.is_file():
        return {"ok": False, "error": f"no saved graph named {name!r} — save it first"}
    return {
        "ok": True,
        "path": str(path),
        "command": f"bl graph plan {path}",
        "mcp_tool": f'graph_interview(name="{name}")',
        "say_to_your_agent": (
            f"The graph '{name}' is saved at {path}. Read it, run graph_interview on it, ask me "
            "the questions it says must be asked, then apply my answers with graph_configure."
        ),
    }


# ── the route table (last, so every handler above is defined) ────────────────

_ROUTES: Mapping[str, Callable[[Mapping[str, Any]], dict]] = {
    "capabilities": _capabilities,
    "forms": _forms,
    "catalog": _catalog,
    "search": _search,
    "lint": _lint,
    "plan": _plan,
    "compose": _compose,
    "runs": _runs,
    "run": _run_status,
    "metrics": _run_metrics,
    "workspace": _workspace_info,
    "graph.read": _graph_read,
    "graph.save": _graph_save,
    "approve": _approve,
    "agents": _agents,
    "execute": _execute,
    "handoff": _handoff,
}

#: Routes that change something. Everything else must be safe to call on a timer.
MUTATING_ROUTES = frozenset({"graph.save", "approve", "execute"})


def is_mutating(route: str) -> bool:
    return route in MUTATING_ROUTES
