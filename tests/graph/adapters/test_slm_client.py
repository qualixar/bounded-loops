"""Tests for SuperlocalMemorySlmClient — the real SLM adapter.

These tests require the ``superlocalmemory`` package to be importable.
If absent the entire module is SKIPPED (not FAILED) — the repo stays
keyless/depless for CI without the optional dep installed.

Every test constructs the client with ``tmp_path / "slm_test.db"`` as
the base directory.  No test may ever touch Varun's real SLM database
at ``~/.superlocalmemory/``.

Run with external_tool marker enabled:
    uv run pytest -m external_tool tests/graph/adapters/test_slm_client.py
"""

import pytest

# Guard — skip the ENTIRE module when superlocalmemory is not importable.
# Must be the first non-import statement (E402 suppressed in pyproject.toml).
pytest.importorskip("superlocalmemory", reason="superlocalmemory not installed")

pytestmark = pytest.mark.external_tool

# All imports below are safe because importorskip already validated availability.
from pathlib import Path

from bounded_loops.graph.adapters.persistence.slm_client import (
    SuperlocalMemorySlmClient,
)
from bounded_loops.graph.adapters.persistence.slm_semantic import (
    SlmClientPort,
    SlmHit,
    SlmSemanticMemory,
)
from bounded_loops.graph.application.memory_store import MemoryNamespace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(tmp_path: Path) -> SuperlocalMemorySlmClient:
    """Construct a client scoped to an isolated tmp directory."""
    return SuperlocalMemorySlmClient(base_dir=tmp_path / "slm_test.db")


def _scope_tag(org: str = "org-x", project: str = "proj-y") -> str:
    """Build the scope tag SlmSemanticMemory would produce for (org, project)."""
    import json
    ns = list(("notes",))
    payload = json.dumps([org, project, ns], separators=(",", ":"))
    return f"bl-scope:{payload}"


# ---------------------------------------------------------------------------
# Constructor safety
# ---------------------------------------------------------------------------


def test_constructor_requires_base_dir() -> None:
    """Omitting base_dir must raise TypeError — no silent defaulting to real DB."""
    with pytest.raises(TypeError):
        SuperlocalMemorySlmClient()  # type: ignore[call-arg]


def test_constructor_with_explicit_path_does_not_touch_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine must use ONLY the given tmp_path, never HOME/.superlocalmemory."""
    # Redirect HOME to a dir that has no .superlocalmemory — any file creation
    # there would be captured and would fail the test.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("SLM_DATA_ROOT", str(tmp_path / "slm_isolated"))

    client = _make_client(tmp_path)
    client.close()

    # The engine's db_path must live under tmp_path, not HOME/.superlocalmemory.
    db_path = client._engine._config.db_path
    assert db_path is not None
    assert str(tmp_path) in str(db_path), (
        f"Engine db_path {db_path!r} must be under tmp_path {tmp_path!r}"
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_client_satisfies_slm_client_port(tmp_path: Path) -> None:
    """SuperlocalMemorySlmClient must pass isinstance check against SlmClientPort."""
    client = _make_client(tmp_path)
    try:
        assert isinstance(client, SlmClientPort)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# remember — returns ID
# ---------------------------------------------------------------------------


def test_remember_returns_nonempty_string(tmp_path: Path) -> None:
    """remember() must return a non-empty string ID."""
    client = _make_client(tmp_path)
    try:
        mem_id = client.remember(
            "Alice went to Paris last summer",
            tags=(_scope_tag(),),
        )
        assert isinstance(mem_id, str)
        assert len(mem_id) > 0
    finally:
        client.close()


def test_remember_returns_distinct_ids(tmp_path: Path) -> None:
    """Each remember() call should return a distinct ID."""
    client = _make_client(tmp_path)
    try:
        id1 = client.remember("Alice went to Paris", tags=(_scope_tag(),))
        id2 = client.remember("Bob stayed in London", tags=(_scope_tag(),))
        assert id1 != id2
    finally:
        client.close()


# ---------------------------------------------------------------------------
# recall — finds stored items (BM25 channel, no embedding needed immediately)
# ---------------------------------------------------------------------------


def test_recall_finds_stored_item(tmp_path: Path) -> None:
    """A stored text must be recallable by relevant query (BM25 at minimum)."""
    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        client.remember("Alice went to Paris last summer", tags=(tag,))
        hits = client.recall("Where did Alice go?", tags=(tag,), limit=5)
        assert isinstance(hits, tuple)
        # BM25 on a fresh store; token overlap should surface the hit.
        assert len(hits) >= 1, "Expected at least one hit for 'Alice' query"
    finally:
        client.close()


def test_recall_returns_slm_hit_objects(tmp_path: Path) -> None:
    """recall() must return a tuple of SlmHit instances."""
    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        client.remember("The quick brown fox jumped over the lazy dog", tags=(tag,))
        hits = client.recall("quick brown fox", tags=(tag,), limit=3)
        for hit in hits:
            assert isinstance(hit, SlmHit)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# SlmHit shape
# ---------------------------------------------------------------------------


def test_slm_hit_has_required_fields(tmp_path: Path) -> None:
    """Each SlmHit must carry memory_id (str), text (str), score (float), tags (tuple)."""
    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        client.remember("Python is a high-level programming language", tags=(tag,))
        hits = client.recall("programming language", tags=(tag,), limit=5)
        assert hits, "Expected at least one recall hit"
        hit = hits[0]
        assert isinstance(hit.memory_id, str) and len(hit.memory_id) > 0
        assert isinstance(hit.text, str) and len(hit.text) > 0
        assert isinstance(hit.score, float)
        assert isinstance(hit.tags, tuple)
    finally:
        client.close()


def test_slm_hit_score_is_finite(tmp_path: Path) -> None:
    """Scores must be finite — NaN/Inf would corrupt SlmSemanticMemory's sort."""
    import math

    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        client.remember("Finite scores are important for ranking", tags=(tag,))
        hits = client.recall("finite scores ranking", tags=(tag,), limit=5)
        for hit in hits:
            assert math.isfinite(hit.score), f"Non-finite score: {hit.score}"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# recall — tag round-trip
