"""Framework examples should fail with an actionable installation message."""

from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("loop_name", "import_root", "package_name"),
    [
        ("langgraph-example", "langgraph", "langgraph"),
        ("crewai-example", "crewai", "crewai"),
        ("autogen-example", "agent_framework", "agent-framework"),
        ("adk-example", "google.adk", "google-adk"),
    ],
)
def test_missing_framework_error_names_the_exact_install_command(
    loop_name: str,
    import_root: str,
    package_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    glue_path = REPO_ROOT / "loops" / loop_name / "glue.py"
    spec = importlib.util.spec_from_file_location(f"test_{loop_name}_glue", glue_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == import_root or name.startswith(import_root + "."):
            raise ModuleNotFoundError(f"No module named {import_root!r}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(RuntimeError) as exc_info:
        module.run_turn("fix the bug", str(tmp_path))

    assert str(exc_info.value) == (
        f"this loop needs {package_name} — pip install {package_name}"
    )
