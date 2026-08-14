"""The vendored front-end must be exactly what we said it is.

Jarvis has no build step: three published artifacts are committed and served from the wheel. That
is what makes the UI work with no node, no npm and no network — and it means a silent swap of the
React we ship would go unnoticed. So the same discipline the engine applies to a loop package is
applied to its own UI: pin by content digest and check it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

VENDOR = (
    Path(__file__).resolve().parents[3]
    / "bounded_loops" / "graph" / "jarvis" / "assets" / "vendor"
)

#: Digests recorded in VENDOR.md when the files were fetched from the npm registry.
_PINS = {
    "react.production.min.js":
        "d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd",
    "react-dom.production.min.js":
        "35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d",
    "htm.module.js":
        "ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4",
}


@pytest.mark.parametrize("name", sorted(_PINS))
def test_the_vendored_file_matches_its_recorded_digest(name: str) -> None:
    path = VENDOR / name
    assert path.is_file(), f"{name} is not shipped"
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == _PINS[name], (
        f"{name} changed: recorded {_PINS[name][:16]}…, found {actual[:16]}…. "
        "If this is a deliberate upgrade, refetch with the npm pack command in VENDOR.md and "
        "update BOTH this test and the VENDOR.md table."
    )


def test_VENDOR_md_records_the_same_digests_this_test_asserts() -> None:
    """Two copies of a digest are two chances to update only one of them."""
    text = (VENDOR / "VENDOR.md").read_text(encoding="utf-8")
    documented = dict(re.findall(r"`([\w.\-]+\.js)`.*?`sha256:([0-9a-f]{64})`", text))
    assert documented == _PINS


def test_nothing_in_the_vendor_directory_is_UNPINNED() -> None:
    """A fourth file appearing here would be an unreviewed dependency in the shipped UI."""
    shipped = {path.name for path in VENDOR.glob("*.js")}
    assert shipped == set(_PINS), f"unpinned vendor files: {sorted(shipped - set(_PINS))}"


def test_the_vendored_react_is_the_PRODUCTION_build() -> None:
    """A development build ships warnings, is several times larger, and is slower."""
    react = (VENDOR / "react.production.min.js").read_text(encoding="utf-8", errors="replace")
    assert "production.min" in react or "__DEV__" not in react
    assert len(react) < 60_000, "that is not a minified production build"


def test_the_assets_are_declared_as_WHEEL_artifacts() -> None:
    """A UI that is not packaged is a UI a `pip install` user does not have.

    The three HTML templates already ride in the wheel this way; forgetting Jarvis would mean
    `bl jarvis` worked from a source checkout and 500'd for everyone who installed it.
    """
    import tomllib

    root = Path(__file__).resolve().parents[3]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    artifacts = config["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]

    assert any("jarvis/assets/*" in entry for entry in artifacts), (
        "the Jarvis assets are not declared as wheel artifacts"
    )
    assert any("jarvis/assets/vendor/*" in entry for entry in artifacts), (
        "the vendored React is not declared as a wheel artifact"
    )
