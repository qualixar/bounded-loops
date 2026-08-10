"""Real SLM adapter: ``SuperlocalMemorySlmClient`` backed by the actual superlocalmemory package.

Implements ``SlmClientPort`` (two methods) so ``SlmSemanticMemory`` can use the real SLM
engine with a shared, fuzzy semantic backend.  Import-guarded so the rest of the package never
hard-depends on superlocalmemory — install ``superlocalmemory`` in your environment (an
out-of-band optional backend, deliberately NOT a pinned dependency of this package, so its heavy
transitive tree never bloats a clean install) to use.

ABSOLUTE SAFETY: the constructor takes an EXPLICIT ``base_dir`` with NO default.
Constructing without a path raises ``TypeError`` — preventing any accidental write to
Varun's real ``~/.superlocalmemory/`` database.  Every test MUST pass ``tmp_path``.

Tags are persisted in ``memories.metadata_json`` under the ``_bl_tags`` key (values
joined by the ASCII Unit Separator ``\\x1f``) so they survive the store→recall round-trip
and are available to ``SlmSemanticMemory``'s fail-closed scope post-filter.

API mapping (verified from superlocalmemory v4.0.1 source):
  remember → engine.store_fast(content, metadata={\"_bl_tags\": sep-joined-tags})
              returns list[fact_id] — first element used as opaque ID
  recall  → engine.recall(query, limit=N) returns RecallResponse
              .results: list[RetrievalResult] — each has .fact: AtomicFact and .score: float
              tags fetched via sql json_extract on memories.metadata_json
"""

from __future__ import annotations

import logging
from pathlib import Path

from bounded_loops.graph.adapters.persistence.slm_semantic import SlmHit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency guard — clear ImportError pointing at the extra.
# The four guarded imports are marked `type: ignore[import-not-found]` so mypy
# accepts the optional dep.  When absent, names resolve to Any, which is
# correct: all uses are inside the `_SLM_AVAILABLE` guard, never reached.
# ---------------------------------------------------------------------------

_SLM_AVAILABLE: bool = False
_SLM_IMPORT_ERROR: str = ""

try:
    from superlocalmemory.core.config import SLMConfig  # type: ignore[import-not-found]
    from superlocalmemory.core.engine import MemoryEngine  # type: ignore[import-not-found]
    from superlocalmemory.core.engine_capabilities import Capabilities  # type: ignore[import-not-found]
    from superlocalmemory.storage.models import Mode  # type: ignore[import-not-found]

    _SLM_AVAILABLE = True
except ImportError as _slm_import_exc:
    _SLM_IMPORT_ERROR = str(_slm_import_exc)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

# ASCII Unit Separator — safe tag delimiter; never appears in scope tags (which
# are JSON-encoded strings prefixed with "bl-scope:").
_TAG_SEP: str = "\x1f"

# Key under which tags are stored in memories.metadata_json.
_META_KEY: str = "_bl_tags"

# JSON path used by SQLite json_extract to retrieve the tags field.
_META_JSON_PATH: str = f"$.{_META_KEY}"


# ---------------------------------------------------------------------------
# Concrete adapter
# ---------------------------------------------------------------------------


