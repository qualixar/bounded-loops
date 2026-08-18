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
    """Every module this file imports, by any of the four routes into a module.

    Function-local imports count. A deferred import is still a dependency — hiding an
    adapter import inside a function would defeat the whole check, and that is exactly the
    shape a future drift is most likely to take.

    The last three routes were added after the P3 audit pointed out that the first version saw
    only absolute ``import``/``from``, so a **relative** import (``from ..adapters.x import y``)
    or a dynamic one (``importlib.import_module("…adapters…")``) would have been invisible to CI.
    A tripwire with a known bypass is worse than no tripwire, because it is trusted.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    found: list[str] = []
    package = _module_name(path).rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level > 0:
                # Relative: resolve against this module's package so the rules below see the same
                # absolute name an equivalent absolute import would have produced.
                prefix = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{prefix}.{node.module}" if node.module else prefix
            if not base:
                continue
            found.append(base)
            # ALSO record ``base.name`` for each imported symbol. ``from bounded_loops.graph import
            # graph_composition`` records only ``bounded_loops.graph`` otherwise — a package name no
            # rule matches — while binding the composition module itself. The audit found that route
            # still open after the first fix.
            found.extend(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            # ``importlib.import_module("x.y")`` / ``__import__("x.y")`` — the target is a literal
            # in every real use; a computed one would be unreachable for any static check, and is
            # itself a review flag. Both the attribute form and the bare NAME form
            # (``from importlib import import_module``) count.
            target = node.func
            dynamic = (
                (isinstance(target, ast.Name) and target.id in ("__import__", "import_module"))
                or (isinstance(target, ast.Attribute) and target.attr == "import_module")
            )
            if dynamic and node.args and isinstance(node.args[0], ast.Constant):
                literal = node.args[0].value
                if isinstance(literal, str):
                    found.append(literal)
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


def test_the_graph_application_layer_does_not_import_the_composition_tier() -> None:
    """The re-export bypass. ``graph_composition`` legitimately imports ``LocalArtifactStore``, so
    ``from bounded_loops.graph.graph_composition import LocalArtifactStore`` inside an application
    module reaches the same concrete adapter while naming only a permitted module — invisible to the
    rule above, which matches on the imported MODULE.

    Forbidding the edge outright is simpler than trying to tell a re-exported adapter from a
    legitimate one, and it is the correct rule anyway: composition points inward, never the reverse.
    """
    offences = _offences(_GRAPH / "application", tuple(sorted(_COMPOSITION_TIER)))
    assert offences == [], (
        "application must not import the composition tier (adapters leak through its re-exports):\n  "
        + "\n  ".join(offences)
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
#: Raised from 800 to 900 on 2026-08-19 by Varun's explicit decision ("I do not care about 800
#: lines. It is not a hard cap.") after the gate-plugin work took composition.py past it twice.
#:
#: Recorded as a decision rather than applied silently, because the alternative on the table was
#: trimming comments until the number fit — which games the tripwire instead of respecting it,
#: and is the exact accumulation this test was written to catch. The cap still holds for every
#: other module; only the ceiling moved.
#:
#: The clean fix if this is revisited is already scoped: `_make_scratch_workspace` and
#: `_make_persistent_run_workspace` are 106 lines of pure filesystem work, verified to touch
#: nothing in composition but `_SCRATCH_MARKER` and no sibling function, so they move out
#: cleanly — re-exported from composition so existing `composition._make_scratch_workspace`
#: references in tests and docs keep resolving.
_MAX_LINES = 900


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
    for whichever one someone edits.

    **Named "exactly once" and only ever checked "not more than once".** Deleting a sentinel
    outright satisfied every assertion below — zero declarations is not two declarations — so the
    ONCE half is now asserted as well. A source-scanning test also passes when the scan finds no
    source, so the module list is proven non-empty before anything is concluded from it.
    """
    literals = ('"local-org"', '"local-project"', '"graph-run"')
    sites: dict[str, list[str]] = {literal: [] for literal in literals}
    modules = list(_modules(_ROOT))

    assert len(modules) >= 20, (
        f"only {len(modules)} module(s) discovered; this test reads source text, so an empty scan "
        "would report every sentinel as correctly placed without opening a file"
    )

    for name, path in modules:
        text = path.read_text(encoding="utf-8")
        for literal in literals:
            if literal in text:
                sites[literal].append(name)

    allowed = {"bounded_loops.graph.graph_composition"}

    missing = [literal for literal, found in sites.items() if not any(n in allowed for n in found)]
    assert missing == [], (
        f"{missing} is declared NOWHERE in {sorted(allowed)}. The sentinel was removed or renamed, "
        "and every 'not declared elsewhere' assertion below is satisfied vacuously by its absence."
    )

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


