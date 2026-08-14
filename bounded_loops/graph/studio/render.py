"""Render the self-contained Graph Studio HTML from starter templates + a seed.

Like the Arena, this only injects JSON into a static template and grants the
page no capability (no network, no eval of injected data). All injected JSON is
escaped so hostile content in a seed graph (e.g. a node id containing
``</script>``) cannot break out of its data block; the app renders every value
with ``textContent``, never ``innerHTML``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from bounded_loops.graph.studio.templates import STARTER_TEMPLATES

_TEMPLATE_PATH = Path(__file__).with_name("studio_template.html")
_TEMPLATES_OPEN = '<script id="studio-templates" type="application/json">'
_SEED_OPEN = '<script id="studio-seed" type="application/json">'
_CATALOGUE_OPEN = '<script id="studio-catalogue" type="application/json">'
_CLOSE = "</script>"


def _escape_for_script(payload: str) -> str:
    return payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _inject(document: str, open_marker: str, payload: str) -> str:
    start = document.find(open_marker)
    if start < 0:
        raise ValueError(f"studio template is missing marker {open_marker!r}")
    start += len(open_marker)
    end = document.find(_CLOSE, start)
    if end < 0:
        raise ValueError("studio template is missing a data close marker")
    return document[:start] + "\n" + payload + "\n" + document[end:]


def render_studio_html(
    seed: Any | None = None,
    *,
    templates: Sequence[dict[str, Any]] = STARTER_TEMPLATES,
    loop_catalogue: Sequence[dict[str, Any]] | None = None,
    template_html: str | None = None,
) -> str:
    """Return a self-contained Graph Studio document.

    ``seed`` is an optional authoring-graph mapping to open for editing
    (e.g. reconstructed from an existing manifest); ``None`` starts empty.
    ``loop_catalogue`` is a list of package descriptors (name, digest, description,
    keyless) for the Studio's loop package picker; omit or pass ``None`` for no
    catalogue (the text-input field remains available for manual digest entry).
    """
    document = template_html if template_html is not None else load_template()
    templates_payload = _escape_for_script(
        json.dumps(list(templates), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    seed_payload = _escape_for_script(
        json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    catalogue_payload = _escape_for_script(
        json.dumps(
            list(loop_catalogue) if loop_catalogue is not None else [],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    )
    document = _inject(document, _TEMPLATES_OPEN, templates_payload)
    document = _inject(document, _SEED_OPEN, seed_payload)
    document = _inject(document, _CATALOGUE_OPEN, catalogue_payload)
    return document


def load_template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")
