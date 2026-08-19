"""A non-UTF-8 manifest is a user's file, not a crash.

`OSError` and `UnicodeDecodeError` are SIBLINGS — the latter is a `ValueError` — so an
`OSError`-only handler let a non-UTF-8 file escape as a raw traceback with no exit code. Handing
someone a stack trace for a file they can see is wrong, and it is the same
widen-one-handler-and-miss-its-sibling defect the gate boundary hit twice.
"""
from __future__ import annotations

from pathlib import Path

from bounded_loops.cli import main


def _invalid_utf8(tmp_path: Path) -> Path:
    target = tmp_path / "graph.yaml"
    target.write_bytes(b"graph_id: x\n\xff\xfe not utf-8\n")
    return target


def test_graph_plan_reports_a_non_utf8_manifest_instead_of_crashing(tmp_path, capsys):
    code = main(["graph", "plan", str(_invalid_utf8(tmp_path))])

    assert code == 2, "a file it cannot decode must be a clean refusal, not a traceback"
    printed = capsys.readouterr()
    assert "cannot read" in (printed.err + printed.out)
    assert "Traceback" not in (printed.err + printed.out)


def test_graph_lint_does_the_same_for_the_sibling_command(tmp_path, capsys):
    """The sibling call site. A fix that reaches one command and leaves the next one crashing is the
    defect this project has committed most often."""
    code = main(["graph", "lint", str(_invalid_utf8(tmp_path))])

    assert code == 2
    printed = capsys.readouterr()
    assert "Traceback" not in (printed.err + printed.out)