def test_every_controller_assembly_path_checks_the_provider_map() -> None:
    """``_preflight`` guards only ``execute_graph_run``; the facade, MCP and console bypass it.

    A run created with ``--providers catalog.toml`` and resumed WITHOUT it has a plan naming a
    provider that no longer exists. The resolver would still end the node terminally rather than
    retrying it, but only after starting it — and after that resume pass had already paid for every
    node upstream. So the check lives at the one chokepoint all of those paths share.
    """
    import inspect

    from bounded_loops.graph import graph_composition

    source = inspect.getsource(graph_composition.build_execution_controller)
    assert "_refuse_unrunnable_providers" in source, (
        "build_execution_controller is the only assembly point the facade, MCP and console share; "
        "the provider check has to be here, not only in _preflight"
    )
    # ...and it must be scoped to nodes that could still run: checking terminal nodes made a run
    # whose nodes had all SUCCEEDED unresumable, which used to return its projection idempotently.
    helper = inspect.getsource(graph_composition._refuse_unrunnable_providers)
    assert "SUCCEEDED" in helper and "completed" in helper


def test_the_provider_rule_has_no_skip_by_omission_default() -> None:
    """``cli_profiles`` is required on the shared rule. A default of "no map means skip" is the
    shape that let the facade path go unchecked in the first place."""
    import inspect

    from bounded_loops.graph.graph_composition import unknown_local_cli_provider

    profiles_param = inspect.signature(unknown_local_cli_provider).parameters["cli_profiles"]
    assert profiles_param.default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "snippet",
    [
        "from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog",
        "import bounded_loops.graph.adapters.persistence.event_log",
        "from ..adapters.persistence.event_log import GraphEventLog",
        "from ...graph.adapters.persistence.event_log import GraphEventLog",
        'importlib.import_module("bounded_loops.graph.adapters.persistence.event_log")',
        '__import__("bounded_loops.graph.adapters.persistence.event_log")',
        # Routes the audit found still open after the first fix: a PACKAGE import that binds the
        # submodule, and the bare-name form of import_module.
        "from bounded_loops.graph.adapters.persistence import event_log",
        "from bounded_loops.graph import adapters",
        'import_module("bounded_loops.graph.adapters.persistence.event_log")',
    ],
)
def test_the_tripwire_sees_every_route_into_an_adapter(tmp_path: Path, snippet: str) -> None:
    """Each of these is a way to reach an adapter, and the first version of ``_imports`` caught
    only the first two. A tripwire with a known bypass is worse than none, because it is trusted.

    Written against a synthetic file rather than by mutating the real tree, so the test proves the
    detector without a step that could be left applied.
    """
    module = tmp_path / "bounded_loops" / "graph" / "application" / "probe.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        f"import importlib\nfrom importlib import import_module\n{snippet}\n",
        encoding="utf-8",
    )

    global _ROOT
    original = _ROOT
    try:
        _ROOT = tmp_path / "bounded_loops"
        seen = _imports(module)
    finally:
        _ROOT = original

    assert any("adapters" in name for name in seen), f"{snippet!r} was invisible: saw {seen}"
