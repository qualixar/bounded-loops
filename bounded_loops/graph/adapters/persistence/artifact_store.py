"""Local controller-owned, content-addressed artifact storage."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from typing import BinaryIO, Sequence

from bounded_loops.graph.domain.artifacts import (
    ArtifactAccess,
    ArtifactPolicy,
    ArtifactRecord,
    ArtifactRef,
    ArtifactState,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError


class LocalArtifactStore:
    """Stores bytes by digest; records retain identity after bytes are removed."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise GraphIntegrityError("artifact root must not be a symlink")
        self._root = root
        self._objects = root / "objects"
        self._metadata = root / "metadata"
        self._objects.mkdir(parents=True, exist_ok=True)
        self._metadata.mkdir(parents=True, exist_ok=True)

    def put(self, stream: BinaryIO, policy: ArtifactPolicy) -> ArtifactRecord:
        return self.put_many(((stream, policy),))[0]

    def put_many(
        self, items: Sequence[tuple[BinaryIO, ArtifactPolicy]],
    ) -> tuple[ArtifactRecord, ...]:
        """Stage, pre-validate, then commit — so a mid-batch failure leaves no
        artifact metadata behind.

        Staging catches byte-cap overruns; the pre-validation pass catches a
        conflict that would otherwise surface only at commit time (for example a
        cross-tenant digest collision on a later output). Any metadata this batch
        does write is rolled back if a subsequent commit still fails, so
        multi-output promotion is all-or-nothing rather than a committed prefix.
        """
        staged: list[tuple[str, int, Path, ArtifactPolicy]] = []
        written: list[Path] = []
        try:
            for stream, policy in items:
                _validate_policy(policy)
                digest, size, temporary = self._write_temporary(stream)
                staged.append((digest, size, temporary, policy))
            for digest, size, _temporary, policy in staged:
                self._preflight_commit(digest, size, policy)
            records: list[ArtifactRecord] = []
            try:
                for digest, size, temporary, policy in staged:
                    record, wrote_metadata = self._commit(digest, size, temporary, policy)
                    if wrote_metadata:
                        written.append(self._metadata_path(digest))
                    records.append(record)
            except BaseException:
                for metadata_path in written:
                    try:
                        metadata_path.unlink()
                    except OSError:
                        pass
                raise
            return tuple(records)
        finally:
            for _digest, _size, temporary, _policy in staged:
                if temporary.exists():
                    temporary.unlink()

    def _preflight_commit(self, digest: str, size: int, policy: ArtifactPolicy) -> None:
        object_path = self._object_path(digest)
        if object_path.exists() and not object_path.is_file():
            raise GraphIntegrityError("artifact object path is not a regular file")
        metadata_path = self._metadata_path(digest)
        if metadata_path.exists():
            existing = self._read_record(digest)
            expected = ArtifactRef(digest, policy.organization_id, policy.project_id)
            if existing.ref != expected or existing.size != size:
                raise GraphIntegrityError("artifact digest conflicts with existing tenant metadata")

    def _commit(self, digest: str, size: int, temporary: Path, policy: ArtifactPolicy) -> tuple[ArtifactRecord, bool]:
        object_path = self._object_path(digest)
        metadata_path = self._metadata_path(digest)
        if object_path.exists() and not object_path.is_file():
            raise GraphIntegrityError("artifact object path is not a regular file")
        if not object_path.exists():
            os.replace(temporary, object_path)
        record = ArtifactRecord(
            ref=ArtifactRef(digest, policy.organization_id, policy.project_id),
            digest=digest,
            media_type=policy.media_type,
            size=size,
            producer_attempt=policy.producer_attempt,
            sensitivity=policy.sensitivity,
            retention_class=policy.retention_class,
            state=ArtifactState.ACTIVE,
            tombstone_reason=None,
            expires_at=policy.expires_at,
            legal_hold_allowed=policy.legal_hold_allowed,
            legal_hold=False,
        )
        if metadata_path.exists():
            existing = self._read_record(digest)
            if existing.ref != record.ref or existing.size != record.size:
                raise GraphIntegrityError("artifact digest conflicts with existing tenant metadata")
            return existing, False
        if not _write_json_exclusive(metadata_path, _record_dict(record)):
            # Lost a concurrent first-writer race for this digest; reconcile.
            existing = self._read_record(digest)
            if existing.ref != record.ref or existing.size != record.size:
                raise GraphIntegrityError("artifact digest conflicts with existing tenant metadata")
            return existing, False
        return record, True

    def open(self, ref: ArtifactRef, access: ArtifactAccess) -> BytesIO:
        record = self._read_record(ref.digest)
        _authorize(record, ref, access)
        if record.state is not ArtifactState.ACTIVE:
            raise GraphIntegrityError("artifact is not active")
        path = self._object_path(record.digest)
        if path.is_symlink() or not path.is_file():
            raise GraphIntegrityError("artifact bytes are unavailable")
        data = path.read_bytes()
        if _digest(data) != record.digest:
            raise GraphIntegrityError("artifact digest mismatch")
        return BytesIO(data)

    def tombstone(self, ref: ArtifactRef, reason: str) -> ArtifactRecord:
        if not isinstance(reason, str) or not reason:
            raise GraphIntegrityError("tombstone reason must be non-empty")
        record = self._read_record(ref.digest)
        _authorize(record, ref, ArtifactAccess(ref.organization_id, ref.project_id))
        if record.state is ArtifactState.TOMBSTONED:
            return record
        if record.legal_hold:
            raise GraphIntegrityError("artifact is under legal hold")
        object_path = self._object_path(record.digest)
        if object_path.exists():
            if object_path.is_symlink() or not object_path.is_file():
                raise GraphIntegrityError("artifact object path is unsafe")
            object_path.unlink()
        tombstoned = ArtifactRecord(
            ref=record.ref, digest=record.digest, media_type=record.media_type, size=record.size,
            producer_attempt=record.producer_attempt, sensitivity=record.sensitivity,
            retention_class=record.retention_class, state=ArtifactState.TOMBSTONED,
            tombstone_reason=reason,
            expires_at=record.expires_at, legal_hold_allowed=record.legal_hold_allowed,
            legal_hold=record.legal_hold,
        )
        _write_json(self._metadata_path(ref.digest), _record_dict(tombstoned))
        return tombstoned

    def set_legal_hold(self, ref: ArtifactRef, enabled: bool) -> ArtifactRecord:
        """Set or release a permitted hold; a tombstoned artifact cannot be restored."""
        if not isinstance(enabled, bool):
            raise GraphIntegrityError("legal hold must be boolean")
        record = self._read_record(ref.digest)
        _authorize(record, ref, ArtifactAccess(ref.organization_id, ref.project_id))
        if record.state is not ArtifactState.ACTIVE:
            raise GraphIntegrityError("legal hold requires an active artifact")
        if not record.legal_hold_allowed:
            raise GraphIntegrityError("artifact policy does not allow legal hold")
        updated = replace(record, legal_hold=enabled)
        _write_json(self._metadata_path(ref.digest), _record_dict(updated))
        return updated

    def sweep_expired(self, now: datetime) -> tuple[ArtifactRecord, ...]:
        """Tombstone expired active bytes unless an allowed legal hold remains active."""
        current = _instant(now, "retention clock")
        expired: list[ArtifactRecord] = []
        for path in sorted(self._metadata.glob("*.json")):
            if path.is_symlink() or not path.is_file() or len(path.stem) != 64:
                raise GraphIntegrityError("artifact metadata directory contains an unsafe entry")
            record = self._read_record("sha256:" + path.stem)
            if (
                record.state is ArtifactState.ACTIVE
                and record.expires_at is not None
                and _instant_text(record.expires_at) <= current
                and not record.legal_hold
            ):
                expired.append(self.tombstone(record.ref, "retention_expired"))
        return tuple(expired)

    def _write_temporary(self, stream: BinaryIO) -> tuple[str, int, Path]:
        fd, name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=self._objects)
        path = Path(name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while chunk := stream.read(1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise GraphIntegrityError("artifact stream must yield bytes")
                    hasher.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if path.exists():
                path.unlink()
            raise
        return "sha256:" + hasher.hexdigest(), size, path

    def _object_path(self, digest: str) -> Path:
        _validate_digest(digest)
        return self._objects / digest.removeprefix("sha256:")

    def _metadata_path(self, digest: str) -> Path:
        _validate_digest(digest)
        return self._metadata / f"{digest.removeprefix('sha256:')}.json"

    def _read_record(self, digest: str) -> ArtifactRecord:
        path = self._metadata_path(digest)
        if path.is_symlink():
            raise GraphIntegrityError("artifact metadata path is unsafe")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphIntegrityError("artifact metadata is unreadable") from exc
        try:
            return ArtifactRecord(
                ref=ArtifactRef(**raw["ref"]), digest=raw["digest"], media_type=raw["media_type"],
                size=raw["size"], producer_attempt=raw["producer_attempt"], sensitivity=raw["sensitivity"],
                retention_class=raw["retention_class"], state=ArtifactState(raw["state"]),
                tombstone_reason=raw["tombstone_reason"],
                expires_at=raw.get("expires_at"),
                legal_hold_allowed=raw.get("legal_hold_allowed", False),
                legal_hold=raw.get("legal_hold", False),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIntegrityError("artifact metadata has an invalid shape") from exc


def _validate_policy(policy: ArtifactPolicy) -> None:
    for value in (
        policy.organization_id, policy.project_id, policy.producer_attempt,
        policy.media_type, policy.sensitivity, policy.retention_class,
    ):
        if not isinstance(value, str) or not value:
            raise GraphIntegrityError("artifact policy fields must be non-empty strings")
    if policy.expires_at is not None:
        _instant_text(policy.expires_at)
    if not isinstance(policy.legal_hold_allowed, bool):
        raise GraphIntegrityError("artifact legal_hold_allowed must be boolean")


def _authorize(record: ArtifactRecord, ref: ArtifactRef, access: ArtifactAccess) -> None:
    if record.ref != ref or (access.organization_id, access.project_id) != (record.ref.organization_id, record.ref.project_id):
        raise GraphIntegrityError("unauthorized artifact access")


def _record_dict(record: ArtifactRecord) -> dict[str, object]:
    return {
        "digest": record.digest, "media_type": record.media_type, "producer_attempt": record.producer_attempt,
        "ref": {"digest": record.ref.digest, "organization_id": record.ref.organization_id, "project_id": record.ref.project_id},
        "retention_class": record.retention_class, "sensitivity": record.sensitivity, "size": record.size,
        "state": record.state.value, "tombstone_reason": record.tombstone_reason,
        "expires_at": record.expires_at, "legal_hold_allowed": record.legal_hold_allowed,
        "legal_hold": record.legal_hold,
    }


def _write_json(path: Path, data: dict[str, object]) -> None:
    fd, name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_exclusive(path: Path, data: dict[str, object]) -> bool:
    """First-writer-wins JSON publish: create ``path`` by hard link so two
    concurrent first commits for the same digest cannot silently overwrite one
    another's attribution. Returns True if this call created the file."""
    fd, name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        if temporary.exists():
            temporary.unlink()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_digest(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
        raise GraphIntegrityError("artifact digest is invalid")


def _instant(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GraphIntegrityError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _instant_text(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise GraphIntegrityError("artifact expiry must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphIntegrityError("artifact expiry must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphIntegrityError("artifact expiry must include a timezone")
    return parsed.astimezone(timezone.utc)
