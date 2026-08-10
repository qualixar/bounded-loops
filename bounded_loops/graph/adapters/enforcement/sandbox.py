"""Native OS sandbox mechanisms — real isolation WITHOUT Docker or root.

Most operators run graph nodes from a Claude Code / Codex / Antigravity CLI on a
laptop with no Docker daemon. This module turns an inner command into a wrapped
argv that a plain POSIX host can still enforce:

* macOS  -> ``sandbox-exec`` (Seatbelt): ``(deny network*)`` denies outbound
  sockets and ``(deny file-write* (subpath "/"))`` confines writes to the
  workspace / HOME / TMPDIR. No root, no daemon.
* Linux  -> ``bubblewrap`` (rootless user namespaces): an isolated network
  namespace (no external interfaces) plus a read-only root filesystem with only
  the workspace / HOME / TMPDIR writable; or ``unshare -n`` as a
  network-namespace-only fallback.
* Docker -> a hardened ``docker run`` for hosts that prefer or require a
  container (one option among several — never the only path to isolation).

Empirically verified on macOS that a Seatbelt profile of
``(allow default)(deny network*)(deny file-write* (subpath "/"))(allow ...)``
runs an interpreter while an outbound socket and an out-of-workspace write both
fail with ``EPERM``. Reads are intentionally NOT confined by the Seatbelt
profile: the material controls this tier promises are network-deny and
write-confinement, and that limitation is published honestly by the capability
matrix rather than hidden.

These builders are pure: they compute argv/profile strings and never spawn a
process or touch a daemon, so they are deterministic and unit-testable on any
platform. Launching is the worker's job.
"""

from __future__ import annotations

from enum import Enum
import os
from pathlib import Path
import re
from typing import Sequence

from bounded_loops.graph.application.execution_policy import NetworkMode

SEATBELT_BINARY = "/usr/bin/sandbox-exec"
# A container image is digest-pinned only if it ends in @sha256:<exactly 64 hex>.
_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}$")
# The only device nodes a sandboxed runtime may legitimately WRITE to — never
# all of /dev, which would expose raw disk / bpf devices.
_WRITABLE_DEVICES = ("/dev/null", "/dev/zero", "/dev/dtracehelper", "/dev/tty", "/dev/random", "/dev/urandom")


def is_digest_pinned(image: object) -> bool:
    """True iff *image* is a non-flag, digest-pinned ref (``name@sha256:<64 hex>``).

    Providers use this to fail closed at probe time — declining honestly — rather
    than only discovering an unpinned (mutable-tag) image when the launch is built.
    """
    return isinstance(image, str) and not image.startswith("-") and _DIGEST_PIN.search(image) is not None


class SandboxMechanism(str, Enum):
    """A concrete mechanism that can deliver a required isolation tier."""

    NONE = "none"  # floor only: process group + rlimits + scrubbed env + isolated HOME
    SEATBELT = "seatbelt"  # macOS sandbox-exec
    BUBBLEWRAP = "bubblewrap"  # Linux rootless namespaces
    UNSHARE_NET = "unshare_net"  # Linux network-namespace-only fallback
    DOCKER = "docker"  # containerized isolation


def _canonical(path: Path) -> str:
    """Return the realpath as a string, rejecting characters that could break
    out of a Seatbelt ``(subpath "...")`` literal or a docker mount spec."""
    resolved = os.path.realpath(path)
    if any(bad in resolved for bad in ('"', "\n", "\r", "\x00", "\\")):
        raise ValueError("sandbox path must not contain quotes or control characters")
    return resolved


def build_seatbelt_profile(*, writable: Sequence[Path], deny_network: bool) -> str:
    """Build a Seatbelt profile that denies network and confines writes.

    ``(allow default)`` keeps arbitrary interpreters runnable; the two explicit
    denials are the controls this tier actually guarantees.
    """
    lines = ["(version 1)", "(allow default)"]
    if deny_network:
        lines.append("(deny network*)")
    lines.append('(deny file-write* (subpath "/"))')
    for path in writable:
        lines.append(f'(allow file-write* (subpath "{_canonical(path)}"))')
    # Only specific device nodes — NOT all of /dev (raw disk / bpf would break
    # the write-confinement guarantee).
    for device in _WRITABLE_DEVICES:
        lines.append(f'(allow file-write* (literal "{device}"))')
    return "\n".join(lines)


