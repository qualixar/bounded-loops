"""`bl graph studio` — emit the self-contained Graph Studio HTML.

With no arguments it writes a blank Studio seeded only with starter templates.
With ``--from <manifest>`` it validates that manifest and opens it for editing
(honest: an invalid manifest is refused, never silently seeded).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from bounded_loops.graph.application.validate_graph import (
    parse_authoring_graph_json,
    parse_authoring_graph_yaml,
)
from bounded_loops.graph.domain.errors import GraphValidationError
from bounded_loops.graph.studio.render import render_studio_html

_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def _parse_loop_roots(raw: list[str] | None) -> tuple[Path, ...] | None:
    """Convert an argparse append-list of root paths to a tuple, or None (use defaults)."""
    return tuple(Path(p) for p in raw) if raw else None


def _build_loop_catalogue(loop_roots: tuple[Path, ...] | None) -> list[dict[str, object]]:
    """Return a list of {digest, name, description, keyless} descriptors for the picker."""
    try:
        import yaml as _yaml  # pyyaml — already a project dependency
        from bounded_loops.graph.adapters.workers.loop_packages import LoopPackageRegistry
        from bounded_loops.graph.loop_node_wiring import _default_loop_roots
    except ImportError:
        return []
    roots = loop_roots if loop_roots is not None else _default_loop_roots()
    try:
        index = LoopPackageRegistry(roots=roots).index()  # bare hex -> Path
    except Exception:  # noqa: BLE001 — fail-open: a bad root must not block the Studio
        return []
    entries: list[dict[str, object]] = []
    for hex_digest, pkg_dir in index.items():
        name: str = pkg_dir.name
        description: str = ""
        keyless: bool = False
        loop_yaml = pkg_dir / "loop.yaml"
        if loop_yaml.is_file():
            try:
                cfg = _yaml.safe_load(loop_yaml.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    name = str(cfg.get("name") or name)
                    description = str(cfg.get("description") or "")
                    keyless = bool(cfg.get("keyless", False))
            except Exception:  # noqa: BLE001
                pass
        entries.append({"digest": f"sha256:{hex_digest}", "name": name,
                         "description": description, "keyless": keyless})
    entries.sort(key=lambda e: str(e.get("name", "")))
    return entries


def cmd_graph_studio(args: argparse.Namespace) -> int:
    """bl graph studio [--from <manifest>] [--out <file.html>]."""
    seed: object | None = None
    src = getattr(args, "from_manifest", None)
    if src:
        manifest = Path(src)
        try:
            if manifest.is_file() and manifest.stat().st_size > _MAX_MANIFEST_BYTES:
                print(f"error: graph studio: manifest exceeds {_MAX_MANIFEST_BYTES} bytes", file=sys.stderr)
                return 2
            text = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: graph studio: cannot read '{manifest}' — {exc}", file=sys.stderr)
            return 2
        suffix = manifest.suffix.lower()
        try:
            if suffix == ".json":
                parse_authoring_graph_json(text)  # validate before seeding
                seed = json.loads(text)
            elif suffix in (".yaml", ".yml"):
                parse_authoring_graph_yaml(text)  # validate before seeding
                seed = yaml.safe_load(text)
            else:
                print(f"error: graph studio: unsupported extension '{suffix}' (use .json/.yaml)", file=sys.stderr)
                return 2
        except GraphValidationError as exc:
            print(
                f"error: graph studio: manifest is invalid [{exc.code}] {exc.pointer} — {exc.message}",
                file=sys.stderr,
            )
            return 2

    out = Path(getattr(args, "out", None) or "graph-studio.html")
    if out.is_symlink():
        print(f"error: graph studio: '{out}' is a symlink; aborting", file=sys.stderr)
        return 2
    loop_roots = _parse_loop_roots(getattr(args, "loop_roots", None))
    catalogue = _build_loop_catalogue(loop_roots)
    # Write atomically via a securely-created temp + replace, so a partial write
    # never leaves a truncated Studio and no planted/guessable path can redirect
    # the write. mkstemp creates an UNPREDICTABLE name with O_CREAT|O_EXCL and
    # mode 0600 atomically (never following a symlink); any failure surfaces as a
    # clean exit 2, not a traceback.
    html = render_studio_html(seed, loop_catalogue=catalogue)
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    except OSError as exc:
        print(f"error: graph studio: cannot create a temp file in '{out.parent}' — {exc}", file=sys.stderr)
        return 2
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(html)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"Graph Studio written to {out}")
    print("Open it in any browser — offline and read-only. Export JSON, then: bl graph lint <graph>.json")
    return 0
