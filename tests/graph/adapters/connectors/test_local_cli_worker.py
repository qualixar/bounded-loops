"""LocalCliConnectorWorker — run an admitted local-CLI connector's CLI freely, or CAGED
under ALLOWLIST (RC Mode 1 + the ALLOWLIST caged path).

Hermetic: a stand-in CLI (a tiny shell/python script) stands in for a real agent CLI, so the
worker's mechanism — resolve, run with the child env, capture stdout as a content-addressed
artifact — is proven deterministically with no subscription and no quota. The truly-live caged
tests (which actually invoke `sandbox-exec`) are skipped unless this host has a real Seatbelt +
the loopback egress proxy, mirroring `test_sandboxed_worker.py`'s own live-test idiom.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import types

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CliInvocation,
    CliProfile,
    LocalCliConnectorWorker,
    StaticCliResolver,
)
from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.adapters.enforcement.egress_proxy import LoopbackEgressProxy
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkDestination, NetworkMode
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.plan import ResolvedBinding

_ORG, _PROJ = "o", "p"

_NO_CAGE = PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True)
_LIVE = probe_platform()
_needs_cage = pytest.mark.skipif(
    not (_LIVE.seatbelt and _LIVE.egress_proxy),
    reason="RC-LOCKDOWN loopback egress cage needs macOS Seatbelt",
)


def _plan(transport: str = "local_cli"):
    binding = ResolvedBinding(
        binding_id="binding-1", slot_id="model", connector_id="c", connector_version="1",
        connection_id="conn-1", admission_digest="sha256:" + "d" * 64, route_policy_digest="sha256:" + "e" * 64,
        provider_id="anthropic", model_target="claude", region="local", fallback=False, transport=transport,
    )
    return types.SimpleNamespace(connection_bindings=(binding,))


def _node(binding_id: str | None = "binding-1"):
    return types.SimpleNamespace(
        node_id="agent", binding_id=binding_id, required_effects=frozenset({Effect.WORKSPACE_WRITE}),
        isolation=IsolationLevel.PROCESS_RESTRICTED, hard_deadline_ms=15000,
    )


def _envelope(mode: NetworkMode = NetworkMode.OPEN):
    return ExecutionEnvelope(IsolationLevel.PROCESS_RESTRICTED, "local_cli", frozenset({Effect.WORKSPACE_WRITE}), mode, ())


def _allowlist_envelope(destinations: tuple[NetworkDestination, ...]):
    return ExecutionEnvelope(
        IsolationLevel.CONTAINER_RESTRICTED, "local_cli", frozenset({Effect.EXTERNAL_WRITE}),
        NetworkMode.ALLOWLIST, destinations,
    )


def _worker(tmp_path, resolver, environ=None, capabilities=None):
    return LocalCliConnectorWorker(
        identity=types.SimpleNamespace(run_id="run-1"),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        resolver=resolver, workspace_root=tmp_path / "work",
        organization_id=_ORG, project_id=_PROJ,
        environ=environ if environ is not None else {"PATH": os.environ.get("PATH", "")},
        capabilities=capabilities,
    )


def _read(store, digest) -> bytes:
    with store.open(ArtifactRef(digest, _ORG, _PROJ), ArtifactAccess(_ORG, _PROJ)) as handle:
        return handle.read()


def _standin(tmp_path: Path, body: str) -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text(body)
    cli.chmod(0o755)
    return str(cli)


def test_runs_a_local_cli_and_captures_its_reply(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\nprintf 'ECHO:'; cat\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="hello graph")))
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)
    assert len(result.output_artifact_digests) == 1
    assert result.observed_transport == "local_cli"
    assert result.observed_route is not None and result.observed_route.provider_id == "anthropic"
    assert _read(worker._store, result.output_artifact_digests[0]) == b"ECHO:hello graph"


def test_rejects_a_non_local_cli_node(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(_standin(tmp_path, "#!/bin/sh\ncat\n")), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="local-CLI"):
        worker.execute(plan=_plan(transport="api_proxy"), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)


def test_a_deny_network_envelope_is_refused(tmp_path):
    # OPEN and ALLOWLIST are the only two envelopes a local_cli connector supports; DENY (and
    # anything else) is refused — the message names both supported modes ("open-network"
    # remains a literal substring, so this also pins backward compat of the error text).
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(_standin(tmp_path, "#!/bin/sh\ncat\n")), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="open-network"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope(mode=NetworkMode.DENY), attempt=1, repair_round=0)


def test_missing_cli_binary_fails_closed(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/no/such/cli-xyz-404"), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="not installed"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)


def test_a_failing_cli_is_a_closed_node_failure(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="exited 3"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)


def test_failure_hint_redacts_a_secret_shaped_token(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\necho 'auth failed key sk-ant-abcdefghijklmnopqrstuvwxyz012345' >&2\nexit 1\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="x")))
    with pytest.raises(GraphIntegrityError) as caught:
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)
    message = str(caught.value)
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz012345" not in message
    assert "REDACTED" in message


def test_unset_env_is_applied_to_the_child(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\nprintf 'SECRET=%s' \"$SECRET_VAR\"\n")
    resolver = StaticCliResolver(CliInvocation(CliProfile(cli, unset_env=("SECRET_VAR",)), prompt=""))
    worker = _worker(tmp_path, resolver, environ={"PATH": os.environ.get("PATH", ""), "SECRET_VAR": "leaked"})
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)
    assert _read(worker._store, result.output_artifact_digests[0]) == b"SECRET="


def test_prompt_delivered_as_argument(tmp_path):
    cli = _standin(tmp_path, '#!/bin/sh\nprintf "ARG:%s" "$1"\n')
    resolver = StaticCliResolver(CliInvocation(CliProfile(cli, prompt_via="arg"), prompt="via-arg"))
    worker = _worker(tmp_path, resolver)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope(), attempt=1, repair_round=0)
    assert _read(worker._store, result.output_artifact_digests[0]) == b"ARG:via-arg"


def test_cli_profile_rejects_an_invalid_prompt_delivery():
    with pytest.raises(GraphValidationError):
        CliProfile("x", prompt_via="telepathy")


# ── ALLOWLIST caged path (DECISION CHANGE: real cage, reusing sandbox.py/egress_proxy.py) ──


def _python_standin(tmp_path: Path, code: str) -> str:
    # Any python3 on PATH will do — the probes use stdlib only (json/os/socket/sys) — and a
    # bare "python3" shebang avoids embedding a (possibly long) sys.executable path.
    cli = tmp_path / "standin_cli.py"
    cli.write_text(f"#!/usr/bin/env python3\n{code}")
    cli.chmod(0o755)
    return str(cli)


def test_allowlist_envelope_without_the_cage_fails_closed_before_launching(tmp_path):
    # Deterministic (injected capabilities): no Seatbelt/egress-proxy on this "host" -> refuse
    # BEFORE anything is launched, never silently fall back to open egress. The stand-in CLI
    # would print a tell-tale reply if it ran; it must not.
    cli = _standin(tmp_path, "#!/bin/sh\nprintf 'SHOULD NOT RUN'\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="x")), capabilities=_NO_CAGE)
    dest = (NetworkDestination(hostname="api.anthropic.com", port=443),)
    with pytest.raises(GraphIntegrityError, match="Seatbelt loopback-proxy cage"):
        worker.execute(plan=_plan(), node=_node(), envelope=_allowlist_envelope(dest), attempt=1, repair_round=0)


def test_caged_argv_builds_a_loopback_only_seatbelt_profile(tmp_path):
    # Pure argv/profile-shape check — no subprocess launched, portable to any host. Exercises
    # the SAME sandbox.py builder SandboxedNodeWorker/https already use, not a hand-rolled one.
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/bin/true"), prompt="x")))
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = {"HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"), "PATH": "/usr/bin"}
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    argv = worker._caged_argv(
        node=_node(), inner_argv=["/bin/true", "hello"], workdir=workdir, env=env, proxy_port=54321,
    )
    assert argv[0].endswith("sandbox-exec")
    profile = argv[2]  # [sandbox-exec, -p, <profile>, *inner_argv]
    assert "(deny network*)" in profile
    assert '(allow network-outbound (remote ip "localhost:54321"))' in profile
    assert '(deny file-write* (subpath "/"))' in profile
    assert f'(allow file-write* (subpath "{workdir}"))' in profile
    assert f'(allow file-write* (subpath "{tmp_path / "home"}"))' in profile  # real HOME writable
    assert argv[-2:] == ["/bin/true", "hello"]


def test_caged_argv_wires_the_proxy_env_vars(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/bin/true"), prompt="x")))
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"}
    (tmp_path / "home").mkdir(exist_ok=True)
    worker._caged_argv(node=_node(), inner_argv=["/bin/true"], workdir=workdir, env=env, proxy_port=9999)
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        assert env[var] == "http://127.0.0.1:9999"


def test_caged_argv_wraps_a_profile_build_failure_closed(tmp_path):
    # An unsafe HOME (quote/control characters _canonical() rejects) must fail closed with a
    # clear GraphIntegrityError, never let a bare ValueError escape uncaught, and never leave a
    # started proxy unaccounted for at this layer (the caller's `finally` owns that; this method
    # itself must not swallow the failure).
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/bin/true"), prompt="x")))
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = {"HOME": '/tmp/ev"il-home', "PATH": "/usr/bin"}
    with pytest.raises(GraphIntegrityError, match="could not build the egress cage"):
        worker._caged_argv(node=_node(), inner_argv=["/bin/true"], workdir=workdir, env=env, proxy_port=1111)


def test_caged_argv_falls_back_to_real_home_when_env_lacks_it(tmp_path, monkeypatch):
    # env may not carry HOME explicitly (e.g. a minimal PATH-only environ); the cage must still
    # resolve a real, writable HOME rather than build a profile with no HOME entry at all.
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    (tmp_path / "real-home").mkdir(exist_ok=True)
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/bin/true"), prompt="x")))
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    argv = worker._caged_argv(node=_node(), inner_argv=["/bin/true"], workdir=workdir, env={"PATH": "/usr/bin"}, proxy_port=1234)
    assert f'(allow file-write* (subpath "{tmp_path / "real-home"}"))' in argv[2]


@_needs_cage
def test_live_allowlist_cages_the_cli_to_only_the_loopback_proxy(tmp_path):
    code = (
        "import json, os, socket, sys\n"
        "sys.stdin.read()\n"
        "proxy = os.environ.get('HTTPS_PROXY', '')\n"
        "port = int(proxy.rsplit(':', 1)[1]) if proxy.count(':') >= 2 else 0\n"
        "def _try(addr, p):\n"
        "    try:\n"
        "        s = socket.socket(); s.settimeout(2); s.connect((addr, p)); s.close(); return 'reachable'\n"
        "    except PermissionError:\n"
        "        return 'denied_by_sandbox'\n"
        "    except OSError as e:\n"
        "        return 'denied_by_sandbox' if e.errno == 1 else ('refused' if e.errno == 61 else 'err:%s' % e.errno)\n"
        "res = {'proxy': proxy, 'to_proxy': _try('127.0.0.1', port), 'to_other': _try('127.0.0.1', 1),\n"
        "       'home_readable': os.path.isdir(os.environ.get('HOME', ''))}\n"
        "print(json.dumps(res))\n"
    )
    cli = _python_standin(tmp_path, code)
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="hello")))
    dest = (NetworkDestination(hostname="api.example.com", port=443),)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_allowlist_envelope(dest), attempt=1, repair_round=0)
    payload = json.loads(_read(worker._store, result.output_artifact_digests[0]))
    assert payload["proxy"].startswith("http://127.0.0.1:"), payload
    assert payload["to_proxy"] == "reachable", payload           # the loopback proxy IS reachable
    assert payload["to_other"] == "denied_by_sandbox", payload   # every other egress is caged
    # The whole point of ALLOWLIST-caged local_cli: the REAL HOME (login config) stays readable —
    # never an isolated empty HOME, which would break the subscription login entirely.
    assert payload["home_readable"] is True, payload


@_needs_cage
def test_live_allowlist_completes_a_real_connect_handshake_and_distinguishes_the_allowlist(tmp_path, caplog):
    # Complements the raw-socket probe above with the FULL protocol: a real HTTP CONNECT
    # request through the proxy, from inside the cage — proving a well-behaved HTTP client
    # (not just a raw TCP probe) reaches the proxy's CONNECT handler over both IPv4 and the
    # dual-bound ::1, and that the PROXY ITSELF (not just Seatbelt) evaluates the allowlist
    # BEFORE ever resolving/dialing anywhere — never a real egress attempt to an unlisted host.
    # Uses a non-resolving destination deliberately (no real external network call at all,
    # per this host's egress policy) and distinguishes admitted-but-unresolvable from
    # not-on-the-allowlist via the proxy's own decision log, not the wire response (both are
    # 403 Forbidden on the wire — the proxy never reveals WHY over the tunnel itself).
    code = (
        "import json, socket, sys\n"
        "sys.stdin.read()\n"
        "port = int(__import__('os').environ.get('HTTPS_PROXY', '').rsplit(':', 1)[1])\n"
        "def _connect(family, addr, target):\n"
        "    try:\n"
        "        s = socket.socket(family, socket.SOCK_STREAM); s.settimeout(3); s.connect((addr, port))\n"
        "        s.sendall(('CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n' % (target, target)).encode())\n"
        "        head = s.recv(200); s.close(); return head.split(b'\\r\\n')[0].decode('ascii', 'replace')\n"
        "    except OSError as e:\n"
        "        return 'denied_by_sandbox' if e.errno == 1 else str(e)\n"
        "res = {\n"
        "    'admitted_v4': _connect(socket.AF_INET, '127.0.0.1', 'api.example.com:443'),\n"
        "    'admitted_v6': _connect(socket.AF_INET6, '::1', 'api.example.com:443'),\n"
        "    'unlisted_v4': _connect(socket.AF_INET, '127.0.0.1', 'evil.example.com:443'),\n"
        "}\n"
        "print(json.dumps(res))\n"
    )
    cli = _python_standin(tmp_path, code)
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="hello")))
    dest = (NetworkDestination(hostname="api.example.com", port=443),)
    with caplog.at_level("WARNING"):
        result = worker.execute(plan=_plan(), node=_node(), envelope=_allowlist_envelope(dest), attempt=1, repair_round=0)
    payload = json.loads(_read(worker._store, result.output_artifact_digests[0]))
    print(f"\n[EMPIRICAL FIX-verify] CONNECT handshake from inside the ALLOWLIST cage: {payload!r}")
    # Both are refused on the wire (this fake domain resolves nowhere real) — the point is
    # the CONNECT handshake itself reached the proxy over BOTH families, proving a real HTTP
    # client (not just a raw socket) can drive the full protocol from inside the cage.
    assert "403 Forbidden" in payload["admitted_v4"], payload
    assert "403 Forbidden" in payload["admitted_v6"], payload
    assert "403 Forbidden" in payload["unlisted_v4"], payload
    # The proxy's OWN decision log proves it evaluated the allowlist distinctly per host:
    # admitted-but-unresolvable reached resolution; the unlisted host never did.
    messages = "\n".join(caplog.messages)
    assert "api.example.com:443" in messages and "could not be resolved" in messages
    assert "evil.example.com:443" in messages and "not on the admitted allowlist" in messages


@_needs_cage
def test_live_caged_cli_blocked_from_a_non_allowlisted_host_fails_closed_with_redacted_diagnostic(tmp_path):
    code = (
        "import socket, sys\n"
        "sys.stdin.read()\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 1)); s.close()\n"
        "    print('UNEXPECTED: reached a non-allowlisted destination', file=sys.stderr)\n"
        "except OSError:\n"
        "    print('network error reaching vendor api key sk-ant-abcdefghijklmnopqrstuvwxyz012345', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    cli = _python_standin(tmp_path, code)
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="hello")))
    dest = (NetworkDestination(hostname="api.example.com", port=443),)
    with pytest.raises(GraphIntegrityError) as caught:
        worker.execute(plan=_plan(), node=_node(), envelope=_allowlist_envelope(dest), attempt=1, repair_round=0)
    message = str(caught.value)
    assert "exited 1" in message
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz012345" not in message  # never a silent/leaked reply
    assert "REDACTED" in message


# ── hardening pass: DNS robustness, NO_PROXY, UDS empirical check ──────────────


@_needs_cage
def test_live_allowlist_blocks_dns_resolution_for_a_real_resolvable_host(tmp_path):
    # github.com DOES resolve outside the cage — verified HERE, first, outside the cage, so a
    # pass below means "the cage blocked it," never "the name doesn't exist" (the exact
    # ambiguity that made the original claim unreliable). Skip (not a false pass) if this
    # network genuinely cannot resolve it at all.
    try:
        socket.getaddrinfo("github.com", 443, type=socket.SOCK_STREAM)
    except OSError:
        pytest.skip("github.com does not resolve on this host/network right now — cannot "
                     "distinguish 'cage blocked it' from 'name does not exist' here")
    code = (
        "import json, socket, sys\n"
        "sys.stdin.read()\n"
        "try:\n"
        "    socket.getaddrinfo('github.com', 443, type=socket.SOCK_STREAM)\n"
        "    res = {'resolution': 'succeeded'}\n"
        "except PermissionError as e:\n"
        "    res = {'resolution': 'denied_by_sandbox (PermissionError): %s' % e}\n"
        "except OSError as e:\n"
        "    res = {'resolution': 'OSError errno=%s: %s' % (e.errno, e)}\n"
        "print(json.dumps(res))\n"
    )
    cli = _python_standin(tmp_path, code)
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="hello")))
    dest = (NetworkDestination(hostname="api.example.com", port=443),)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_allowlist_envelope(dest), attempt=1, repair_round=0)
    payload = json.loads(_read(worker._store, result.output_artifact_digests[0]))
    print(f"\n[EMPIRICAL FIX 1] getaddrinfo('github.com') inside the ALLOWLIST cage: "
          f"{payload['resolution']!r}")
    assert payload["resolution"] != "succeeded", payload


def test_caged_argv_clears_no_proxy_from_the_child_env(tmp_path):
    # FIX 2 (Grok M3): a cooperating client honoring a pre-existing NO_PROXY/no_proxy could
    # attempt a direct connect for an excluded host — Seatbelt EPERMs it either way (not a
    # bypass), but clearing it gives a cleaner, less confusing fail mode.
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/bin/true"), prompt="x")))
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = {
        "HOME": str(tmp_path / "home"), "PATH": "/usr/bin",
        "NO_PROXY": "vendor.example.com", "no_proxy": "vendor.example.com",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    worker._caged_argv(node=_node(), inner_argv=["/bin/true"], workdir=workdir, env=env, proxy_port=8888)
    assert "NO_PROXY" not in env
    assert "no_proxy" not in env


@_needs_cage
def test_live_allowlist_unix_domain_socket_connect_behavior_is_empirically_recorded(tmp_path):
    # FIX 5: resolves the Grok/Muse disagreement empirically rather than by argument — does
    # the Seatbelt ALLOWLIST cage's `(deny network*)` also cover AF_UNIX connect (Grok), or is
    # a co-resident Unix-socket listener an uncaged exfil path (Muse)? A REAL listener is bound
    # under workdir (writable + reachable — reads are unconfined) BEFORE the caged child runs,
    # so a "connected" result would mean an ACTUAL local IPC channel was reachable, not merely
    # that the socket path existed.
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    # AF_UNIX paths are capped at ~104 bytes on macOS — pytest's tmp_path nesting is far too
    # long, so the listener socket (reachability only requires path traversal, never write
    # access, so it need not live under workdir) uses a short, dedicated directory instead.
    socket_dir = tempfile.mkdtemp(prefix="bl-uds-")
    socket_path = Path(socket_dir) / "s.sock"

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    try:
        code = (
            "import json, socket, sys\n"
            "try:\n"
            "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2)\n"
            "    s.connect(sys.argv[1]); s.close(); result = 'connected'\n"
            "except PermissionError as e:\n"
            "    result = 'denied_by_sandbox (PermissionError): %s' % e\n"
            "except OSError as e:\n"
            "    result = 'OSError errno=%s: %s' % (e.errno, e)\n"
            "print(json.dumps({'uds_connect': result}))\n"
        )
        cli = tmp_path / "uds_probe.py"
        cli.write_text(f"#!/usr/bin/env python3\n{code}")
        cli.chmod(0o755)

        worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(str(cli)), prompt="")))
        env = worker._child_env(CliProfile(str(cli)))
        proxy = LoopbackEgressProxy(allowed=(NetworkDestination(hostname="api.example.com", port=443),))
        proxy_port = proxy.start()
        try:
            argv = worker._caged_argv(
                node=_node(), inner_argv=[str(cli), str(socket_path)], workdir=workdir, env=env, proxy_port=proxy_port,
            )
            proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True, timeout=10)
        finally:
            proxy.stop()
    finally:
        listener.close()
        shutil.rmtree(socket_dir, ignore_errors=True)

    payload = json.loads(proc.stdout.strip())
    observed = payload["uds_connect"]
    print(f"\n[EMPIRICAL FIX 5] AF_UNIX connect to a real co-resident listener under the "
          f"ALLOWLIST cage: {observed!r}")
    # RESOLVED (empirically, live, against a REAL listener — not argued): Grok was right.
    # `(deny network*)` covers AF_UNIX connect too; Muse's "uncaged co-resident helper" claim
    # does not hold on this host. Locked in as a real regression-catching assertion, not just
    # a printed observation — see docs/graph-egress-posture.md's Known limitation section.
    assert observed.startswith("denied_by_sandbox"), payload