class SuperlocalMemorySlmClient:
    """Concrete ``SlmClientPort`` backed by the real superlocalmemory package.

    Uses ``MemoryEngine`` at ``Capabilities.FULL`` with an explicit ``base_dir``.
    Tags are persisted in metadata so ``SlmSemanticMemory``'s fail-closed scope
    post-filter operates correctly against a shared SLM backend.

    Usage::

        client = SuperlocalMemorySlmClient(base_dir=Path("/some/isolated/dir"))
        mem_id = client.remember("Alice went to Paris", tags=("bl-scope:[...]",))
        hits = client.recall("Where did Alice go?", tags=("bl-scope:[...]",), limit=10)
        client.close()
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialise a full-capability SLM engine rooted at *base_dir*.

        Args:
            base_dir: Explicit filesystem path for the engine's database files
                      (``memory.db`` and ``learning.db`` are created here).
                      **No default** — must be supplied by every caller.
                      Pass ``tmp_path`` from pytest fixtures in tests.

        Raises:
            ImportError: ``superlocalmemory`` is not installed.  Install it in
                         this environment (an out-of-band optional backend).
            TypeError:   Raised implicitly if ``base_dir`` is omitted (Python
                         enforces this because there is no default value).
        """
        if not _SLM_AVAILABLE:
            raise ImportError(
                "superlocalmemory is not installed. Install it in this environment "
                "(an out-of-band optional backend, not a pinned dependency of this package). "
                f"Underlying error: {_SLM_IMPORT_ERROR}"
            )
        _base = Path(base_dir)
        config = SLMConfig.for_mode(Mode.A, base_dir=_base)
        self._engine = MemoryEngine(config, capabilities=Capabilities.FULL)
        self._engine.initialize()
        logger.debug("SuperlocalMemorySlmClient initialised: base_dir=%s", _base)

    # ------------------------------------------------------------------
    # SlmClientPort — remember
    # ------------------------------------------------------------------

    def remember(self, text: str, *, tags: tuple[str, ...]) -> str:
        """Store *text* with *tags* and return an opaque fact ID.

        Tags are stored in ``memories.metadata_json`` under ``_bl_tags`` (joined
        by ``\\x1f``) so they are recovered during recall.

        If the SLM ingest gate rejects the content (quality gate or size limit)
        ``store_fast`` returns an empty list; this method then RAISES rather than
        fabricate an opaque ID for a memory that was never actually stored
        (fail-closed, honest — a fake ID would be unrecallable).

        Returns:
            The stored fact's opaque hex ID string.
        """
        metadata = {_META_KEY: _TAG_SEP.join(tags)}
        try:
            fact_ids: list[str] = self._engine.store_fast(text, metadata=metadata)
        except Exception as exc:
            raise RuntimeError(
                f"SuperlocalMemorySlmClient.remember failed: {exc}"
            ) from exc
        if not fact_ids:
            raise RuntimeError(
                "SuperlocalMemorySlmClient.remember: superlocalmemory stored nothing "
                "(the ingest gate rejected the content); the memory was not persisted"
            )
        return fact_ids[0]

    # ------------------------------------------------------------------
    # SlmClientPort — recall
    # ------------------------------------------------------------------

    def recall(
        self, query: str, *, tags: tuple[str, ...], limit: int
    ) -> tuple[SlmHit, ...]:
        """Recall relevant memories for *query* and return ``SlmHit`` tuples.

        Runs full SLM semantic retrieval (6-channel RRF + optional cross-encoder
        reranker) then attaches the persisted scope tags to each hit so
        ``SlmSemanticMemory.recall`` can apply its fail-closed post-filter.

        The *tags* parameter is accepted by the port signature but is NOT
        forwarded as a server-side filter — SLM's recall API has no tag filter
        parameter.  All narrowing by scope happens in ``SlmSemanticMemory``.

        Returns:
            Tuple of ``SlmHit`` ordered by SLM's descending relevance score.
            Empty tuple when no matches are found or the evidence-floor gate
            rejects all candidates.
        """
        try:
            response = self._engine.recall(query, limit=limit)
        except Exception as exc:
            raise RuntimeError(
                f"SuperlocalMemorySlmClient.recall failed: {exc}"
            ) from exc

        results = response.results or []
        if not results:
            return ()

        # Batch-fetch tags stored in memories.metadata_json for all result memory_ids.
        # Single SQL round-trip with IN clause (sized to len(results), never large).
        memory_ids: list[str] = [
            r.fact.memory_id for r in results if r.fact.memory_id
        ]
        tags_by_memory_id: dict[str, tuple[str, ...]] = {}
        if memory_ids:
            ph = ",".join("?" * len(memory_ids))
            try:
                rows = self._engine._db.execute(
                    f"SELECT memory_id, "
                    f"json_extract(metadata_json, '{_META_JSON_PATH}') AS bl_tags "
                    f"FROM memories WHERE memory_id IN ({ph})",
                    tuple(memory_ids),
                )
                for row in rows:
                    row_dict = dict(row)
                    mid: str = row_dict.get("memory_id") or ""
                    raw: str = row_dict.get("bl_tags") or ""
                    parsed: tuple[str, ...] = (
                        tuple(t for t in raw.split(_TAG_SEP) if t) if raw else ()
                    )
                    if mid:
                        tags_by_memory_id[mid] = parsed
            except Exception as exc:
                # Fail-open on the DB lookup: missing tags cause SlmSemanticMemory
                # to drop the hit (fail-closed scope filter) — the safe outcome.
                logger.warning(
                    "SuperlocalMemorySlmClient.recall: tag lookup failed (%s). "
                    "Affected hits will be dropped by the scope post-filter.",
                    exc,
                )

        hits: list[SlmHit] = []
        for r in results:
            fact = r.fact
            mem_tags: tuple[str, ...] = tags_by_memory_id.get(
                fact.memory_id or "", ()
            )
            hits.append(
                SlmHit(
                    memory_id=fact.fact_id,
                    text=fact.content,
                    score=float(r.score),
                    tags=mem_tags,
                )
            )

        return tuple(hits)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the underlying SLM engine's resources.

        Safe to call multiple times.  Silently absorbs cleanup errors to avoid
        masking the primary exception in ``finally`` blocks.
        """
        try:
            self._engine.close()
        except Exception as exc:
            logger.debug("SuperlocalMemorySlmClient.close: %s", exc)
