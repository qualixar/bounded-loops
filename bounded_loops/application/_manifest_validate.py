"""Small validators shared by `manifest.py` and `manifest_bounds.py`.

One home rather than two copies. This project has already had to remove six mirrored change
detectors and four mirrored prompt builders, both of which had silently diverged; splitting a module
by copying its helpers into the new file is how that starts.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from bounded_loops.domain.errors import ManifestError


class _DuplicateYamlKeyError(yaml.YAMLError):
    def __init__(self, key: object) -> None:
        self.key = key
        super().__init__(f"duplicate key {key!r}")


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every mapping level."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.YAMLError(f"mapping key {key!r} is not hashable") from exc
        if duplicate:
            raise _DuplicateYamlKeyError(key)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _resolve_contained(loop_dir: Path, rel: str, field_name: str) -> Path:
    """
    Security fix: resolve a manifest-relative path and REJECT any
    path that escapes loop_dir via '..' or a symlink. Prevents a malicious
    loop.yaml (e.g. `spec: ../../../../.ssh/id_rsa`) from reading files
    outside the loop folder and injecting them into the agent prompt.
    """
    resolved = (loop_dir / rel).resolve()
    loop_dir_resolved = loop_dir.resolve()
    if not resolved.is_relative_to(loop_dir_resolved):
        raise ManifestError(
            f"loop.yaml: '{field_name}: {rel}' resolves outside the loop "
            f"folder ({resolved} is not inside {loop_dir_resolved}) — rejected."
        )
    return resolved


def _load_yaml_mapping(path: Path, label: str) -> dict:
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except _DuplicateYamlKeyError as exc:
        raise ManifestError(f"{label}: duplicate key {exc.key!r}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{label} at {path} is not valid YAML: {exc}") from exc
    if raw is None:
        raise ManifestError(f"{label} is empty ({path})")
    if not isinstance(raw, dict):
        raise ManifestError(f"{label} must contain a mapping at the document root")
    return raw


def _reject_unknown_keys(values: Mapping[object, object], allowed: frozenset[str], section: str) -> None:
    unknown = sorted((key for key in values if key not in allowed), key=repr)
    if unknown:
        # Name the file so the author knows exactly where to look.
        file_hint = "bounds.yaml" if section == "bounds" else "loop.yaml"
        raise ManifestError(
            f"{file_hint} [{section}]: unknown key {unknown[0]!r}. "
            f"Valid keys for this section: {sorted(allowed)}. "
            f"Remove or rename this key, or check for a typo."
        )


def _positive_int(value: object, field_name: str, *, allow_none: bool = False,
                  allow_zero: bool = False) -> int | None:
    """`allow_zero` exists for `handoff_reserve_s`, where 0 means "decline the wind-down turn".

    Zero is a real choice there, not a missing value, so it must be expressible without abusing
    null — which for the other bounds already means "use the conservative platform default".
    """
    if value is None and allow_none:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        suffix = " or null" if allow_none else ""
        raise ManifestError(f"{field_name} must be an integer{suffix}")
    floor = 0 if allow_zero else 1
    if value < floor:
        if field_name == "max_iterations":
            raise ManifestError("max_iterations must be at least 1 (positive int)")
        raise ManifestError(f"{field_name} must be at least {floor}")
    return value


def _strict_bool(value: object, field_name: str, *, allow_none: bool = False) -> bool | None:
    if value is None and allow_none:
        return None
    if type(value) is not bool:
        suffix = " or null" if allow_none else ""
        raise ManifestError(f"{field_name} must be a boolean{suffix}")
    return value
