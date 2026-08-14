"""Content-addressed, append-only audit persistence (LLD 06 / ADR-12).

Each domain object (AuditPlan, AuditResult, AuditedArtifact, RepairAttempt) is
serialised to canonical JSON (sort_keys, no extra whitespace), its SHA-256 is
computed, and the blob is written under ``<type_dir>/<sha256hex>.json``.  Writes
are atomic (write-to-temp then rename) so a crash never leaves a partial file.
Reads verify the on-disk digest against the expected key before returning —
any byte-level tamper is caught at load time with a ``GraphIntegrityError``.

Cryptographic *signing* of stored blobs is DEFERRED — the next implementer
should layer a signing key over the ``put_*`` / ``load_*`` pairs.  The digest
check here provides tamper-evidence but not non-repudiation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import tempfile

from bounded_loops.graph.domain.audit_serde import (
    artifact_from_mapping as _artifact_from_dict,
    artifact_to_dict as _artifact_to_dict,
    plan_from_mapping,
    plan_to_dict as _plan_to_dict,
    repair_from_mapping as _repair_from_dict,
    repair_to_dict as _repair_to_dict,
    result_from_mapping,
    result_to_dict as _result_to_dict,
)
from bounded_loops.graph.domain.audits import (
    AuditPlan,
    AuditResult,
    AuditedArtifact,
    RepairAttempt,
)

from bounded_loops.graph.domain.errors import GraphIntegrityError


# Sub-directories, one per stored type.
_PLANS = "audit_plans"
_RESULTS = "audit_results"
_ARTIFACTS = "audited_artifacts"
_REPAIRS = "repair_attempts"


class LocalAuditStore:
    """Append-only, content-addressed local store for audit domain objects.

    All four stored types share the same mechanics:
    - ``put_*`` serialises → digests → writes atomically → returns ``sha256:…``.
    - ``load_*`` reads → re-digests → compares → deserialises.
    - Idempotent: putting the same content twice is a no-op (first-writer wins).
    - Tamper-evident: a corrupted file raises ``GraphIntegrityError`` on load.
    """

    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise GraphIntegrityError("audit store root must not be a symlink")
        self._root = root
        for subdir in (_PLANS, _RESULTS, _ARTIFACTS, _REPAIRS):
            (root / subdir).mkdir(parents=True, exist_ok=True)

    # ── AuditPlan ────────────────────────────────────────────────────────────

    def put_plan(self, plan: AuditPlan) -> str:
        """Serialise and store an AuditPlan; return its content digest."""
        return self._put(_PLANS, _plan_to_dict(plan))

    def load_plan(self, plan_digest: str) -> AuditPlan:
        """Load an AuditPlan by its content digest; raise on missing or tamper."""
        data = self._load(_PLANS, plan_digest)
        try:
            return plan_from_mapping(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError("audit plan has an invalid shape") from exc

    # ── AuditResult ──────────────────────────────────────────────────────────

    def put_result(self, result: AuditResult) -> str:
        """Serialise and store an AuditResult; return its content digest."""
        return self._put(_RESULTS, _result_to_dict(result))

    def load_result(self, result_digest: str) -> AuditResult:
        """Load an AuditResult by its content digest; raise on missing or tamper."""
        data = self._load(_RESULTS, result_digest)
        try:
            return result_from_mapping(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError("audit result has an invalid shape") from exc

    # ── AuditedArtifact ──────────────────────────────────────────────────────

    def put_audited_artifact(self, artifact: AuditedArtifact) -> str:
        """Serialise and store an AuditedArtifact; return its content digest."""
        return self._put(_ARTIFACTS, _artifact_to_dict(artifact))

    def load_audited_artifact(self, record_digest: str) -> AuditedArtifact:
        """Load an AuditedArtifact by its content digest; raise on missing or tamper."""
        data = self._load(_ARTIFACTS, record_digest)
        try:
            return _artifact_from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError("audited artifact has an invalid shape") from exc

    # ── RepairAttempt ────────────────────────────────────────────────────────

    def put_repair(self, repair: RepairAttempt) -> str:
        """Serialise and store a RepairAttempt; return its content digest."""
        return self._put(_REPAIRS, _repair_to_dict(repair))

    def load_repair(self, repair_digest: str) -> RepairAttempt:
        """Load a RepairAttempt by its content digest; raise on missing or tamper."""
        data = self._load(_REPAIRS, repair_digest)
        try:
            return _repair_from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError("repair attempt has an invalid shape") from exc

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _put(self, subdir: str, data: dict[str, object]) -> str:
        canonical = _canonical_json(data)
        digest = _sha256(canonical)
        path = self._path(subdir, digest)
        if not path.exists():
            _write_atomic(path, canonical)
        return digest

    def _load(self, subdir: str, digest: str) -> dict[str, object]:
        _require_sha256(digest)
        path = self._path(subdir, digest)
        if not path.exists():
            raise GraphIntegrityError(f"audit record not found: {digest}")
        if path.is_symlink():
            raise GraphIntegrityError("audit record path is a symlink")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GraphIntegrityError("audit record is unreadable") from exc
        actual = _sha256(content)
        if actual != digest:
            raise GraphIntegrityError(
                f"audit record tampered: expected {digest}, got {actual}"
            )
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise GraphIntegrityError("audit record is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise GraphIntegrityError("audit record must be a JSON object")
        return raw

    def _path(self, subdir: str, digest: str) -> Path:
        _require_sha256(digest)
        hex_key = digest.removeprefix("sha256:")
        return self._root / subdir / f"{hex_key}.json"


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _canonical_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_sha256(value: str) -> None:
    # Enforce the FULL hex charset, not just prefix+length: a load digest is caller-supplied, and a
    # 71-char string containing "/" or ".." would otherwise path-escape the store root in `_path`.
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise GraphIntegrityError("audit store key must be a SHA-256 digest")


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a temp file + rename."""
    fd, name = tempfile.mkstemp(prefix=".audit-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, path)
        except OSError:
            # Lost a concurrent first-writer race — existing content wins (idempotent).
            pass
    finally:
        if tmp.exists():
            tmp.unlink()
