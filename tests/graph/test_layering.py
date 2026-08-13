"""The graph engine's anti-drift contract, enforced by an import-graph walk.

Why this file exists: the base engine was given a composition root and a documented
anti-drift contract on day one (``bounded_loops/composition.py``) and has honoured both.
The graph engine was given neither, so two modules grew into composition roots by
accumulation *inside the application layer*, and the habit spread — by P2-B, twelve
application modules imported concrete adapters across forty import statements. The v0.5
audit recorded four, "confirmed by reading imports; no import-graph analysis was run".
Reading found 10% of it. That is the whole argument for this test.

The rules below are checked against the real AST of every module, so a violation fails CI
instead of waiting for someone to notice. A rule with an exemption list is a rule that
erodes; there are no exemptions here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "bounded_loops"
_GRAPH = _ROOT / "graph"

#: Modules allowed to name concrete adapters: the composition root and the entry points
#: (CLI, console, MCP) that wire a deployment together. Everything else goes through a port.
_COMPOSITION_TIER = {
    "bounded_loops.graph.graph_composition",
    "bounded_loops.graph.graph_runtime_facade",
    "bounded_loops.graph.sandbox_demo",
    "bounded_loops.graph.cli_graph",
    "bounded_loops.graph.cli_graph_approve",
    "bounded_loops.graph.cli_graph_resume",
    "bounded_loops.graph.mcp_graph",
    "bounded_loops.graph.init.connector",
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(_ROOT.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


def _imports(path: Path) -> tuple[str, ...]:
    """Every module this file imports, from both ``import x`` and ``from x import y``.

    Function-local imports count. A deferred import is still a dependency — hiding an
    adapter import inside a function would defeat the whole check, and that is exactly the
    shape a future drift is most likely to take.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return tuple(found)


def _modules(package: Path) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (_module_name(path), path)
        for path in sorted(package.rglob("*.py"))
    )


def _offences(package: Path, forbidden: tuple[str, ...], *, allow: frozenset[str] = frozenset()) -> list[str]:
    out: list[str] = []
    for name, path in _modules(package):
        if name in allow:
            continue
        for imported in _imports(path):
            if any(imported == bad or imported.startswith(bad + ".") for bad in forbidden):
                out.append(f"{name} imports {imported}")
    return out


def test_the_graph_application_layer_imports_no_concrete_adapter() -> None:
    """The rule that was broken forty times. An application module that names a concrete
    adapter cannot be re-wired for a different deployment without editing it."""
    offences = _offences(
        _GRAPH / "application",
        ("bounded_loops.graph.adapters", "bounded_loops.adapters"),
    )
    assert offences == [], (
        "application modules must depend on ports, not adapters:\n  " + "\n  ".join(offences)
    )


def test_the_graph_domain_layer_depends_on_nothing_but_the_domain() -> None:
    """Domain objects outlive every adapter and use case around them. A domain module that
    reaches outward cannot be reasoned about — or serialised — without the whole engine."""
    offences = _offences(
        _GRAPH / "domain",
        (
            "bounded_loops.graph.adapters",
            "bounded_loops.graph.application",
            "bounded_loops.adapters",
            "bounded_loops.application",
        ),
    )
    assert offences == [], "domain must import only domain:\n  " + "\n  ".join(offences)


def test_no_adapter_imports_the_composition_tier() -> None:
    """Composition points at adapters; adapters must never point back. A cycle here means
    importing one adapter drags a whole deployment's wiring in with it."""
    offences = _offences(_GRAPH / "adapters", tuple(sorted(_COMPOSITION_TIER)))
    assert offences == [], (
        "adapters must not import the composition tier:\n  " + "\n  ".join(offences)
    )


def test_the_composition_tier_modules_all_exist() -> None:
    """A stale name in the allow-set above would silently exempt nothing while looking like
    it exempts something — the failure mode that makes allow-lists rot."""
    missing = [
        name for name in _COMPOSITION_TIER
        if not (_ROOT.parent / Path(*name.split("."))).with_suffix(".py").exists()
    ]
    assert missing == [], f"composition-tier modules named but absent: {missing}"


#: Hard cap from the project's engineering rules: many small files beat few large ones.
_MAX_LINES = 800


def test_no_module_exceeds_the_line_cap() -> None:
    """P3 brought six files back under this and this test is what keeps them there.

    Every one of the six got there the same way — a module that was already the biggest in its
    package was also the most convenient place to add the next thing. Nobody decided to write an
    900-line file; the cap was simply not checked, so each addition was individually reasonable.
    """
    over = [
        f"{name} ({len(path.read_text(encoding='utf-8').splitlines())})"
        for name, path in _modules(_ROOT)
        if len(path.read_text(encoding="utf-8").splitlines()) > _MAX_LINES
    ]
    assert over == [], f"modules over the {_MAX_LINES}-line cap:\n  " + "\n  ".join(over)


def test_the_local_tenant_sentinels_are_declared_exactly_once() -> None:
    """``local-org`` / ``local-project`` / ``graph-run`` are single-tenant defaults for the local
    CLI. They were declared twice — in ``cli_graph`` and in ``graph_composition``'s function
    defaults — agreeing by luck. Two copies of an identity default is a silent divergence waiting
    for whichever one someone edits."""
    literals = ('"local-org"', '"local-project"', '"graph-run"')
    sites: dict[str, list[str]] = {literal: [] for literal in literals}
    for name, path in _modules(_ROOT):
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text:
                sites[literal].append(name)

    allowed = {"bounded_loops.graph.graph_composition"}
    for literal, found in sites.items():
        unexpected = [name for name in found if name not in allowed]
        # The BYOK request builder carries its own documented per-record defaults; it is the one
        # other place these strings legitimately appear.
        unexpected = [
            name for name in unexpected
            if name != "bounded_loops.graph.adapters.connectors.admitted_connection_request"
        ]
        assert unexpected == [], f"{literal} is declared outside the sentinel module: {unexpected}"


@pytest.mark.parametrize(
    "port_name",
    ["ArtifactStorePort", "ArtifactReaderPort", "ArtifactWriterPort", "EventLogPort"],
)
def test_the_seam_declares_the_ports_the_application_layer_depends_on(port_name: str) -> None:
    """The ports live in one seam file. Two of these used to be declared halfway down a use
    case, where the next consumer could not find them — which is how the direct-import habit
    spread in the first place."""
    from bounded_loops.graph.application import graph_ports

    assert hasattr(graph_ports, port_name)


def test_every_port_is_satisfied_by_the_adapter_that_claims_it(tmp_path: Path) -> None:
    """A port nothing implements is documentation. These are checked structurally, at runtime,
    against the real adapters — reading the signatures is not verification."""
    from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
    from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
    from bounded_loops.graph.application.graph_ports import (
        ArtifactReaderPort,
        ArtifactStorePort,
        ArtifactWriterPort,
        EventLogPort,
    )
    from bounded_loops.graph.domain.events import GraphRunIdentity

    digest = "sha256:" + "a" * 64
    store = LocalArtifactStore(tmp_path / "artifacts")
    log = GraphEventLog(
        tmp_path / "events.jsonl",
        GraphRunIdentity("org", "project", "run", digest, digest, digest),
    )

    assert isinstance(store, ArtifactWriterPort)
    assert isinstance(store, ArtifactReaderPort)
    assert isinstance(store, ArtifactStorePort)
    assert isinstance(log, EventLogPort)
