"""Controller-owned validation and promotion of graph-worker outputs."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from types import MappingProxyType
from typing import BinaryIO, Mapping, Protocol, Sequence

from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRecord, ArtifactRef
from bounded_loops.graph.domain.errors import GraphIntegrityError


class ArtifactWriterPort(Protocol):
    def put(self, stream: BinaryIO, policy: ArtifactPolicy) -> ArtifactRecord: ...


class ArtifactReaderPort(Protocol):
    def open(self, ref: ArtifactRef, access: ArtifactAccess) -> BinaryIO: ...


@dataclass(frozen=True)
class WorkspaceInput:
    target_path: str
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        _validate_relative_output(self.target_path)


@dataclass(frozen=True)
class WorkspacePromotionPolicy:
    """The complete output authority for exactly one graph attempt."""

    organization_id: str
    project_id: str
    producer_attempt: str
    declared_outputs: Mapping[str, str]
    max_file_bytes: int
    sensitivity: str
    retention_class: str

    def __post_init__(self) -> None:
        outputs = dict(self.declared_outputs)
        if not outputs:
            raise GraphIntegrityError("declared outputs must not be empty")
        for relative, media_type in outputs.items():
            _validate_relative_output(relative)
            if not isinstance(media_type, str) or not media_type:
                raise GraphIntegrityError("declared output media type must be non-empty")
        for value in (self.organization_id, self.project_id, self.producer_attempt, self.sensitivity, self.retention_class):
            if not isinstance(value, str) or not value:
                raise GraphIntegrityError("workspace promotion policy fields must be non-empty")
        if isinstance(self.max_file_bytes, bool) or not isinstance(self.max_file_bytes, int) or self.max_file_bytes < 1:
            raise GraphIntegrityError("max file bytes must be positive")
        object.__setattr__(self, "declared_outputs", MappingProxyType(outputs))


def promote_workspace_outputs(
    workspace: Path,
    policy: WorkspacePromotionPolicy,
    artifact_writer: ArtifactWriterPort,
) -> tuple[ArtifactRecord, ...]:
    """Reject unsafe output trees, then promote only declared regular files."""
    if workspace.is_symlink() or not workspace.is_dir():
        raise GraphIntegrityError("workspace must be a non-symlink directory")
    actual = _regular_output_paths(workspace)
    declared = set(policy.declared_outputs)
    unexpected = sorted(actual - declared)
    if unexpected:
        raise GraphIntegrityError(f"workspace contains undeclared output: {unexpected[0]}")
    missing = sorted(declared - actual)
    if missing:
        raise GraphIntegrityError(f"workspace is missing declared output: {missing[0]}")
    promoted: list[ArtifactRecord] = []
    for relative in sorted(declared):
        path = workspace / relative
        descriptor = _open_declared_regular_file(path, policy.max_file_bytes)
        try:
            with os.fdopen(descriptor, "rb") as handle:
                promoted.append(artifact_writer.put(
                    handle,
                    ArtifactPolicy(
                        organization_id=policy.organization_id,
                        project_id=policy.project_id,
                        producer_attempt=policy.producer_attempt,
                        media_type=policy.declared_outputs[relative],
                        sensitivity=policy.sensitivity,
                        retention_class=policy.retention_class,
                    ),
                ))
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
    return tuple(promoted)


def materialize_workspace_inputs(
    workspace: Path,
    inputs: Sequence[WorkspaceInput],
    access: ArtifactAccess,
    artifact_reader: ArtifactReaderPort,
) -> tuple[Path, ...]:
    """Materialize declared tenant-authorized input artifacts as read-only files."""
    if workspace.is_symlink() or not workspace.is_dir():
        raise GraphIntegrityError("workspace must be a non-symlink directory")
    targets = tuple(item.target_path for item in inputs)
    if len(set(targets)) != len(targets):
        raise GraphIntegrityError("workspace inputs must not target the same path")
    materialized: list[Path] = []
    for item in inputs:
        target = workspace / item.target_path
        _ensure_safe_parent(workspace, target.parent)
        if target.exists() or target.is_symlink():
            raise GraphIntegrityError("workspace input target already exists")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".input-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination, artifact_reader.open(item.artifact, access) as source:
                while chunk := source.read(1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise GraphIntegrityError("artifact input stream must yield bytes")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.replace(temporary, target)
            materialized.append(target)
        finally:
            if temporary.exists():
                temporary.unlink()
    return tuple(materialized)


def _regular_output_paths(workspace: Path) -> set[str]:
    found: set[str] = set()

    def walk(directory: Path, prefix: PurePosixPath) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = prefix / entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    raise GraphIntegrityError(f"workspace contains symlink output: {relative}")
                if stat.S_ISDIR(mode):
                    walk(Path(entry.path), relative)
                elif stat.S_ISREG(mode):
                    found.add(str(relative))
                else:
                    raise GraphIntegrityError(f"workspace contains special output: {relative}")

    walk(workspace, PurePosixPath())
    return found


def _open_declared_regular_file(path: Path, maximum: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GraphIntegrityError("workspace output is unavailable or unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GraphIntegrityError("workspace output is not a regular file")
        if metadata.st_size > maximum:
            raise GraphIntegrityError("workspace output is oversized")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_safe_parent(workspace: Path, parent: Path) -> None:
    relative = parent.relative_to(workspace)
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise GraphIntegrityError("workspace input parent is a symlink")
        if current.exists() and not current.is_dir():
            raise GraphIntegrityError("workspace input parent is not a directory")
        current.mkdir(exist_ok=True)


def _validate_relative_output(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphIntegrityError("declared output path must be non-empty")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise GraphIntegrityError("declared output path must be relative and traversal-free")
