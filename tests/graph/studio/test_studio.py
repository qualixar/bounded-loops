"""U1.2 — Graph Studio: templates compile, render is XSS-safe, CLI seeds honestly."""

from __future__ import annotations

import argparse
import json

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_json
from bounded_loops.graph.studio.cli_studio import cmd_graph_studio
from bounded_loops.graph.studio.render import render_studio_html
from bounded_loops.graph.studio.templates import STARTER_TEMPLATES

_SNAP = CompileSnapshot(policy_digest="sha256:" + "a" * 64, package_digests=frozenset(), connections=())


@pytest.mark.parametrize("template", STARTER_TEMPLATES, ids=[t["id"] for t in STARTER_TEMPLATES])
def test_every_starter_template_parses_and_compiles(template):
    spec = parse_authoring_graph_json(json.dumps(template["spec"]))
    plan = compile_graph(spec, _SNAP)
    assert plan.nodes
    # runnable_now claim is honest: only the sandbox demo is executable today.
    assert template["runnable_now"] is (template["id"] == "sandbox-demo")


def test_render_injects_templates_and_null_seed():
    html = render_studio_html(None)
    assert '<script id="studio-templates" type="application/json">' in html
    assert "sandbox-demo" in html and "blog-pipeline" in html and "code-review-pipeline" in html
    # empty seed serializes to null
    seed_block = html.split('<script id="studio-seed" type="application/json">')[1].split("</script>")[0]
    assert seed_block.strip() == "null"


def test_render_escapes_hostile_seed_content():
    hostile = {
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "x",
        "version": "1.0.0",
        "nodes": [{"id": "a</script><script>window.__pwned=1//", "kind": "tool", "tool_ref": "x",
                   "inputs": {}, "outputs": {"r": "text"}, "budget": {"max_attempts": 1, "max_wallclock_s": 1},
                   "effects": ["read_only"], "isolation": "workspace_only"}],
        "edges": [], "connection_slots": [], "policies": {"data_class": "public", "fail_mode": "fail_closed"},
    }
    html = render_studio_html(hostile)
    # The injected payload must not contain a raw closing tag or a live <script>.
    assert '"id":"a</script>' not in html
    assert "window.__pwned=1" in html  # present, but only as escaped data
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in html


def test_studio_template_has_no_html_injection_sinks():
    """No dynamic-HTML sink is *used* (a denylisted property name may appear as
    the guard's key, but never as a `.member` write/read or a call)."""
    from bounded_loops.graph.studio.render import load_template
    html = load_template()
    for sink in (".innerHTML", ".outerHTML", ".insertAdjacentHTML(", "document.write", "eval(", "new Function("):
        assert sink not in html, "studio template must not use HTML-injection sink: " + sink


def test_cli_writes_blank_studio(tmp_path, capsys):
    out = tmp_path / "studio.html"
    rc = cmd_graph_studio(argparse.Namespace(from_manifest=None, out=str(out)))
    assert rc == 0 and out.is_file()
    assert "Graph Studio" in out.read_text(encoding="utf-8")
    assert "written to" in capsys.readouterr().out


def test_cli_seeds_from_valid_manifest(tmp_path):
    manifest = tmp_path / "blog.json"
    blog = next(t["spec"] for t in STARTER_TEMPLATES if t["id"] == "blog-pipeline")
    manifest.write_text(json.dumps(blog), encoding="utf-8")
    out = tmp_path / "seeded.html"
    rc = cmd_graph_studio(argparse.Namespace(from_manifest=str(manifest), out=str(out)))
    assert rc == 0
    assert "blog-pipeline" in out.read_text(encoding="utf-8")


def test_cli_refuses_invalid_manifest(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"api_version":"bounded-loops.dev/graph/v1","graph_id":"x"}', encoding="utf-8")
    rc = cmd_graph_studio(argparse.Namespace(from_manifest=str(bad), out=str(tmp_path / "o.html")))
    assert rc == 2
    assert "invalid" in capsys.readouterr().err


def test_cli_temp_write_cannot_overwrite_a_victim(tmp_path):
    """The atomic write uses an unpredictable mkstemp name created O_EXCL/0600,
    so a symlink planted at a guessable temp path can never redirect the write to
    overwrite an arbitrary file (regression for the round-2/3 Grok+Muse HIGH)."""
    import os
    out = tmp_path / "studio.html"
    victim = tmp_path / "victim.txt"
    victim.write_text("SACRED", encoding="utf-8")
    os.symlink(victim, tmp_path / f"{out.name}.{os.getpid()}.tmp")  # planted, guessable
    rc = cmd_graph_studio(argparse.Namespace(from_manifest=None, out=str(out)))
    assert rc == 0 and out.is_file()
    assert victim.read_text(encoding="utf-8") == "SACRED"  # never overwritten


def test_cli_refuses_unsupported_extension(tmp_path, capsys):
    src = tmp_path / "g.txt"
    src.write_text("nope", encoding="utf-8")
    rc = cmd_graph_studio(argparse.Namespace(from_manifest=str(src), out=str(tmp_path / "o.html")))
    assert rc == 2
    assert "unsupported" in capsys.readouterr().err