# ---------------------------------------------------------------------------


def test_recall_returns_tags_on_hit(tmp_path: Path) -> None:
    """Tags stored via remember() must appear in the SlmHit returned by recall()."""
    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        client.remember("The Eiffel Tower is in Paris", tags=(tag,))
        hits = client.recall("Eiffel Tower Paris", tags=(tag,), limit=5)
        assert hits, "Expected at least one recall hit"
        # The scope tag must survive the store→recall round-trip.
        found = any(tag in hit.tags for hit in hits)
        assert found, (
            f"Scope tag {tag!r} not found in any hit tags: "
            + str([h.tags for h in hits])
        )
    finally:
        client.close()


def test_recall_respects_limit(tmp_path: Path) -> None:
    """recall() must not return more hits than *limit*."""
    client = _make_client(tmp_path)
    try:
        tag = _scope_tag()
        # Store several items so the limit is meaningful.
        for i in range(5):
            client.remember(f"Memory item number {i} about dogs", tags=(tag,))
        hits = client.recall("dogs", tags=(tag,), limit=2)
        assert len(hits) <= 2, f"Expected ≤2 hits but got {len(hits)}"
    finally:
        client.close()


def test_recall_tags_different_scopes_isolated(tmp_path: Path) -> None:
    """Memories stored with scope A's tag must NOT appear tagged with scope B."""
    client = _make_client(tmp_path)
    try:
        tag_a = _scope_tag(org="org-a", project="proj-a")
        tag_b = _scope_tag(org="org-b", project="proj-b")
        client.remember("Secret data for org-a only", tags=(tag_a,))
        hits = client.recall("Secret data org-a", tags=(tag_b,), limit=5)
        # Any hit that surfaces must NOT carry tag_a if it's being returned for
        # scope-b queries — or if it does, SlmSemanticMemory will drop it.
        for hit in hits:
            # A legitimate cross-scope hit could appear (SLM is fuzzy); assert
            # it does NOT carry tag_b (which was never stored).
            assert tag_b not in hit.tags, (
                f"Hit unexpectedly carries tag_b: {hit.tags}"
            )
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Integration: SlmSemanticMemory round-trip through the real client
# ---------------------------------------------------------------------------


def test_slm_semantic_memory_roundtrip(tmp_path: Path) -> None:
    """Full integration: SlmSemanticMemory.remember → recall via real client."""
    client = _make_client(tmp_path)
    try:
        mem = SlmSemanticMemory(
            organization_id="test-org",
            project_id="test-project",
            client=client,
        )
        ns: MemoryNamespace = ("notes",)
        mem.remember(ns, "The speed of light is approximately 3×10⁸ m/s")

        hits = mem.recall(ns, "speed of light", limit=5)
        assert isinstance(hits, tuple)
        # The recall must surface a hit with the stored text.
        texts = [h.text for h in hits]
        assert any("light" in t.lower() or "speed" in t.lower() for t in texts), (
            f"Expected recall to surface the stored fact, got: {texts}"
        )
    finally:
        client.close()


def test_slm_semantic_memory_scope_isolation(tmp_path: Path) -> None:
    """Memories in one namespace must not bleed into another namespace's recall."""
    client = _make_client(tmp_path)
    try:
        mem = SlmSemanticMemory(
            organization_id="test-org",
            project_id="test-project",
            client=client,
        )
        ns_a: MemoryNamespace = ("namespace-alpha",)
        ns_b: MemoryNamespace = ("namespace-beta",)

        mem.remember(ns_a, "Alpha namespace secret: xyzzy42")

        hits_b = mem.recall(ns_b, "xyzzy42 alpha secret", limit=5)
        # SlmSemanticMemory's fail-closed post-filter must drop the ns_a memory.
        texts_b = [h.text for h in hits_b]
        assert not any("xyzzy42" in t for t in texts_b), (
            f"Namespace bleed: ns_a content surfaced in ns_b recall: {texts_b}"
        )
    finally:
        client.close()
