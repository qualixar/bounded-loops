"""Semantic (recall-by-meaning) agent memory — a SEPARATE capability from the exact-KV
memory spine in ``memory_store.py``.

The exact-KV spine answers "give me the value at THIS key". This port answers "give me the
memories in this namespace most RELATED IN MEANING to this query" — the thing SLM actually
does well (SLM V4: ``remember`` extracts facts, ``recall`` is a fuzzy ranked query). It is
deliberately a different port because a semantic store cannot honestly satisfy the exact-KV
contract (no verbatim get, no prefix listing) and vice-versa.

Like the exact-KV spine, semantic memory is tenant-scoped BY CONSTRUCTION (one store per
org/project; no cross-tenant API) and is UX, NEVER authority (ADR-12 D4): a recalled memory
can never substitute for a receipt — the event log is the source of truth. Reads are fuzzy
and ranked; callers must treat them as hints, not facts of record.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol, runtime_checkable

from bounded_loops.graph.application.memory_store import MemoryNamespace
from bounded_loops.graph.domain.errors import GraphValidationError

_MAX_IDENTIFIER_BYTES = 1024  # an org/project/namespace part is an identifier, not bulk state
_MAX_TEXT_BYTES = 64 * 1024   # a semantic memory is a fact/snippet, not a place for bulk state
_DEFAULT_RECALL_LIMIT = 10
_MAX_RECALL_LIMIT = 100
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class SemanticHit:
    """One recalled memory, ranked by relevance to a query (higher score = more relevant).
    ``namespace`` is the scope it was recalled from; ``text`` is the stored memory."""

    memory_id: str
    text: str
    score: float
    namespace: MemoryNamespace


@runtime_checkable
class SemanticMemoryPort(Protocol):
    """Recall-by-meaning agent memory, tenant-scoped by construction. UX, not authority."""

    def remember(self, namespace: MemoryNamespace, text: str) -> str:
        """Store a memory (a fact/snippet) in a namespace; returns its opaque id."""
        ...

    def recall(
        self, namespace: MemoryNamespace, query: str, *, limit: int = _DEFAULT_RECALL_LIMIT
    ) -> tuple[SemanticHit, ...]:
        """Return up to ``limit`` memories in ``namespace`` ranked by relevance to ``query``."""
        ...


class InMemorySemanticMemory:
    """Reference ``SemanticMemoryPort``: tenant-bound, deterministic keyword-overlap ranking
    (a stand-in for embeddings) so the port CONTRACT — namespace scoping, score ordering,
    limit, fail-closed validation — is testable without a real semantic backend."""

    def __init__(self, *, organization_id: str, project_id: str) -> None:
        _validate_tenant(organization_id, project_id)
        self._org = organization_id
        self._project = project_id
        self._records: list[tuple[str, MemoryNamespace, str]] = []  # (id, namespace, text)
        self._counter = 0

    def remember(self, namespace: MemoryNamespace, text: str) -> str:
        ns = _validate_namespace(namespace)
        _validate_text(text)
        self._counter += 1
        memory_id = f"mem-{self._counter}"
        self._records.append((memory_id, ns, text))
        return memory_id

    def recall(
        self, namespace: MemoryNamespace, query: str, *, limit: int = _DEFAULT_RECALL_LIMIT
    ) -> tuple[SemanticHit, ...]:
        ns = _validate_namespace(namespace)
        _validate_query(query)
        capped = _validate_limit(limit)
        query_tokens = _tokens(query)
        hits = [
            SemanticHit(memory_id, text, _overlap(query_tokens, _tokens(text)), ns)
            for memory_id, record_ns, text in self._records
            if record_ns == ns  # namespace-scoped: never recall across namespaces
        ]
        ranked = [hit for hit in hits if hit.score > 0.0]
        # score desc, then id for a deterministic tie-break
        ranked.sort(key=lambda hit: (-hit.score, hit.memory_id))
        return tuple(ranked[:capped])


def _validate_tenant(organization_id: str, project_id: str) -> None:
    _validate_identifier("semantic_tenant", "/", organization_id, "organization")
    _validate_identifier("semantic_tenant", "/", project_id, "project")


def _validate_namespace(namespace: MemoryNamespace) -> MemoryNamespace:
    if not isinstance(namespace, tuple) or not namespace:
        raise GraphValidationError("semantic_namespace", "/namespace", "namespace must be a non-empty tuple")
    for part in namespace:
        _validate_identifier("semantic_namespace", "/namespace", part, "namespace part")
    return namespace


def _validate_identifier(code: str, pointer: str, value: str, label: str) -> None:
    # Mirrors memory_store's identifier rules (non-empty stripped, byte-capped, no NUL) so a
    # semantic scope tag is as un-spoofable and backend-safe as the exact-KV netstring key.
    if not isinstance(value, str) or not value.strip():
        raise GraphValidationError(code, pointer, f"{label} must be a non-empty string")
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise GraphValidationError(code, pointer, f"{label} exceeds the identifier byte cap")
    if "\x00" in value:
        raise GraphValidationError(code, pointer, f"{label} must not contain a NUL byte")


def _validate_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise GraphValidationError("semantic_text", "/text", "memory text must be a non-empty string")
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise GraphValidationError("semantic_text", "/text", "memory text exceeds the byte cap")


def _validate_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise GraphValidationError("semantic_query", "/query", "query must be a non-empty string")
    if len(query.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise GraphValidationError("semantic_query", "/query", "query exceeds the byte cap")


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > _MAX_RECALL_LIMIT:
        raise GraphValidationError("semantic_limit", "/limit", f"limit must be an int in 1..{_MAX_RECALL_LIMIT}")
    return limit


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(text.lower()))


def _overlap(query_tokens: frozenset[str], text_tokens: frozenset[str]) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)