def seatbelt_argv(profile: str, inner_argv: Sequence[str]) -> list[str]:
    if not inner_argv:
        raise ValueError("inner argv must not be empty")
    return [SEATBELT_BINARY, "-p", profile, *inner_argv]


def bubblewrap_argv(
    *,
    inner_argv: Sequence[str],
    workspace: Path,
    home: Path,
    tmpdir: Path,
    deny_network: bool,
) -> list[str]:
    if not inner_argv:
        raise ValueError("inner argv must not be empty")
    work, hm, tmp = _canonical(workspace), _canonical(home), _canonical(tmpdir)
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", work, work,
        "--bind", hm, hm,
        "--bind", tmp, tmp,
        "--chdir", work,
        "--unshare-user", "--unshare-ipc", "--unshare-pid",
        "--unshare-uts", "--unshare-cgroup",
        "--die-with-parent", "--new-session",
    ]
    argv.append("--unshare-net" if deny_network else "--share-net")
    argv.append("--")
    argv.extend(inner_argv)
    return argv


def unshare_net_argv(inner_argv: Sequence[str]) -> list[str]:
    if not inner_argv:
        raise ValueError("inner argv must not be empty")
    return ["unshare", "-n", "--", *inner_argv]


def docker_argv(
    *,
    image: str,
    inner_argv: Sequence[str],
    workspace: Path,
    deny_network: bool,
    cpus: str = "1.0",
    memory: str = "1g",
    pids_limit: str = "256",
) -> list[str]:
    if not is_digest_pinned(image):
        raise ValueError("container image must be a non-flag, digest-pinned ref (name@sha256:<64 hex>)")
    if not inner_argv:
        raise ValueError("inner argv must not be empty")
    source = _canonical(workspace)
    if ":" in source or "," in source:
        raise ValueError("workspace path must not contain ':' or ',' for a container bind mount")
    argv = [
        "docker", "run", "--rm", "-i",
        "--network", "none" if deny_network else "bridge",
        "--cpus", cpus,
        "--memory", memory,
        "--pids-limit", pids_limit,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--stop-timeout", "1",
        "--mount", f"type=bind,source={source},target=/workspace",
        "-w", "/workspace",
    ]
    uid = getattr(os, "getuid", lambda: None)()
    gid = getattr(os, "getgid", lambda: None)()
    if uid is not None and gid is not None:
        argv.extend(["--user", f"{uid}:{gid}"])
    argv.append(image)
    argv.extend(inner_argv)
    return argv


def wrap_argv(
    mechanism: SandboxMechanism,
    *,
    inner_argv: Sequence[str],
    workspace: Path,
    home: Path,
    tmpdir: Path,
    network_mode: NetworkMode,
    image: str | None = None,
) -> list[str]:
    """Wrap *inner_argv* in the selected mechanism, denying network when the
    envelope's network mode is ``DENY``.

    Authorized (allowlist) egress is not implemented, so this refuses to build a
    network-open wrapper: it never silently opens the network. The capability
    matrix already fails closed on allowlist before reaching here; this is the
    second, independent guard against that fail-open.
    """
    if network_mode is NetworkMode.ALLOWLIST:
        raise ValueError("authorized (allowlist) egress is not implemented; refusing to open the network")
    deny = network_mode is NetworkMode.DENY
    if mechanism is SandboxMechanism.NONE:
        return list(inner_argv)
    if mechanism is SandboxMechanism.SEATBELT:
        profile = build_seatbelt_profile(writable=[workspace, home, tmpdir], deny_network=deny)
        return seatbelt_argv(profile, inner_argv)
    if mechanism is SandboxMechanism.BUBBLEWRAP:
        return bubblewrap_argv(
            inner_argv=inner_argv, workspace=workspace, home=home, tmpdir=tmpdir, deny_network=deny,
        )
    if mechanism is SandboxMechanism.UNSHARE_NET:
        return unshare_net_argv(inner_argv)
    if mechanism is SandboxMechanism.DOCKER:
        if not image:
            raise ValueError("docker mechanism requires a digest-pinned image")
        return docker_argv(image=image, inner_argv=inner_argv, workspace=workspace, deny_network=deny)
    raise ValueError(f"unknown sandbox mechanism: {mechanism!r}")
