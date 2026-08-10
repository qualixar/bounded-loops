"""SlmSemanticMemory — a ``SemanticMemoryPort`` backed by SLM, via an INJECTED narrow client.

The engine never hard-depends on SLM's API (the same principle as the durable exact-KV
adapter): SLM is bound behind ``SlmClientPort`` (two methods), so any semantic backend can be
swapped in and the adapter is unit-testable with a fake client. SLM's remember/recall are
semantic and FUZZY, and if one SLM instance is shared across tenants a fuzzy recall could
surface another scope's memory — so the security core is a single injection-safe SCOPE TAG
attached on remember and FAIL-CLOSED post-filtered on recall (the semantic analogue of the
exact-KV netstring key plus envelope cross-check). Semantic memory is UX, never authority
(ADR-12 D4); the post-filter favours correctness over recall completeness (it may return
fewer than ``limit`` rather than surface an unscoped hit).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Protocol, runtime_checkable

from bounded_loops.graph.application.memory_store import MemoryNamespace
from bounded_loops.graph.application.semantic_memory import (
    _DEFAULT_RECALL_LIMIT,
    SemanticHit,
    _validate_limit,
    _validate_namespace,
    _validate_query,
    _validate_tenant,
    _validate_text,
)


@dataclass(frozen=True)
class SlmHit:
    """One raw hit from the injected SLM client: an opaque id, the stored text, a relevance
    score, and the tags SLM has on it (the adapter re-checks these to confirm scope)."""

    memory_id: str
    text: str
    score: float
    tags: tuple[str, ...]


@runtime_checkable
class SlmClientPort(Protocol):
    """The narrow SLM surface the adapter needs — the deployment adapts real SLM to this."""

    def remember(self, text: str, *, tags: tuple[str, ...]) -> str: ...

    def recall(self, query: str, *, tags: tuple[str, ...], limit: int) -> tuple[SlmHit, ...]: ...


class SlmSemanticMemory:
    """``SemanticMemoryPort`` over an injected ``SlmClientPort``, tenant-scoped by a single
    injection-safe scope tag and a fail-closed recall post-filter."""

    def __init__(self, *, organization_id: str, project_id: str, client: SlmClientPort) -> None:
        _validate_tenant(organization_id, project_id)
        self._org = organization_id
        self._project = project_id
        self._client = client

    def remember(self, namespace: MemoryNamespace, text: str) -> str:
        ns = _validate_namespace(namespace)
        _validate_text(text)
        return self._client.remember(text, tags=(self._scope_tag(ns),))

    def recall(
        self, namespace: MemoryNamespace, query: str, *, limit: int = _DEFAULT_RECALL_LIMIT
    ) -> tuple[SemanticHit, ...]:
        ns = _validate_namespace(namespace)
        _validate_query(query)
        capped = _validate_limit(limit)
        scope = self._scope_tag(ns)
        hits: list[SemanticHit] = []
        for hit in self._client.recall(query, tags=(scope,), limit=capped):
            # FAIL-CLOSED: a fuzzy/shared backend may return a hit outside this scope; surface
            # ONLY hits SLM confirms carry this exact scope tag. No match => drop, never leak.
            if scope not in tuple(hit.tags):
                continue
            score = float(hit.score)
            # Drop a non-finite score (NaN/Inf): it poisons ranking and downstream JSON
            # (allow_nan), and a hint with no real relevance score is not worth surfacing.
            if not math.isfinite(score):
                continue
            hits.append(SemanticHit(hit.memory_id, hit.text, score, ns))
        # Enforce the port's ranked-by-relevance contract regardless of client order — a
        # buggy/hostile client must not defeat ranking. Tie-break by id (matches the reference).
        hits.sort(key=lambda h: (-h.score, h.memory_id))
        return tuple(hits[:capped])

    def _scope_tag(self, namespace: MemoryNamespace) -> str:
        # One tag uniquely + un-spoofably identifying (org, project, namespace): json fully
        # quotes/escapes each element, so a crafted namespace/org/project cannot forge another
        # scope's tag (the same unambiguity the exact-KV store gets from netstring keys).
        payload = json.dumps([self._org, self._project, list(namespace)], separators=(",", ":"))
        return f"bl-scope:{payload}"
