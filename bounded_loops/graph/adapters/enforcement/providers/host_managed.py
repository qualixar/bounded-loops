"""host_managed provider — defer to an ambient host sandbox, PROVING it first.

When the engine runs inside Claude Code / Codex / OpenShell, the host already
confines the worker's child processes, so wrapping again is redundant. But
deferring on env markers is a downgrade attack (markers are forgeable and a host
may sandbox only *its own* tool subprocess, not our later worker). So this
provider claims a tier ONLY after a **live negative probe on a child launched
exactly as the worker would**: an out-of-workspace write and a loopback socket
open must FAIL. If the probe shows no ambient confinement, the provider is
unavailable and the registry falls through to `native`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

from bounded_loops.adapters._env import build_subprocess_env
from bounded_loops.adapters.runners.process_lifecycle import ProcessTurn
from bounded_loops.domain.models import TurnState
from bounded_loops.graph.adapters.enforcement.provider import (
    Availability,
    Control,
    EnforcedControls,
    LaunchSpec,
)
from bounded_loops.graph.application.execution_policy import NetworkMode
from bounded_loops.graph.domain.authoring import IsolationLevel

# Reports whether an out-of-workspace write and a loopback socket are DENIED by
# the ambient host. Runs UNWRAPPED so it observes exactly the confinement our
# worker's children would inherit. errno 1 (EPERM) => sandbox-denied; errno 61
# (ECONNREFUSED) => network reachable (not confined).
_PROBE = (
    "import json, os, socket, sys\n"
    "ws = sys.argv[1]\n"
    "res = {}\n"
    "try:\n"
    "    p = os.path.join(ws, '..', 'hm_probe_%d.tmp' % os.getpid())\n"
    "    open(p, 'w').write('x'); os.unlink(p); res['oow_write'] = 'allowed'\n"
    "except Exception:\n"
    "    res['oow_write'] = 'denied'\n"
    "try:\n"
    "    s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 1)); s.close(); res['socket'] = 'allowed'\n"
    "except PermissionError:\n"
    "    res['socket'] = 'denied'\n"
    "except OSError as e:\n"
    "    res['socket'] = 'denied' if e.errno in (1, 13) else 'allowed'\n"
    "print(json.dumps(res))\n"
)


class HostManagedProvider:
    provider_id = "host_managed"

    def __init__(self, *, python: str | None = None) -> None:
        self._python = python or sys.executable
        # (fs_write, net) observed per RESOLVED workspace — ambient confinement is
        # verified against the exact path the node will run in, never reused blindly
        # across workspaces.
        self._cached: dict[str, tuple[Control, Control]] = {}

    def _run_probe(self, workspace: Path) -> tuple[Control, Control] | None:
        """Return (fs_write, net) observed ambient controls, or None if the probe
        could not run (then we cannot claim confinement → fail toward native)."""
        try:
            turn = ProcessTurn.start(
                [self._python, "-I", "-B", "-c", _PROBE, str(workspace)],
                cwd=workspace,
                env=build_subprocess_env(),
                output_limit_bytes=8192,
            )
            result = turn.wait(timeout_s=10.0)
        except Exception:  # noqa: BLE001
            return None
        if result.state is not TurnState.COMPLETED:
            return None
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        fs_write = Control.ENFORCED if data.get("oow_write") == "denied" else Control.NOT_ENFORCED
        net = Control.ENFORCED if data.get("socket") == "denied" else Control.NOT_ENFORCED
        return (fs_write, net)

    def probe(
        self, *, tier: IsolationLevel, network_mode: NetworkMode, workspace: Path | None = None,
    ) -> Availability:
        if network_mode is NetworkMode.ALLOWLIST:
            return Availability(False, "host_managed cannot provide authorized-egress", EnforcedControls())
        if workspace is None:
            return Availability(False, "host_managed requires a workspace to run its confinement probe", EnforcedControls())
        key = str(workspace.resolve())
        observed = self._cached.get(key)
        if observed is None:
            observed = self._run_probe(workspace)
            if observed is None:
                return Availability(False, "host_managed confinement probe could not run", EnforcedControls())
            self._cached[key] = observed
        fs_write, net = observed
        if fs_write is not Control.ENFORCED and net is not Control.ENFORCED:
            return Availability(
                False, "no ambient host confinement detected (out-of-workspace write and socket both succeeded)",
                EnforcedControls(fs_write=fs_write, net=net),
            )
        controls = EnforcedControls(
            net=net, fs_write=fs_write, fs_read=Control.UNKNOWN, pid=Control.UNKNOWN,
            user=Control.UNKNOWN, kernel=Control.UNKNOWN, egress=Control.NOT_ENFORCED,
            notes=(
                "deferred to the ambient host sandbox; confinement PROVEN by a live negative probe "
                f"(out-of-workspace write={fs_write.value}, loopback socket={net.value}); "
                "dimensions the host manages are UNKNOWN to us, not claimed",
            ),
        )
        return Availability(True, "", controls)

    def build_launch(
        self,
        *,
        inner_argv: Sequence[str],
        workspace: Path,
        home: Path,
        tmpdir: Path,
        tier: IsolationLevel,
        network_mode: NetworkMode,
    ) -> LaunchSpec:
        # Defer to the host sandbox: run the node unwrapped (the host confines it,
        # proven by the probe the registry ran before selecting us).
        return LaunchSpec(kind="local", argv=tuple(inner_argv))
