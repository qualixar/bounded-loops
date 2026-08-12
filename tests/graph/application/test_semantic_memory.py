"""Semantic recall-by-meaning memory — the InMemorySemanticMemory reference and the
SemanticMemoryPort contract: ranked recall, namespace scoping, limit, fail-closed validation.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.application.semantic_memory import (
    InMemorySemanticMemory,
    SemanticHit,
    SemanticMemoryPort,
)
from bounded_loops.graph.domain.errors import GraphValidationError

_NS = ("notes",)


def _store(*, org="org-1", project="proj-1"):
    return InMemorySemanticMemory(organization_id=org, project_id=project)


def test_it_satisfies_the_port():
    assert isinstance(_store(), SemanticMemoryPort)


def test_remember_then_recall_by_meaning():
    s = _store()
    s.remember(_NS, "the cat sat on the mat")
    s.remember(_NS, "database indexing strategies")
    hits = s.recall(_NS, "cat mat")
    assert [h.text for h in hits] == ["the cat sat on the mat"]
    assert isinstance(hits[0], SemanticHit)
    assert hits[0].score > 0.0 and hits[0].namespace == _NS


def test_recall_ranks_more_relevant_first():
    s = _store()
    s.remember(_NS, "alpha beta gamma")  # full overlap with "alpha beta"
    s.remember(_NS, "alpha only here")   # partial overlap
    hits = s.recall(_NS, "alpha beta")
    assert hits[0].text == "alpha beta gamma"
    assert hits[0].score >= hits[-1].score


def test_recall_is_namespace_scoped():
    s = _store()
    s.remember(("a",), "shared word apple")
    s.remember(("b",), "shared word apple")
    hits = s.recall(("a",), "apple")
    assert len(hits) == 1 and all(h.namespace == ("a",) for h in hits)


def test_recall_respects_limit():
    s = _store()
    for i in range(5):
        s.remember(_NS, f"apple number {i}")
    assert len(s.recall(_NS, "apple", limit=3)) == 3


def test_recall_returns_empty_when_nothing_matches():
    s = _store()
    s.remember(_NS, "nothing relevant here")
    assert s.recall(_NS, "zzzznomatch") == ()


def test_two_tenants_do_not_share():
    a = _store(org="orgA")
    b = _store(org="orgB")
    a.remember(_NS, "orgA secret apple")
    assert b.recall(_NS, "apple") == ()


@pytest.mark.parametrize("bad", ["", "   ", "x" + chr(0) + "y"])
def test_rejects_bad_identifiers(bad):
    with pytest.raises(GraphValidationError):
        _store(org=bad)
    with pytest.raises(GraphValidationError):
        _store().remember((bad,), "text")


def test_rejects_bad_text_query_and_limit():
    s = _store()
    with pytest.raises(GraphValidationError, match="text"):
        s.remember(_NS, "   ")
    with pytest.raises(GraphValidationError, match="query"):
        s.recall(_NS, "")
    with pytest.raises(GraphValidationError, match="limit"):
        s.recall(_NS, "apple", limit=0)
    with pytest.raises(GraphValidationError, match="limit"):
        s.recall(_NS, "apple", limit=10_000)
    with pytest.raises(GraphValidationError, match="limit"):
        s.recall(_NS, "apple", limit=True)  # bool is not an int limit


def test_empty_namespace_is_refused():
    with pytest.raises(GraphValidationError, match="namespace"):
        _store().remember((), "text")
