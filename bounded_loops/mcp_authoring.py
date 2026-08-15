"""MCP authoring and run-inspection tools — the half of the contract that acts on graphs.

`mcp_discovery` tells a host what the engine can do. This module lets it *do* it: assemble a
manifest, get it validated, see the compiled plan, and read a run's receipts afterwards.

Two design decisions worth stating up front, because both are the opposite of what the obvious
implementation would do.

**`graph_compose` does not turn prose into a graph.** Turning "check this repo's release
readiness" into nodes and edges needs a language model, and this tool has no keys and should never
have any — the host IS the model. So the split is: the host does intent -> structure, informed by
`bl_capabilities` and `bl_search_loops`, and `graph_compose` does structure -> *valid* manifest,
filling required fields with safe defaults, refusing what the compiler would refuse, and reporting
the gaps it cannot fill as tickets. Everything it returns is checkable, which a prose-to-graph
tool's output would not be.

**A run is addressed by NAME, never by path.** Every tool below takes a run-directory name and
resolves it through `Workspace.run_dir`, which validates it. Accepting a path from a model
argument would make this surface a read primitive over the whole filesystem; `../../../etc` is a
run id the validator rejects.

The subject identity is derived from the run itself, never from a tool argument — a model can say
which run and which decision, and can never claim to be someone. See `_facade_and_payload` for
what that means for approval attribution, which is weaker than it should be and says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from bounded_loops.domain.errors import ManifestError
from bounded_loops.graph.application.refusals import explain
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.workspace import Workspace, discover

_MAX_MANIFEST_BYTES = 512 * 1024


def register(mcp: object) -> None:
    """Wire the authoring and inspection tools onto an MCPServer instance."""
    tool: Callable[..., Any] = mcp.tool  # type: ignore[attr-defined]

    @tool()
    def graph_lint(manifest_yaml: str) -> dict:
        """Validate a graph manifest and explain every refusal in plain language.

        Returns `{"ok": true}` for a valid manifest, or the refusal with its code, the JSON
        pointer to the offending field, and — this is the useful part — what to change. Run this
        before `graph_plan`; a manifest that does not lint cannot compile.

        Read-only."""
        return _lint(manifest_yaml)

    @tool()
    def graph_plan(manifest_yaml: str) -> dict:
        """Compile a manifest to an execution plan and report what it WILL do.

        Returns the plan id, the execution waves, each node's declared effects and isolation
        tier, where the run will pause for a human, and the repair bound. Compiling is not
        running: nothing is executed and no run directory is created.

        Read-only."""
        return _plan(manifest_yaml)

    @tool()
    def graph_compose(
        graph_id: str,
        nodes: list[dict],
        edges: list[dict] | None = None,
        version: str = "1.0.0",
        policies: dict | None = None,
        connection_slots: list[dict] | None = None,
    ) -> dict:
        """Assemble a VALID manifest from a node/edge sketch, and report what is missing.

        You supply the structure — which nodes, of which kind, wired how. This fills in the
        fields the compiler requires but you did not state (budgets, effects, isolation), then
        lints the result, and returns:

          manifest  — the YAML, ready for `graph_plan` or `bl graph run`
          gaps      — what it could NOT fill for you, as tickets: a loop node with no package
                      digest, a node whose verification has no shipped mechanical gate
          refusal   — if the assembled manifest still cannot compile, why, and how to fix it

        It never invents a `loop_package` digest: digests are content-addressed, so a made-up one
        names a package that does not exist. Use `bl_search_loops` to find a real one.

        Returns a draft. Nothing runs."""
        return compose(
            graph_id=graph_id,
            nodes=nodes,
            edges=edges,
            version=version,
            policies=policies,
            connection_slots=connection_slots,
        )

    @tool()
    def graph_run(manifest_yaml: str) -> dict:
        """PREVIEW what running this manifest would do. This tool never executes.

        Executing a graph is deliberately not available over MCP in this release, for a reason
        worth knowing: this server speaks JSON-RPC over stdio, and the execution path writes
        progress to stdout, which would corrupt the transport framing mid-run. Exposing it needs
        a print-free execution core, not a wrapper.

        So this returns the compiled plan plus the exact command to run it, which the human (or
        the monitor UI, which has their connections configured) executes.

        Read-only."""
        planned = _plan(manifest_yaml)
        if not planned.get("ok"):
            return planned
        return {
            **planned,
            "executed": False,
            "how_to_execute": (
                "Write the manifest to a file and run: "
                "bl graph run --execute <manifest.yaml>  "
                "(the run lands in .bounded-loops/runs/<stamp>-<rand>/ and the path is printed)"
            ),
        }

    @tool()
    def graph_status(run: str) -> dict:
        """Read a run's current state from its receipt log, by run-directory NAME.

        Returns each node's state, attempt counts, the current repair round, terminal status, and
        spend. Only a run whose status is SUCCEEDED succeeded; every other terminal state is
        unfinished work.

        Read-only."""
        return _with_run(run, _status_payload)

    @tool()
    def graph_state_md(run: str) -> dict:
        """The same run state as a human-readable markdown document. Read-only."""
        return _with_run(run, _state_md_payload)

    @tool()
    def graph_metrics(run: str) -> dict:
        """What the independent gate actually achieved on this run, with its caveats attached.

        The intervals reported here are labelled with their estimand and method. Read the caveat
        string before quoting a number: the headline rate is a marginal rate, not a per-run one.

        Read-only."""
        return _with_run(run, _metrics_payload)

    @tool()
    def graph_runs() -> dict:
        """List the runs in this project's workspace, newest first. Read-only."""
        return runs()

    @tool()
    def graph_interview(name: str | None = None, manifest_yaml: str | None = None) -> dict:
        """ASK THE HUMAN THESE BEFORE YOU RUN ANYTHING. The questions this graph needs answered.

        Pass a saved graph `name` or a `manifest_yaml`. Returns plain-language questions ordered by
        consequence, each with why it matters, where the answer goes, and what the engine does if
        nobody answers.

        This exists because a graph has around forty authorable fields, and the ones that matter
        most are exactly the ones you must not quietly default: whether a person approves an
        irreversible effect, what a node is allowed to spend, whether a retry would send something
        twice. Ask the `must_ask` questions in your own words, take the answers, and apply them
        with `graph_configure`.

        Never answer a HIGH question on someone's behalf and then report the graph as ready. Say
        which defaults you applied.

        Read-only."""
        return _interview(name=name, manifest_yaml=manifest_yaml)

    @tool()
    def graph_configure(name: str, changes: list[dict], confirm: bool = False) -> dict:
        """Apply interview answers to a SAVED graph, through the validator.

        Each change is `{"pointer": "/nodes/0/isolation", "value": "process_restricted"}` — the
        pointer comes from `graph_interview`, so you are not guessing at the shape.

        MUTATING and gated: `confirm=False` returns the diff and the lint result of the PROPOSED
        graph without writing. `confirm=True` writes it, and only if it still compiles — a
        configuration that the compiler would refuse is rejected here rather than saved to fail
        later, when the person who answered has stopped looking.

        Writes only to `.bounded-loops/graphs/<name>.yaml`."""
        return _configure(name=name, changes=changes, confirm=confirm)

    @tool()
    def graph_approve(
        run: str,
        node_id: str,
        decision: str,
        confirm: bool = False,
    ) -> dict:
        """Record a human decision for a paused approval node, then resume the run past it.

        MUTATING, and gated: with `confirm=False` this returns a preview of exactly what it would
        record and changes nothing. Call again with `confirm=True` to record it.

        `decision` is 'approved' or 'rejected'. WHO approved is NOT yours to state: the subject is
        derived from the run, never from your arguments, so a model cannot attribute a decision to
        someone. Be aware of what the receipt therefore records — on a local run the actor is the
        run's ORGANIZATION, not a named person, which is the same attribution `bl graph approve`
        produces.

        An approval authorizes every effect reachable DOWNSTREAM of it, not just the effects
        declared on the approval node itself. That is what the recorded grant contains, and it
        is the only sane reading: a gate exists to release what follows it. So an approval
        declaring no effects of its own is NOT inert — approving a bare gate in front of a
        publish authorizes that publish. Call this with confirm=false first; the preview names
        the effects and flags the ones that cannot be undone."""
        return _mutate(
            run,
            lambda facade, payload: _graph_approve_handler(
                facade, payload, node_id=node_id, decision=decision, confirm=confirm,
            ),
        )

    @tool()
    def graph_resume(
        run: str,
        confirm: bool = False,
        max_tokens: int | None = None,
        max_cost_usd: str | None = None,
    ) -> dict:
        """Continue an interrupted run, or one that paused on its spend ceiling.

        MUTATING, and gated the same way: `confirm=False` previews, `confirm=True` resumes.

        A run that stopped BECAUSE it hit a spend ceiling needs a new one supplied here.
        Resuming such a run without raising the ceiling asks it to stop again — which is why the
        new ceiling is an explicit argument rather than an implicit "no limit"."""
        return _mutate(
            run,
            lambda facade, payload: _graph_resume_handler(
                facade, payload, confirm=confirm,
                max_tokens=max_tokens, max_cost_usd=max_cost_usd,
            ),
        )


