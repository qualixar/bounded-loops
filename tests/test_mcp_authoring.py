"""The MCP authoring surface: lint, plan, compose, and run inspection.

The tests that matter most here are the ones that check this surface does NOT become something it
should not be: a prose-to-graph generator whose output nobody can check, a file-read primitive
over the whole filesystem, or a channel through which a model can claim to be a different subject.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bounded_loops import mcp_authoring

REPO_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE = REPO_ROOT / "graphs" / "customer-data-request" / "graph.yaml"


@pytest.fixture
def reference_manifest() -> str:
    """A real shipped graph. Fixtures drift; the thing we ship does not."""
    return _REFERENCE.read_text(encoding="utf-8")


class _RecordingMcp:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.functions: dict[str, Any] = {}

    def tool(self):  # noqa: ANN201 - mirrors FastMCP's untyped decorator factory
        def _decorate(fn):  # noqa: ANN001, ANN202
            self.registered.append(fn.__name__)
            self.functions[fn.__name__] = fn
            return fn

        return _decorate


# ── registration ─────────────────────────────────────────────────────────────


def test_every_authoring_tool_is_registered() -> None:
    recorder = _RecordingMcp()

    mcp_authoring.register(recorder)

    assert set(recorder.registered) == {
        "graph_lint",
        "graph_plan",
        "graph_compose",
        "graph_run",
        "graph_status",
        "graph_state_md",
        "graph_metrics",
        "graph_runs",
        "graph_approve",
        "graph_resume",
    }


def test_the_two_MUTATING_tools_are_gated_by_a_confirm_argument() -> None:
    """`confirm` defaults to False on every side-effecting tool.

    A mutating tool whose default is to mutate would let a model record a human's approval by
    calling it once, exploratively. The preview/confirm pair is the same pattern `bl_run` already
    uses for loops.
    """
    import inspect

    recorder = _RecordingMcp()
    mcp_authoring.register(recorder)

    for name in ("graph_approve", "graph_resume"):
        signature = inspect.signature(recorder.functions[name])
        assert signature.parameters["confirm"].default is False, name

    read_only = set(recorder.registered) - {"graph_approve", "graph_resume"}
    for name in read_only:
        assert "confirm" not in inspect.signature(recorder.functions[name]).parameters, (
            f"{name} takes a confirm argument but has no side effect to gate"
        )


def test_a_mutating_tool_on_a_bad_run_name_refuses_BEFORE_touching_a_facade() -> None:
    recorder = _RecordingMcp()
    mcp_authoring.register(recorder)

    result = recorder.functions["graph_approve"](
        run="../../../etc", node_id="n", decision="approved", confirm=True,
    )

    assert result["ok"] is False
    assert "run_id must be" in result["error"]


def test_the_shipped_server_registers_them() -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from bounded_loops import mcp_server

    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert {
        "graph_lint", "graph_plan", "graph_compose", "graph_status",
        "graph_approve", "graph_resume",
    } <= names


def test_NO_tool_accepts_a_subject_identity_argument() -> None:
    """An approval receipt must name who really granted it.

    If a subject could arrive as a tool argument, a model could attribute its own decision to a
    human. The subject comes from the OS user running the server and from nowhere else.
    """
    recorder = _RecordingMcp()
    mcp_authoring.register(recorder)

    for name, fn in recorder.functions.items():
        parameters = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        for parameter in parameters:
            assert "subject" not in parameter.lower(), f"{name}({parameter})"
            assert "user" not in parameter.lower(), f"{name}({parameter})"


def test_no_tool_takes_a_filesystem_path_for_a_run() -> None:
    """A run is addressed by NAME so the validator can refuse a traversal."""
    recorder = _RecordingMcp()
    mcp_authoring.register(recorder)

    for name, fn in recorder.functions.items():
        parameters = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        for parameter in parameters:
            assert parameter not in {"path", "run_dir", "directory"}, f"{name}({parameter})"


# ── lint ─────────────────────────────────────────────────────────────────────


def test_a_shipped_reference_graph_lints_clean(reference_manifest: str) -> None:
    result = mcp_authoring._lint(reference_manifest)

    assert result["ok"] is True
    assert result["graph_id"] == "customer-data-request"
    assert len(result["nodes"]) >= 3


@pytest.mark.parametrize(
    ("mutation", "replacement", "expected_code"),
    [
        ("bounded-loops.dev/graph/v1", "bogus/v9", "api_version"),
        ("kind: loop", "kind: wizardry", "unknown_node_kind"),
    ],
)
def test_a_real_defect_returns_its_code_AND_a_fix(
    reference_manifest: str, mutation: str, replacement: str, expected_code: str,
) -> None:
    """Mutations of a shipped graph, not hand-written broken fixtures.

    A hand-written fixture can be broken in a way the real authoring path never produces; mutating
    something that genuinely ships exercises the refusal a user will actually hit.
    """
    result = mcp_authoring._lint(reference_manifest.replace(mutation, replacement, 1))

    assert result["ok"] is False
    assert result["refusal"]["code"] == expected_code
    assert result["refusal"]["fix"], "a refusal with no fix teaches a host model nothing"
    assert result["refusal"]["pointer"] is not None


def test_a_mutable_package_reference_is_refused(reference_manifest: str) -> None:
    """The digest is the whole point: it names the exact bytes that will run."""
    import re

    mutated = re.sub(r'"sha256:[0-9a-f]{64}"', '"latest"', reference_manifest, count=1)

    result = mcp_authoring._lint(mutated)

    assert result["refusal"]["code"] == "mutable_package_reference"
    assert "digest" in result["refusal"]["fix"].lower()


def test_an_implausibly_large_manifest_is_refused_before_the_validator() -> None:
    result = mcp_authoring._lint("x" * (mcp_authoring._MAX_MANIFEST_BYTES + 1))

    assert result["ok"] is False
    assert "exceeds" in result["refusal"]["message"]


# ── plan ─────────────────────────────────────────────────────────────────────


def test_planning_a_shipped_graph_reports_where_it_will_PAUSE(reference_manifest: str) -> None:
    """The approval node is the thing a human most needs to know about in advance."""
    result = mcp_authoring._plan(reference_manifest)

    assert result["ok"] is True
    assert result["plan_id"].startswith("sha256:")
    assert result["pauses_at"] == ["approve-customer"]
    approval = next(n for n in result["nodes"] if n["node_id"] == "approve-customer")
    assert approval["pauses_for_a_human"] is True


def test_a_plan_says_plainly_that_nothing_has_run(reference_manifest: str) -> None:
    result = mcp_authoring._plan(reference_manifest)

    assert "Nothing has run" in result["compiled_only"]


def test_graph_run_never_executes_and_says_why(reference_manifest: str) -> None:
    """`graph_run` returning a plan and refusing to execute is a deliberate contract.

    Executing writes progress to stdout, which is this server's JSON-RPC transport. Wrapping the
    existing path would corrupt the framing mid-run, so the tool reports the command instead.
    """
    recorder = _RecordingMcp()
    mcp_authoring.register(recorder)

    result = recorder.functions["graph_run"](reference_manifest)

    assert result["ok"] is True
    assert result["executed"] is False
    assert "bl graph run --execute" in result["how_to_execute"]


# ── compose ──────────────────────────────────────────────────────────────────


def test_compose_produces_a_manifest_that_actually_LINTS() -> None:
    """The whole value of compose is that its output is checkable. So check it."""
    result = mcp_authoring.compose(
        graph_id="composed",
        nodes=[{"id": "claim", "kind": "research_claim", "outputs": {"o": "internal"}}],
    )

    assert result["ok"] is True
    assert mcp_authoring._lint(result["manifest"])["ok"] is True
    assert mcp_authoring._plan(result["manifest"])["ok"] is True


def test_compose_fills_the_required_fields_at_the_SAFE_end_of_their_range() -> None:
    import yaml

    result = mcp_authoring.compose(
        graph_id="defaults",
        nodes=[{"id": "claim", "kind": "research_claim", "outputs": {"o": "internal"}}],
    )
    node = yaml.safe_load(result["manifest"])["nodes"][0]

    assert node["budget"]["max_attempts"] == 1
    assert node["isolation"] == "workspace_only"
    # Empty, not read_only: an effect is a grant of authority, so the default grants nothing.
    assert node["effects"] == []
    assert yaml.safe_load(result["manifest"])["policies"]["fail_mode"] == "fail_closed"
    assert "grant of authority" in result["defaults_applied"]


def test_compose_never_overwrites_a_field_the_CALLER_stated() -> None:
    import yaml

    result = mcp_authoring.compose(
        graph_id="explicit",
        nodes=[
            {
                "id": "claim",
                "kind": "research_claim",
                "outputs": {"o": "internal"},
                "isolation": "process_restricted",
                "budget": {"max_attempts": 4, "max_wallclock_s": 30},
            }
        ],
    )
    node = yaml.safe_load(result["manifest"])["nodes"][0]

    assert node["isolation"] == "process_restricted"
    assert node["budget"]["max_attempts"] == 4


def test_compose_reports_a_missing_loop_digest_as_a_GAP_rather_than_inventing_one() -> None:
    """A fabricated digest names a package that does not exist. That is the worst answer."""
    result = mcp_authoring.compose(
        graph_id="needs-a-package",
        nodes=[{"id": "ship", "kind": "loop", "outputs": {"o": "internal"}}],
    )

    assert result["ok"] is False
    assert [gap["gap"] for gap in result["gaps"]] == ["no loop_package digest"]
    gap = result["gaps"][0]
    assert "cannot be invented" in gap["why"]
    assert "bl_search_loops" in gap["next_step"]
    assert "sha256:" not in result["manifest"], "a digest was fabricated"


def test_compose_refuses_an_unknown_kind_and_lists_the_real_ones() -> None:
    result = mcp_authoring.compose(graph_id="x", nodes=[{"id": "a", "kind": "wizardry"}])

    assert result["ok"] is False
    assert result["refusal"]["code"] == "unknown_node_kind"
    assert "join" in result["refusal"]["fix"]


def test_compose_refuses_a_node_with_no_id() -> None:
    result = mcp_authoring.compose(graph_id="x", nodes=[{"kind": "research_claim"}])

    assert result["ok"] is False
    assert result["refusal"]["code"] == "missing_field"


def test_compose_surfaces_the_engines_OWN_refusal_when_the_sketch_is_unrunnable() -> None:
    """A join with no incoming edge is refused by the compiler, and compose must not hide that."""
    result = mcp_authoring.compose(
        graph_id="lonely-join",
        nodes=[{"id": "j", "kind": "join", "mode": "all_successful", "outputs": {"o": "internal"}}],
    )

    assert result["ok"] is False
    assert result["refusal"]["code"] == "impossible_join"


# ── run inspection and its containment ───────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    ["../../../etc", "..", "/etc/passwd", "../.bounded-loops", "a/../../b"],
)
def test_a_traversal_attempt_in_a_run_NAME_is_refused(hostile: str) -> None:
    """This surface takes a name, not a path, so the run-id validator is the containment."""
    result = mcp_authoring._with_run(hostile, lambda path: {"leaked": str(path)})

    assert result["ok"] is False
    assert "leaked" not in result
    assert "run_id must be" in result["error"] or "no run named" in result["error"]


def test_a_missing_run_is_an_error_not_an_empty_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path))

    result = mcp_authoring._with_run("nope", lambda path: {"unreachable": True})

    assert result["ok"] is False
    assert "no run named" in result["error"]


def test_listing_runs_in_an_empty_workspace_is_an_empty_list_not_a_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path))

    result = mcp_authoring.runs()

    assert result["ok"] is True
    assert result["runs"] == []


def test_runs_are_listed_newest_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(tmp_path))
    runs_dir = tmp_path / ".bounded-loops" / "runs"
    runs_dir.mkdir(parents=True)
    for name in ("20260101T000000Z-aaaaaa", "20260814T120000Z-bbbbbb"):
        (runs_dir / name).mkdir()

    result = mcp_authoring.runs()

    assert result["runs"] == ["20260814T120000Z-bbbbbb", "20260101T000000Z-aaaaaa"]


def test_the_read_subject_is_the_RUNS_OWN_organization_not_the_os_user() -> None:
    """Replaces a test of a helper that no longer exists, and pins the reason it went away.

    `_subject()` returned the OS user, which read well ("the receipt names the person") and broke
    every read: `SameTenantArenaAuthorizer` authorizes only when `subject_id == organization_id`.
    The subject now comes from the run's identity, and the honest consequence — that an approval
    receipt names the ORGANIZATION rather than a person — is documented rather than papered over.
    """
    import inspect

    source = inspect.getsource(mcp_authoring._facade_and_payload)
    assert '"subject_id": identity.organization_id' in source
    assert not hasattr(mcp_authoring, "_subject"), "the misleading helper is back"
    # The limitation must stay stated where the next reader will find it.
    assert "not the person" in source or "not a named person" in source or "the tenant" in source


def test_every_tool_result_survives_a_json_round_trip(reference_manifest: str) -> None:
    for result in (
        mcp_authoring._lint(reference_manifest),
        mcp_authoring._plan(reference_manifest),
        mcp_authoring.compose(
            graph_id="rt",
            nodes=[{"id": "claim", "kind": "research_claim", "outputs": {"o": "internal"}}],
        ),
        mcp_authoring.runs(),
    ):
        assert json.loads(json.dumps(result)) == result


# ── the gap that let a broken read path ship ─────────────────────────────────


def test_a_run_the_ENGINE_produced_can_actually_be_READ(tmp_path: Path, monkeypatch) -> None:
    """Every test above checked a REFUSAL. None checked a success, and the success was broken.

    `for_run_dir` defaults to `SameTenantArenaAuthorizer`, which authorizes a read only when
    `subject_id == organization_id`. This surface passed the OS user, so every read of every real
    run raised "Arena reader is unauthorized" — and no test noticed, because the suite proved
    traversal was refused and a missing run errored, and never once opened a run that existed.

    So this runs the engine and reads the result back.
    """
    import os
    import subprocess

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("BOUNDED_LOOPS_WORKSPACE", str(project))
    completed = subprocess.run(
        ["uv", "run", "bl", "graph", "run", "--execute"],
        cwd=REPO_ROOT,
        env={**os.environ, "BOUNDED_LOOPS_WORKSPACE": str(project), "TMPDIR": "/tmp"},
        capture_output=True, text=True, timeout=300,
    )
    runs_dir = project / ".bounded-loops" / "runs"
    if completed.returncode != 0 or not runs_dir.is_dir():
        pytest.skip(f"the engine could not run here: {completed.stderr[-300:]}")

    listed = mcp_authoring.runs()
    assert listed["ok"] is True
    assert len(listed["runs"]) == 1

    status = mcp_authoring._with_run(listed["runs"][0], mcp_authoring._status_payload)
    assert status["ok"] is True, f"reading a real run failed: {status.get('error')}"
    assert status["projection"]["run_state"] == "SUCCEEDED"
    assert status["projection"]["nodes"], "a projection with no nodes is not a projection"

    markdown = mcp_authoring._with_run(listed["runs"][0], mcp_authoring._state_md_payload)
    assert markdown["ok"] is True
    assert "SUCCEEDED" in markdown["markdown"]
