"""Independent negative corpus for the F0.1 strict manifest contract.

These fixtures describe inputs that a strict manifest loader must reject.  The
tests deliberately write YAML text for duplicate-key cases because constructing
the data through a Python mapping would erase the defect before parsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bounded_loops.application.manifest import load
from bounded_loops.domain.errors import ManifestError


MINIMAL_VALID = {
    "name": "negative-corpus-loop",
    "description": "A test loop",
    "pattern": "evaluator-optimizer",
    "role": ["backend"],
    "rung": "L1",
    "runner": {"default": "stub"},
    "gate": {"kind": "pytest"},
}


def write_loop(
    tmp_path: Path,
    *,
    loop_yaml: dict | None = None,
    bounds_yaml: dict | None = None,
    loop_yaml_text: str | None = None,
    bounds_yaml_text: str | None = None,
) -> Path:
    """Create a valid loop unless a test explicitly supplies raw YAML text."""
    loop_dir = tmp_path / "negative-loop"
    loop_dir.mkdir()
    (loop_dir / "loop.yaml").write_text(
        loop_yaml_text
        if loop_yaml_text is not None
        else yaml.safe_dump(loop_yaml or MINIMAL_VALID, sort_keys=False),
        encoding="utf-8",
    )
    (loop_dir / "bounds.yaml").write_text(
        bounds_yaml_text
        if bounds_yaml_text is not None
        else yaml.safe_dump(
            bounds_yaml if bounds_yaml is not None else {"max_iterations": 5},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (loop_dir / "PROMPT.md").write_text("Fix the bug.\n", encoding="utf-8")
    return loop_dir


def manifest_yaml(**overrides: object) -> str:
    values = {**MINIMAL_VALID, **overrides}
    return yaml.safe_dump(values, sort_keys=False)


def test_duplicate_loop_yaml_key_is_rejected(tmp_path: Path) -> None:
    loop_dir = write_loop(
        tmp_path,
        loop_yaml_text=manifest_yaml(name="first") + "name: second\n",
    )

    with pytest.raises(ManifestError, match="duplicate|Duplicate"):
        load(loop_dir)


def test_duplicate_bounds_yaml_key_is_rejected(tmp_path: Path) -> None:
    loop_dir = write_loop(
        tmp_path,
        bounds_yaml_text="max_iterations: 5\nmax_iterations: 6\n",
    )

    with pytest.raises(ManifestError, match="duplicate|Duplicate"):
        load(loop_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_iterations", True),
        ("max_iterations", "5"),
        ("max_iterations", -1),
        ("no_progress_window", True),
        ("no_progress_window", "3"),
        ("no_progress_window", 0),
        ("no_progress_window", -1),
        ("max_tokens", True),
        ("max_tokens", "100"),
        ("max_tokens", 0),
        ("max_tokens", -1),
        ("max_wallclock_s", True),
        ("max_wallclock_s", "300"),
        ("max_wallclock_s", 0),
        ("max_wallclock_s", -1),
    ],
    ids=lambda value: str(value),
)
def test_numeric_bounds_reject_boolean_non_integer_and_non_positive_values(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ManifestError):
        load(write_loop(tmp_path, bounds_yaml={"max_iterations": 5, field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sandbox", "true"),
        ("sandbox", 1),
        ("quarantine_inputs", "false"),
        ("quarantine_inputs", 0),
        ("trace", "true"),
        ("trace", 1),
        ("require_approval", "false"),
        ("require_approval", 0),
    ],
    ids=lambda value: str(value),
)
def test_optional_boolean_bounds_reject_non_boolean_values(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ManifestError):
        load(write_loop(tmp_path, bounds_yaml={"max_iterations": 5, field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 123),
        ("schema", True),
        ("schema", []),
        ("spec", 123),
        ("spec", True),
        ("bounds", []),
        ("bounds", True),
        ("memory", 123),
        ("memory", False),
    ],
    ids=lambda value: str(value),
)
def test_optional_string_fields_reject_non_string_values(
    tmp_path: Path, field: str, value: object
) -> None:
    overrides = {field: value}
    with pytest.raises(ManifestError):
        load(
            write_loop(
                tmp_path,
                loop_yaml={**MINIMAL_VALID, **overrides}
                if field in {"spec", "bounds", "memory"}
                else None,
                bounds_yaml={"max_iterations": 5, **overrides}
                if field == "schema"
                else None,
            )
        )


@pytest.mark.parametrize(
    "unknown_field",
    [
        "unexpected_top_level",
        "future_provider_config",
    ],
)
def test_unknown_top_level_fields_are_rejected(tmp_path: Path, unknown_field: str) -> None:
    with pytest.raises(ManifestError, match="unknown|unexpected|field"):
        load(write_loop(tmp_path, loop_yaml={**MINIMAL_VALID, unknown_field: True}))


def test_unknown_bounds_field_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="unknown|unexpected|field"):
        load(
            write_loop(
                tmp_path,
                bounds_yaml={"max_iterations": 5, "future_budget_mode": "strict"},
            )
        )


def test_unknown_runner_field_is_rejected(tmp_path: Path) -> None:
    manifest = {**MINIMAL_VALID, "runner": {"default": "stub", "future_option": True}}
    with pytest.raises(ManifestError, match="unknown|unexpected|field"):
        load(write_loop(tmp_path, loop_yaml=manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec", "/etc/passwd"),
        ("bounds", "/etc/passwd"),
        ("memory", "/etc/passwd"),
        ("spec", "../negative-loop-sibling/PROMPT.md"),
        ("bounds", "../negative-loop-sibling/bounds.yaml"),
        ("memory", "../negative-loop-sibling/STATE.md"),
    ],
    ids=lambda value: str(value),
)
def test_absolute_and_sibling_paths_are_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(ManifestError, match="outside the loop"):
        load(write_loop(tmp_path, loop_yaml={**MINIMAL_VALID, field: value}))


def test_symlink_path_to_external_prompt_is_rejected(tmp_path: Path) -> None:
    loop_dir = write_loop(tmp_path)
    external_prompt = tmp_path / "external-prompt.md"
    external_prompt.write_text("secret prompt", encoding="utf-8")
    (loop_dir / "linked-prompt.md").symlink_to(external_prompt)
    manifest = {**MINIMAL_VALID, "spec": "linked-prompt.md"}
    (loop_dir / "loop.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ManifestError, match="outside the loop"):
        load(loop_dir)


@pytest.mark.parametrize("cassette", ["/tmp/cassette.json", "../cassette.json"])
def test_runner_cassette_must_remain_contained(tmp_path: Path, cassette: str) -> None:
    manifest = {**MINIMAL_VALID, "runner": {"default": "stub", "cassette": cassette}}
    with pytest.raises(ManifestError, match="outside the loop"):
        load(write_loop(tmp_path, loop_yaml=manifest))