# ── lint / plan / compose ─────────────────────────────────────────────────────


def _parse(manifest_yaml: str) -> Any:
    """Parse a manifest, refusing anything implausibly large before handing it to the validator."""
    if len(manifest_yaml.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise GraphValidationError(
            code="type",
            pointer="/",
            message=f"manifest exceeds {_MAX_MANIFEST_BYTES} bytes",
        )
    from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml

    return parse_authoring_graph_yaml(manifest_yaml)


def _refusal(exc: GraphValidationError) -> dict:
    """A refusal a host model can act on: the code, where, and what to change."""
    guidance = explain(getattr(exc, "code", "") or "")
    return {
        "ok": False,
        "refusal": {
            "code": getattr(exc, "code", None),
            "pointer": getattr(exc, "pointer", None),
            "message": getattr(exc, "message", str(exc)),
            "means": guidance.summary if guidance else None,
            "fix": guidance.fix if guidance else None,
        },
    }


def _lint(manifest_yaml: str) -> dict:
    try:
        graph = _parse(manifest_yaml)
    except GraphValidationError as exc:
        return _refusal(exc)
    return {
        "ok": True,
        "graph_id": graph.graph_id,
        "version": graph.version,
        "nodes": [{"id": node.id, "kind": node.kind.value} for node in graph.nodes],
        "edges": len(graph.edges),
    }


def _plan(manifest_yaml: str) -> dict:
    from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
    from bounded_loops.graph.domain.authoring import _NULL_POLICY_DIGEST
    from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests

    try:
        graph = _parse(manifest_yaml)
        plan = compile_graph(
            graph,
            CompileSnapshot(
                policy_digest=_NULL_POLICY_DIGEST,
                package_digests=admitted_loop_package_digests(),
                # No connection candidates: a plan compiled here is a preview, and resolving a
                # slot to a real connection is the job of the run, which supplies its own
                # candidates. A graph whose slots cannot be satisfied is refused at run time by
                # the preflight, not silently pre-bound here to something this tool invented.
                connections=(),
            ),
        )
    except GraphValidationError as exc:
        return _refusal(exc)
    except GraphIntegrityError as exc:
        return {"ok": False, "refusal": {"code": "integrity", "message": str(exc)}}

    return {
        "ok": True,
        "plan_id": plan.plan_id,
        "graph_digest": plan.source_graph_digest,
        "nodes": [
            {
                "node_id": node.node_id,
                "kind": node.kind,
                "isolation": node.isolation.value,
                "effects": sorted(effect.value for effect in node.required_effects),
                "max_attempts": node.budgets.get("max_attempts"),
                "hard_deadline_ms": node.hard_deadline_ms,
                # Spend ceilings, because a caller showing someone "the ceilings" before they
                # press Run and omitting the two about MONEY is showing them the reassuring
                # half. `None` here means no ceiling — which is not a large ceiling, and the
                # surfaces that render this say so in those words.
                "max_tokens": node.budgets.get("max_tokens"),
                "max_cost_microunits": node.budgets.get("max_cost_microunits"),
                "pauses_for_a_human": node.kind == "approval",
            }
            for node in plan.nodes
        ],
        # Edges, because a caller drawing the DAG needs them and a node list alone is a bag of
        # boxes. The UI stream found this by trying to render a saved graph and getting no arrows.
        "edges": [
            {
                "from_node": edge.from_node,
                "from_port": edge.from_port,
                "to_node": edge.to_node,
                "to_port": edge.to_port,
                "when": edge.when,
            }
            for edge in plan.edges
        ],
        "pauses_at": [node.node_id for node in plan.nodes if node.kind == "approval"],
        "compiled_only": "This is a plan. Nothing has run and no run directory exists.",
    }


def compose(
    *,
    graph_id: str,
    nodes: list[dict],
    edges: list[dict] | None = None,
    version: str = "1.0.0",
    policies: Mapping[str, Any] | None = None,
    connection_slots: list[dict] | None = None,
) -> dict:
    """Fill a node/edge sketch into a compiler-valid manifest, and report the gaps."""
    import yaml

    from bounded_loops.graph.domain.authoring import NodeKind

    known_kinds = {kind.value for kind in NodeKind}
    gaps: list[dict[str, str]] = []
    filled: list[dict[str, Any]] = []

    for index, raw in enumerate(nodes):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            return {
                "ok": False,
                "refusal": {
                    "code": "missing_field",
                    "pointer": f"/nodes/{index}",
                    "message": "every node needs at least an 'id' and a 'kind'",
                    "fix": "Give each node a string id and one of the supported kinds.",
                },
            }
        kind = raw.get("kind")
        if kind not in known_kinds:
            return {
                "ok": False,
                "refusal": {
                    "code": "unknown_node_kind",
                    "pointer": f"/nodes/{index}/kind",
                    "message": f"{kind!r} is not a node kind this engine runs",
                    "fix": f"Use one of: {', '.join(sorted(known_kinds))}. "
                           "bl_capabilities lists what each one requires.",
                },
            }

        node = _fill_node(raw)
        if kind == "loop" and not raw.get("loop_package"):
            gaps.append(
                {
                    "node_id": raw["id"],
                    "gap": "no loop_package digest",
                    "why": (
                        "A loop node runs a content-addressed package; the digest cannot be "
                        "invented because it names the exact bytes that will execute."
                    ),
                    "next_step": (
                        "Find a shipped package with bl_search_loops, or author one and pin it "
                        "with `bl graph digest <dir>`."
                    ),
                }
            )
        filled.append(node)

    manifest: dict[str, Any] = {
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": graph_id,
        "version": version,
        # Required at the top level, and empty is the correct default: a slot declares a
        # capability the graph needs a connection for, and a graph of keyless loops needs none.
        "connection_slots": [dict(slot) for slot in (connection_slots or [])],
        "nodes": filled,
        "edges": [dict(edge) for edge in (edges or [])],
        "policies": dict(policies) if policies else dict(_DEFAULT_POLICIES),
    }
    manifest_yaml = yaml.safe_dump(manifest, sort_keys=False)

    linted = _lint(manifest_yaml)
    return {
        "ok": bool(linted.get("ok")),
        "manifest": manifest_yaml,
        "gaps": gaps,
        "defaults_applied": _DEFAULTS_EXPLANATION,
        **({} if linted.get("ok") else {"refusal": linted["refusal"]}),
    }


_DEFAULT_POLICIES: Mapping[str, Any] = {
    "data_class": "internal",
    "fail_mode": "fail_closed",
    "repair_budget": 0,
}

_DEFAULTS_EXPLANATION = (
    "Unstated required fields were filled at the safe end of their range: one attempt, a 300s "
    "deadline, NO declared effects, workspace_only isolation, fail_closed, and no repair budget. "
    "Widen each one deliberately. In particular, effects default to empty because a declared "
    "effect is a grant of authority, and the narrowest declaration is the correct starting "
    "point. Note this does NOT make an approval node inert: approving a gate releases every "
    "effect reachable downstream of it, whatever the gate itself declares."
)


def _fill_node(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Add the fields the compiler requires, without overwriting anything the caller stated."""
    node = dict(raw)
    node.setdefault("inputs", {})
    node.setdefault("outputs", {})
    # Empty, matching every shipped reference graph: effects are grants, so the default grants
    # nothing. `read_only` would look harmless and still be a declaration nobody asked for.
    node.setdefault("effects", [])
    node.setdefault("isolation", "workspace_only")
    budget = dict(node.get("budget") or {})
    budget.setdefault("max_attempts", 1)
    budget.setdefault("max_wallclock_s", 300)
    node["budget"] = budget
    return node


# ── run inspection ───────────────────────────────────────────────────────────


def _resolve_run(name: str) -> tuple[Workspace, Path]:
    """The run directory for `name` inside this project's workspace.

    `Workspace.run_dir` validates the name through the one run-id validator, so a traversal
    attempt (`../../etc`) is refused here rather than reaching the filesystem.
    """
    workspace = discover()
    run_dir = workspace.run_dir(name)
    if not run_dir.is_dir():
        raise ManifestError(f"no run named {name!r} in {workspace.runs_dir}")
    if run_dir.is_symlink():
        raise ManifestError(f"run {name!r} is a symlink; refusing to follow it")
    return workspace, run_dir


def _with_run(name: str, handler: Callable[[Path], dict]) -> dict:
    try:
        _workspace, run_dir = _resolve_run(name)
    except ManifestError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return {"ok": True, **handler(run_dir)}
    except (GraphIntegrityError, GraphValidationError, ManifestError, OSError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _facade_and_payload(run_dir: Path) -> tuple[Any, dict]:
    """A flat-addressed facade for one run directory, plus the payload the handlers expect.

    Flat addressing (`for_run_dir`) rather than the hosted `runs_root/org/project/run` convention,
    because that is how runs actually live on disk here — the workspace writes
    `.bounded-loops/runs/<name>/` directly. Reconstructing the hosted path math was removed in
    0.4.0 after both auditors called it public-contract debt; this does not bring it back.

    The subject is the run's own organization id, NOT the OS user — and that is load-bearing
    rather than lazy. `for_run_dir` defaults to `SameTenantArenaAuthorizer`, which authorizes a
    read only when `subject_id == organization_id`; passing the OS user here refused EVERY read of
    EVERY run, which is how this was found. `bl graph status` has always passed the organization
    id for the same reason.

    Consequence worth stating plainly, because it is a real limitation and not a detail:
    `_authorize_mutation` uses that same arena authorizer, so an approval recorded through this
    surface carries `actor_id: "<organization>"` — the tenant, not the person. `bl graph approve`
    has the same property today. A receipt that names an organization as the approver is weaker
    evidence than one naming a human, and closing that gap needs a real local-identity concept
    rather than a different string here.
    """
    from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
    from bounded_loops.graph.graph_runtime_facade import LocalGraphRuntimeFacade
    from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests

    facade = LocalGraphRuntimeFacade.for_run_dir(run_dir)
    _plan_obj, identity, _meta = load_plan_from_run_dir(
        run_dir.resolve(), package_digests=admitted_loop_package_digests(),
    )
    payload = {
        "subject_id": identity.organization_id,
        "organization_id": identity.organization_id,
        "project_id": identity.project_id,
        "run_id": identity.run_id,
    }
    return facade, payload


def _mutate(name: str, handler: Callable[[Any, dict], dict]) -> dict:
    """Run a MUTATING handler against one named run, refusing anything unresolvable first."""
    try:
        _workspace, run_dir = _resolve_run(name)
        facade, payload = _facade_and_payload(run_dir)
    except (ManifestError, GraphIntegrityError, GraphValidationError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return handler(facade, payload)


def _graph_approve_handler(
    facade: Any, payload: dict, *, node_id: str, decision: str, confirm: bool,
) -> dict:
    """Delegate to the existing handler, which owns the confirm gate and never prints."""
    from bounded_loops.graph.mcp_graph import graph_approve as _handler

    return _handler(facade, payload, node_id=node_id, decision=decision, confirm=confirm)


def _graph_resume_handler(
    facade: Any, payload: dict, *, confirm: bool,
    max_tokens: int | None, max_cost_usd: str | None,
) -> dict:
    from bounded_loops.graph.mcp_graph import graph_resume as _handler

    return _handler(
        facade, payload, confirm=confirm, max_tokens=max_tokens, max_cost_usd=max_cost_usd,
    )


def _projection(run_dir: Path) -> Any:
    from bounded_loops.graph.application.arena_projection import ArenaReadRequest

    facade, payload = _facade_and_payload(run_dir)
    return facade.status(
        ArenaReadRequest(
            subject_id=payload["subject_id"],
            organization_id=payload["organization_id"],
            project_id=payload["project_id"],
            run_id=payload["run_id"],
        )
    )


def _status_payload(run_dir: Path) -> dict:
    from bounded_loops.graph.mcp_graph import _projection_dict

    return {"run": run_dir.name, "projection": _projection_dict(_projection(run_dir))}


def _state_md_payload(run_dir: Path) -> dict:
    from bounded_loops.graph.application.state_document import render_state_markdown

    return {"run": run_dir.name, "markdown": render_state_markdown(_projection(run_dir))}


def _metrics_payload(run_dir: Path) -> dict:
    from bounded_loops.graph.cli_graph_metrics import metrics_document

    return {"run": run_dir.name, "metrics": metrics_document(run_dir)}


def runs() -> dict:
    """Every run in this project's workspace, newest first by directory name."""
    try:
        workspace = discover()
    except ManifestError as exc:
        return {"ok": False, "error": str(exc)}
    if not workspace.runs_dir.is_dir():
        return {"ok": True, "workspace": str(workspace.root), "runs": []}
    # `is_dir()` follows symlinks, so a planted `runs/anything -> /etc` used to be advertised
    # as a run. Clicking it then failed deeper in, with a refusal written for a corrupt run
    # rather than for a thing that was never a run. Excluded here so the list only ever names
    # what the rest of this module will agree to open.
    names = sorted(
        (
            entry.name
            for entry in workspace.runs_dir.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        ),
        reverse=True,
    )
    return {"ok": True, "workspace": str(workspace.root), "runs": names}

# ── the configuration interview ──────────────────────────────────────────────


def _saved_graph_path(name: str) -> Path:
    """The saved-graph file for `name`, refusing anything that is not one safe segment."""
    if not name or len(name) > 64 or not name.replace("-", "").replace("_", "").isalnum():
        raise ManifestError(
            "a graph name may hold only letters, digits, '-' and '_' — no paths or dots"
        )
    workspace = discover()
    path = workspace.graphs_dir / f"{name}.yaml"
    if path.is_symlink():
        raise ManifestError(f"{name}.yaml is a symlink; refusing to follow it")
    return path


def _interview(*, name: str | None, manifest_yaml: str | None) -> dict:
    """Questions for a saved graph or a supplied manifest."""
    import yaml

    from bounded_loops.graph.application.interview import interview_document

    try:
        if manifest_yaml is None:
            if not name:
                return {"ok": False, "error": "pass either a saved graph name or manifest_yaml"}
            path = _saved_graph_path(name)
            if not path.is_file():
                return {"ok": False, "error": f"no saved graph named {name!r}"}
            manifest_yaml = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(manifest_yaml)
    except (ManifestError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    except yaml.YAMLError as exc:
        return {"ok": False, "error": f"the manifest is not parseable YAML: {exc}"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "a manifest must be a mapping"}
    return {"ok": True, "graph": name, **interview_document(parsed)}


def _configure(*, name: str, changes: list[dict], confirm: bool) -> dict:
    """Apply pointer/value changes to a saved graph, refusing anything that stops compiling."""
    import yaml

    try:
        path = _saved_graph_path(name)
    except ManifestError as exc:
        return {"ok": False, "error": str(exc)}
    if not path.is_file():
        return {"ok": False, "error": f"no saved graph named {name!r}"}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return {"ok": False, "error": f"cannot read {name}.yaml: {exc}"}
    if not isinstance(document, dict):
        return {"ok": False, "error": "a manifest must be a mapping"}

    applied: list[dict] = []
    for change in changes:
        if not isinstance(change, dict) or "pointer" not in change or "value" not in change:
            return {
                "ok": False,
                "error": "each change needs a 'pointer' and a 'value'",
            }
        try:
            before = _apply_pointer(document, str(change["pointer"]), change["value"])
        except (KeyError, IndexError, ValueError) as exc:
            return {"ok": False, "error": f"{change['pointer']}: {exc}"}
        applied.append(
            {"pointer": change["pointer"], "from": before, "to": change["value"]}
        )

    proposed = yaml.safe_dump(document, sort_keys=False)
    linted = _lint(proposed)
    if not linted.get("ok"):
        return {
            "ok": False,
            "applied": applied,
            "refusal": linted.get("refusal"),
            "written": False,
            "why": (
                "These answers produce a graph the compiler refuses, so nothing was written. "
                "Fixing it now is cheaper than discovering it at run time."
            ),
        }
    if not confirm:
        return {
            "ok": True,
            "applied": applied,
            "manifest": proposed,
            "written": False,
            "next": "call again with confirm=true to write it",
        }
    try:
        path.write_text(proposed, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"cannot write {name}.yaml: {exc}"}
    return {"ok": True, "applied": applied, "written": True, "path": str(path)}


def _apply_pointer(document: dict, pointer: str, value: object) -> object:
    """Set one JSON-pointer location, returning what was there. Creates no new structure.

    Deliberately refuses to invent containers: a pointer that does not resolve is a pointer the
    caller got wrong, and silently creating `/nodes/7` in a five-node graph would produce a
    manifest nobody asked for.
    """
    if not pointer.startswith("/"):
        raise ValueError("a pointer must start with '/'")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    target: Any = document
    for part in parts[:-1]:
        target = _descend(target, part)
    last = parts[-1]
    if isinstance(target, list):
        index = _as_index(target, last)
        before = target[index]
        target[index] = value
        return before
    if isinstance(target, dict):
        before = target.get(last)
        target[last] = value
        return before
    raise ValueError(f"cannot set {last!r} on a {type(target).__name__}")


def _descend(target: Any, part: str) -> Any:
    if isinstance(target, list):
        return target[_as_index(target, part)]
    if isinstance(target, dict):
        if part not in target:
            raise ValueError(f"{part!r} does not exist; this tool does not create new structure")
        return target[part]
    raise ValueError(f"cannot descend into a {type(target).__name__}")


def _as_index(target: list, part: str) -> int:
    if not part.isdigit():
        raise ValueError(f"{part!r} is not a list index")
    index = int(part)
    if index >= len(target):
        raise ValueError(f"index {index} is past the end of a {len(target)}-item list")
    return index
