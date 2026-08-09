"""Render a self-contained Graph Arena HTML page from an ArenaProjection.

The Arena is a read-only receipt projection. This module only injects the
projection JSON into the static template; it performs no I/O against a run and
grants the page no capability. The injected JSON is escaped so that hostile
receipt content (for example a node id containing ``</script>``) cannot break
out of the data block.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

_TEMPLATE_PATH = Path(__file__).with_name("arena_template.html")
_OPEN = '<script id="arena-data" type="application/json">'
_CLOSE = "</script>"


def _to_jsonable(projection: Any) -> Any:
    if dataclasses.is_dataclass(projection) and not isinstance(projection, type):
        return dataclasses.asdict(projection)
    return projection


def _escape_for_script(payload: str) -> str:
    # Inside a <script> block only these three matter; escaping them as JSON
    # unicode escapes keeps the text valid JSON that JSON.parse restores exactly.
    return payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render_arena_html(projection: Any, *, template: str | None = None) -> str:
    """Return a self-contained HTML document for one ArenaProjection.

    ``projection`` may be an ``ArenaProjection`` dataclass or a plain mapping.
    """
    data = _to_jsonable(projection)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = _escape_for_script(payload)

    document = template if template is not None else load_template()
    start = document.find(_OPEN)
    if start < 0:
        raise ValueError("arena template is missing the arena-data open marker")
    start += len(_OPEN)
    end = document.find(_CLOSE, start)
    if end < 0:
        raise ValueError("arena template is missing the arena-data close marker")
    return document[:start] + "\n" + payload + "\n" + document[end:]


def load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
