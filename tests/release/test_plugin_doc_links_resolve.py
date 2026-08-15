"""Every relative link inside a plugin pack must resolve INSIDE that pack.

A host pack is installed on its own — the user gets `plugins/claude-code/`, not the repository. So
a link that only resolves from the repo root is a dead link for every real reader, and the failure
is silent: the agent following it finds nothing and carries on without the refusal codes it was
sent to read.

That is exactly what shipped. Eleven of twelve references to `refusal-reference.md` were broken:

* the prose ones named `plugins/shared/docs/refusal-reference.md`, a repo-relative path that means
  nothing once a single pack is installed;
* the markdown ones used `../../docs/refusal-reference.md`, which resolves correctly inside
  `plugins/shared/` — where the file lives — and nowhere else, because the host packs are copies of
  shared that never received the `docs/` directory.

Found by the wave-1 audit, which reported it as one pack. It was three.

This test is deliberately general rather than a check for that one filename: the packs are
maintained by copying, so the next doc added to shared will go missing the same way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"

#: `[text](target)` — the markdown links an agent or a human would actually follow.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

#: Backticked relative PATHS ending in .md — prose, but prose an agent is told to go and read.
#: Requires a `/`, so a bare `` `AGENTS.md` `` does not match. That distinction is load-bearing:
#: several packs tell the user to append a snippet to *their own* `AGENTS.md`, which is a file in
#: the user's project and must not be resolved against the pack. Matching it flagged a working
#: instruction as a dead link.
_PROSE_PATH = re.compile(r"`((?:\.\./)*[\w.-]+/[\w./-]*\.md)`")


def _packs() -> list[Path]:
    return sorted(p for p in _PLUGINS.iterdir() if p.is_dir())


def _markdown_files() -> list[Path]:
    return sorted(_PLUGINS.rglob("*.md"))


def _pack_of(document: Path) -> Path | None:
    """The pack a document belongs to, or None for a file sitting directly in `plugins/`.

    `plugins/README.md` describes the packs rather than living in one, so it has no containment
    boundary to enforce — its links still have to resolve, but they may point across packs.
    Treating it as a pack named "README.md" produced a nonsense failure message.
    """
    parts = document.relative_to(_PLUGINS).parts
    return _PLUGINS / parts[0] if len(parts) > 1 else None


def _relative_targets(text: str) -> set[str]:
    """Link targets that point at a local file. Skips URLs, anchors and mail links."""
    found = set(_MARKDOWN_LINK.findall(text)) | set(_PROSE_PATH.findall(text))
    return {
        target.split("#", 1)[0].strip()
        for target in found
        if not target.startswith(("http://", "https://", "#", "mailto:"))
        and target.split("#", 1)[0].strip()
    }


@pytest.mark.parametrize("document", _markdown_files(), ids=lambda p: str(p.relative_to(_PLUGINS)))
def test_every_relative_link_in_a_plugin_document_resolves(document: Path) -> None:
    """Resolved from the document, and required to land inside its own pack."""
    pack = _pack_of(document)
    broken: list[str] = []

    for target in _relative_targets(document.read_text(encoding="utf-8")):
        resolved = (document.parent / target).resolve()
        if not resolved.exists():
            broken.append(f"{target} -> {resolved} (missing)")
        elif pack is not None and pack.resolve() not in resolved.parents:
            broken.append(f"{target} -> escapes {pack.name}/, which is installed on its own")

    where = f"installing {pack.name}/ alone" if pack is not None else "reading plugins/"
    assert not broken, (
        f"{document.relative_to(_PLUGINS)} has links that a user {where} cannot follow:\n  "
        + "\n  ".join(broken)
    )


@pytest.mark.parametrize("pack", _packs(), ids=lambda p: p.name)
def test_a_pack_that_references_the_refusal_codes_also_ships_them(pack: Path) -> None:
    """The specific instance, kept as its own check because it is the one that shipped broken.

    The general test above would catch it, but only once someone reads the failure closely. This
    one names the artefact, so the fix is obvious rather than inferred.
    """
    references = [
        document for document in pack.rglob("*.md")
        if "refusal-reference.md" in document.read_text(encoding="utf-8")
    ]
    if not references:
        pytest.skip(f"{pack.name} does not reference the refusal codes")

    assert (pack / "docs" / "refusal-reference.md").is_file(), (
        f"{pack.name} tells the agent to read the refusal codes but does not ship them. "
        f"Referenced by: {[str(r.relative_to(pack)) for r in references]}"
    )


def test_the_packs_agree_on_the_refusal_reference_contents() -> None:
    """Copies drift. A pack carrying stale refusal codes is worse than one carrying none.

    Byte-comparison rather than a "looks similar" check, because the whole point of a refusal code
    is that it is looked up verbatim.
    """
    canonical = (_PLUGINS / "shared" / "docs" / "refusal-reference.md").read_bytes()

    for pack in _packs():
        copy = pack / "docs" / "refusal-reference.md"
        if not copy.is_file():
            continue
        assert copy.read_bytes() == canonical, (
            f"{pack.name}/docs/refusal-reference.md has drifted from plugins/shared/. "
            "Re-copy it rather than editing a pack copy in place."
        )
