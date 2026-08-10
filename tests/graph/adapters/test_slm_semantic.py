"""SlmSemanticMemory — SemanticMemoryPort over an injected SLM client. Tests the injection-safe
scope tag, the FAIL-CLOSED recall post-filter, and shared-backend tenant/namespace isolation.
"""

from __future__ import annotations

import json
import math

import pytest

from bounded_loops.graph.adapters.persistence.slm_semantic import (
    SlmHit,
    SlmSemanticMemory,
)
from bounded_loops.graph.application.semantic_memory import SemanticHit, SemanticMemoryPort
from bounded_loops.graph.domain.errors import GraphValidationError

_NS = ("notes",)


class _FakeSlm:
    """A shared, fuzzy semantic backend: stores (text, tags); recall ranks by whitespace-token
    overlap with the query and, unless ``leak`` is set, honours the scope tag. ``leak`` returns
    rows regardless of tag — modelling a fuzzy/compromised store so the adapter's post-filter
    is exercised."""

    def __init__(self, *, leak: bool = False) -> None:
        self.remembered: list[tuple[str, tuple[str, ...]]] = []
        self._leak = leak

    def remember(self, text: str, *, tags: tuple[str, ...]) -> str:
        self.remembered.append((text, tuple(tags)))
        return f"id-{len(self.remembered)}"

    def recall(self, query: str, *, tags: tuple[str, ...], limit: int) -> tuple[SlmHit, ...]:
        wanted = set(query.lower().split())
        hits = []
        for i, (text, row_tags) in enumerate(self.remembered, start=1):
            if not self._leak and not (set(tags) & set(row_tags)):
                continue  # a well-behaved store honours the scope tag
            overlap = len(wanted & set(text.lower().split()))
            if overlap == 0:
                continue
            hits.append(SlmHit(f"id-{i}", text, float(overlap), row_tags))
        hits.sort(key=lambda h: -h.score)
        return tuple(hits[:limit])


def _mem(client, *, org="org-1", project="proj-1"):
    return SlmSemanticMemory(organization_id=org, project_id=project, client=client)


def test_it_satisfies_the_port():
    assert isinstance(_mem(_FakeSlm()), SemanticMemoryPort)


def test_remember_attaches_a_single_scope_tag():
    client = _FakeSlm()
    _mem(client).remember(_NS, "hello")
    text, tags = client.remembered[0]
    expected = "bl-scope:" + json.dumps(["org-1", "proj-1", ["notes"]], separators=(",", ":"))
    assert text == "hello" and tags == (expected,)


def test_recall_maps_hits_and_preserves_namespace():
    client = _FakeSlm()
    m = _mem(client)
    m.remember(_NS, "alpha")
    hits = m.recall(_NS, "alpha")
    assert len(hits) == 1
    assert isinstance(hits[0], SemanticHit)
    assert hits[0].text == "alpha" and hits[0].namespace == _NS and hits[0].score == 1.0


def test_recall_post_filter_drops_a_leaked_out_of_scope_hit():
    client = _FakeSlm(leak=True)
    m = _mem(client)
    # a row planted under a DIFFERENT scope, returned by a leaky/compromised backend
    client.remembered.append(("other tenant secret", ("bl-scope:other",)))
    assert m.recall(_NS, "secret") == ()  # post-filter refuses the unscoped hit


def test_shared_backend_isolates_tenants_and_namespaces():
    client = _FakeSlm()  # ONE backend shared by all stores
    a = _mem(client, org="orgA")
    b = _mem(client, org="orgB")
    a.remember(_NS, "orgA apple")
    b.remember(_NS, "orgB apple")
    a.remember(("other",), "orgA banana in other ns")
    assert [h.text for h in b.recall(_NS, "apple")] == ["orgB apple"]
    assert [h.text for h in a.recall(_NS, "apple")] == ["orgA apple"]
    assert [h.text for h in a.recall(_NS, "banana")] == []          # ("other",) ns excluded
    assert [h.text for h in a.recall(("other",), "banana")] == ["orgA banana in other ns"]


def test_a_crafted_namespace_cannot_forge_another_scope():
    client = _FakeSlm()
    a = _mem(client, org="orgA")
    a.remember(("notes",), "orgA apple")
    b = _mem(client, org="orgB")
    # A namespace crafted to try to "close" the json and inject orgA's scope tag.
    evil_ns = ('", "proj-1", ["notes"]]',)
    b.remember(evil_ns, "orgB decoy")
    assert b.recall(("notes",), "apple") == ()
    assert b.recall(evil_ns, "apple") == ()
    a_tag = a._scope_tag(("notes",))
    b_tag = b._scope_tag(evil_ns)
    assert a_tag != b_tag
    assert a_tag.startswith('bl-scope:["orgA"') and b_tag.startswith('bl-scope:["orgB"')


class _ScopedClient:
    """Returns fixed hits (all carrying the requested scope so the post-filter keeps them),
    letting a test isolate the adapter's own ordering / score handling."""

    def __init__(self, scores):
        self._scores = list(scores)

    def remember(self, text, *, tags):  # pragma: no cover - unused here
        return "id"

    def recall(self, query, *, tags, limit):
        return tuple(
            SlmHit(f"id-{i}", f"t{i}", score, tuple(tags))
            for i, score in enumerate(self._scores)
        )


def test_recall_enforces_descending_score_order_regardless_of_client():
    # A client that returns hits out of order must not defeat the "ranked by relevance"
    # contract — the adapter re-sorts by score desc.
    m = _mem(_ScopedClient([0.1, 0.9, 0.5]))
    assert [h.score for h in m.recall(_NS, "q")] == [0.9, 0.5, 0.1]


def test_recall_drops_non_finite_scores():
    # NaN/Inf poison ranking and downstream JSON (allow_nan); drop them fail-closed.
    m = _mem(_ScopedClient([float("nan"), float("inf"), 0.5]))
    hits = m.recall(_NS, "q")
    assert [h.text for h in hits] == ["t2"]
    assert all(math.isfinite(h.score) for h in hits)


def test_validation_is_fail_closed_before_touching_the_client():
    client = _FakeSlm()
    m = _mem(client)
    with pytest.raises(GraphValidationError):
        m.remember(("x" + chr(0) + "y",), "text")
    with pytest.raises(GraphValidationError):
        m.recall(_NS, "")
    assert client.remembered == []  # nothing reached the backend
