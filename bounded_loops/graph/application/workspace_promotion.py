"""Controller-owned validation and promotion of graph-worker outputs.

The workspace actor is untrusted. Every traversal is anchored to an open
directory descriptor and refuses to follow symlinks at any component, so a
name validated during enumeration can never be re-resolved to a swapped target
during the open (defeats pathname TOCTOU). Byte caps are enforced while
streaming an opened descriptor, not only from a pre-read ``fstat`` (defeats
growth-after-check). Declared paths are rejected unless they are portable,
POSIX-relative, and traversal-free (defeats Windows-separator/drive ambiguity).
Multi-output promotion stages every output before committing any (all-or-
nothing), and materialization renames only after every input is staged.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from types import MappingProxyType
from typing import BinaryIO, Mapping, Protocol, Sequence
from uuid import uuid4

from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactRecord, ArtifactRef
from bounded_loops.graph.domain.errors import GraphIntegrityError

_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_CHUNK = 1024 * 1024


class _ByteSource(Protocol):
    def read(self, size: int = ...) -> bytes: ...


class ArtifactWriterPort(Protocol):
    def put_many(self, items: Sequence[tuple[_ByteSource, ArtifactPolicy]]) -> tuple[ArtifactRecord, ...]: ...


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
    """Reject unsafe output trees, then atomically promote declared regular files."""
    root_fd = _open_directory(workspace)
    try:
        found = _enumerate_regular_outputs(root_fd)
        declared = set(policy.declared_outputs)
        unexpected = sorted(found - declared)
        if unexpected:
            raise GraphIntegrityError(f"workspace contains undeclared output: {unexpected[0]}")
        missing = sorted(declared - found)
        if missing:
            raise GraphIntegrityError(f"workspace is missing declared output: {missing[0]}")
        descriptors: list[int] = []
        try:
            items: list[tuple[_ByteSource, ArtifactPolicy]] = []
            for relative in sorted(declared):
                descriptor = _open_declared_output(root_fd, relative, policy.max_file_bytes)
                descriptors.append(descriptor)
                items.append((
                    _BoundedReader(descriptor, policy.max_file_bytes),
                    ArtifactPolicy(
                        organization_id=policy.organization_id,
                        project_id=policy.project_id,
                        producer_attempt=policy.producer_attempt,
                        media_type=policy.declared_outputs[relative],
                        sensitivity=policy.sensitivity,
                        retention_class=policy.retention_class,
                    ),
                ))
            return artifact_writer.put_many(items)
        finally:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    finally:
        os.close(root_fd)


def materialize_workspace_inputs(
    workspace: Path,
    inputs: Sequence[WorkspaceInput],
    access: ArtifactAccess,
    artifact_reader: ArtifactReaderPort,
) -> tuple[Path, ...]:
    """Materialize declared tenant-authorized input artifacts as read-only files.

    Every input is staged to a temporary file beside its descriptor-verified
    parent before any target name is published, so a later failure leaves the
    workspace with no partially materialized inputs.
    """
    root_fd = _open_directory(workspace)
    owned_fds: list[int] = []
    staged: list[tuple[int, str, str, Path, os.stat_result]] = []
    committed: list[tuple[int, str]] = []
    try:
        targets = tuple(item.target_path for item in inputs)
        if len(set(targets)) != len(targets):
            raise GraphIntegrityError("workspace inputs must not target the same path")
        for item in inputs:
            parts = PurePosixPath(item.target_path).parts
            parent_fd, opened = _ensure_parent_dirs(root_fd, parts[:-1])
            owned_fds.extend(opened)
            leaf = parts[-1]
            if _exists_at(parent_fd, leaf):
                raise GraphIntegrityError("workspace input target already exists")
            temporary = f".input-{uuid4().hex}.tmp"
            identity = _stage_input(parent_fd, temporary, item, access, artifact_reader)
            staged.append((parent_fd, temporary, leaf, workspace / item.target_path, identity))
        materialized: list[Path] = []
        for parent_fd, temporary, leaf, target, identity in staged:
            # Publish by hard link so an existing target fails closed instead of
            # being silently replaced (rename would clobber a raced/squatted name).
            try:
                os.link(temporary, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileExistsError as exc:
                raise GraphIntegrityError("workspace input target appeared before publish") from exc
            # Drop the staged temp name first so a correctly published leaf has a
            # single link, then verify the leaf is exactly the inode we wrote with
            # no extra hardlink alias — a temp/leaf swapped or aliased in the
            # window fails closed.
            _unlink_quietly(parent_fd, temporary)
            _verify_published(parent_fd, leaf, identity)
            committed.append((parent_fd, leaf))
            materialized.append(target)
        return tuple(materialized)
    except BaseException:
        for parent_fd, leaf in committed:
            _unlink_quietly(parent_fd, leaf)
        for parent_fd, temporary, _leaf, _target, _identity in staged:
            _unlink_quietly(parent_fd, temporary)
        raise
    finally:
        for descriptor in owned_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        os.close(root_fd)


class _BoundedReader:
    """Adapt an open descriptor to a capped byte stream for the artifact writer."""

    def __init__(self, descriptor: int, maximum: int) -> None:
        self._descriptor = descriptor
        self._maximum = maximum
        self._seen = 0

    def read(self, size: int = -1) -> bytes:
        want = _CHUNK if size is None or size < 0 else size
        chunk = os.read(self._descriptor, want)
        self._seen += len(chunk)
        if self._seen > self._maximum:
            raise GraphIntegrityError("workspace output is oversized")
        return chunk


def _stage_input(
    parent_fd: int,
    temporary: str,
    item: WorkspaceInput,
    access: ArtifactAccess,
    artifact_reader: ArtifactReaderPort,
) -> os.stat_result:
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
        0o600,
        dir_fd=parent_fd,
    )
    wrote = False
    identity: os.stat_result | None = None
    try:
        with os.fdopen(descriptor, "wb") as destination, artifact_reader.open(item.artifact, access) as source:
            while chunk := source.read(_CHUNK):
                if not isinstance(chunk, bytes):
                    raise GraphIntegrityError("artifact input stream must yield bytes")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
            os.fchmod(destination.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            # Capture the written inode while the descriptor is still open so the
            # publish step can prove the target resolves to exactly this file.
            identity = _stat_fd(destination.fileno())
        wrote = True
    finally:
        if not wrote:
            _unlink_quietly(parent_fd, temporary)
    if identity is None:  # pragma: no cover - unreachable once the write succeeds
        raise GraphIntegrityError("workspace input staging failed to capture an identity")
    return identity


def _stat_at(name: str, dir_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _enumerate_regular_outputs(root_fd: int) -> set[str]:
    found: set[str] = set()

    def walk(directory_fd: int, prefix: PurePosixPath) -> None:
        try:
            with os.scandir(directory_fd) as scanner:
                names = [entry.name for entry in scanner]
        except OSError as exc:
            raise GraphIntegrityError("workspace output tree could not be enumerated") from exc
        for name in names:
            relative = prefix / name
            try:
                info = _stat_at(name, directory_fd)
            except OSError as exc:
                raise GraphIntegrityError("workspace output changed during enumeration") from exc
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                raise GraphIntegrityError(f"workspace contains symlink output: {relative}")
            if stat.S_ISDIR(mode):
                try:
                    child = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=directory_fd)
                except OSError as exc:
                    raise GraphIntegrityError("workspace output changed during enumeration") from exc
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                found.add(str(relative))
            else:
                raise GraphIntegrityError(f"workspace contains special output: {relative}")

    walk(root_fd, PurePosixPath())
    return found


def _open_declared_output(root_fd: int, relative: str, maximum: int) -> int:
    parts = PurePosixPath(relative).parts
    current = root_fd
    intermediates: list[int] = []
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=current)
            intermediates.append(child)
            current = child
        return _open_regular_source(parts[-1], current, maximum)
    except OSError as exc:
        raise GraphIntegrityError("workspace output is unavailable or unsafe") from exc
    finally:
        for descriptor in intermediates:
            os.close(descriptor)


def _open_regular_source(name: str, dir_fd: int, maximum: int) -> int:
    descriptor = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=dir_fd)
    try:
        info = _stat_fd(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GraphIntegrityError("workspace output is not a regular file")
        if info.st_size > maximum:
            raise GraphIntegrityError("workspace output is oversized")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_parent_dirs(root_fd: int, parts: tuple[str, ...]) -> tuple[int, list[int]]:
    current = root_fd
    owned: list[int] = []
    try:
        for part in parts:
            try:
                info = os.stat(part, dir_fd=current, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass  # created concurrently; validated by the no-follow open below
            else:
                if stat.S_ISLNK(info.st_mode):
                    raise GraphIntegrityError("workspace input parent is a symlink")
                if not stat.S_ISDIR(info.st_mode):
                    raise GraphIntegrityError("workspace input parent is not a directory")
            try:
                child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=current)
            except OSError as exc:
                raise GraphIntegrityError("workspace input parent is unsafe") from exc
            owned.append(child)
            current = child
    except BaseException:
        for descriptor in owned:
            os.close(descriptor)
        raise
    return current, owned


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _require_descriptor_safe_flags() -> None:
    # These POSIX flags are the basis of the no-follow traversal. If the running
    # interpreter/platform lacks them the getattr fallbacks are 0, which would
    # silently disable symlink protection — refuse rather than fail open.
    if not _O_NOFOLLOW or not _O_DIRECTORY or not _O_NONBLOCK:
        raise GraphIntegrityError("platform lacks descriptor-safe file flags (O_NOFOLLOW/O_DIRECTORY/O_NONBLOCK)")


def _verify_published(parent_fd: int, leaf: str, identity: os.stat_result) -> None:
    descriptor = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=parent_fd)
    try:
        published = _stat_fd(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(published.st_mode)
        or published.st_nlink != 1
        or published.st_dev != identity.st_dev
        or published.st_ino != identity.st_ino
        or published.st_size != identity.st_size
    ):
        _unlink_quietly(parent_fd, leaf)
        raise GraphIntegrityError("published workspace input does not match the staged artifact")


def _open_directory(workspace: Path) -> int:
    _require_descriptor_safe_flags()
    try:
        descriptor = os.open(workspace, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as exc:
        raise GraphIntegrityError("workspace must be a non-symlink directory") from exc
    try:
        if not stat.S_ISDIR(_stat_fd(descriptor).st_mode):
            raise GraphIntegrityError("workspace must be a non-symlink directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _stat_fd(descriptor: int) -> os.stat_result:
    return os.fstat(descriptor)


def _validate_relative_output(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise GraphIntegrityError("declared output path must be non-empty")
    if "\\" in value or ":" in value:
        raise GraphIntegrityError("declared output path must be POSIX-relative and portable")
    # Split the raw string ourselves: PurePosixPath normalizes "." and "//" away,
    # which would let "./x", "a//b", or "a/./b" pass a parts-based check.
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise GraphIntegrityError("declared output path must be relative, canonical, and traversal-free")
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or windows.anchor:
        raise GraphIntegrityError("declared output path must not be Windows-rooted")
