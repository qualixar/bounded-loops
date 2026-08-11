"""E2.2 — the sandboxed node worker executes real work WITHOUT Docker.

The live tests run only where a native sandbox exists (macOS Seatbelt here) and
assert the two guarantees empirically: an outbound socket is denied and a write
outside the workspace is denied, while the declared output is still promoted.
The fail-closed tests inject capabilities so they are deterministic everywhere.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import (
    PlatformCapabilities,
    probe_platform,
)
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkDestination, NetworkMode
from bounded_loops.graph.application.sandboxed_worker import (
    NodeExecutionSpec,
    SandboxedNodeWorker,
)
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.adapters.enforcement.provider import Control
from bounded_loops.graph.domain.errors import GraphIntegrityError

_ORG, _PROJ = "org-1", "proj-1"

_PROBE = (
    "import json, os, socket\n"
    "net = 'unknown'\n"
    "try:\n"
    "    s = socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 1)); s.close(); net = 'reachable'\n"
    "except PermissionError:\n"
    "    net = 'denied_by_sandbox'\n"
    "except OSError as e:\n"
    "    net = 'denied_by_sandbox' if e.errno == 1 else ('refused' if e.errno == 61 else 'err:%s' % e.errno)\n"
    "open('result.json', 'w').write(json.dumps({'network': net, 'home': os.environ.get('HOME')}))\n"
)
_WRITE_OUTSIDE = (
    "import json, os\n"
    "outside = 'denied'\n"
    "try:\n"
    "    open(os.path.join(os.environ['HOME'], '..', '..', 'escape.txt'), 'w').write('x'); outside = 'wrote'\n"
    "except OSError:\n"
    "    outside = 'denied'\n"
    "open('result.json', 'w').write(json.dumps({'outside': outside}))\n"
)


_ALLOWLIST_PROBE = (
    "import json, os, socket\n"
    "proxy = os.environ.get('HTTPS_PROXY', '')\n"
    "port = int(proxy.rsplit(':', 1)[1]) if proxy.count(':') >= 2 else 0\n"
    "def _try(family, addr, p):\n"
    "    try:\n"
    "        s = socket.socket(family); s.settimeout(2); s.connect((addr, p)); s.close(); return 'reachable'\n"
    "    except PermissionError:\n"
    "        return 'denied_by_sandbox'\n"
    "    except OSError as e:\n"
    "        return 'denied_by_sandbox' if e.errno == 1 else ('refused' if e.errno == 61 else 'err:%s' % e.errno)\n"
    "res = {'proxy': proxy,\n"
    "       'to_proxy_v4': _try(socket.AF_INET, '127.0.0.1', port),\n"
    "       'to_proxy_v6': _try(socket.AF_INET6, '::1', port),\n"
    "       'to_other': _try(socket.AF_INET, '127.0.0.1', 1)}\n"
    "open('result.json', 'w').write(json.dumps(res))\n"
)


class _Resolver:
    def __init__(self, spec: NodeExecutionSpec) -> None:
        self._spec = spec

    def resolve(self, node) -> NodeExecutionSpec:  # noqa: ANN001
        return self._spec


def _node(level=IsolationLevel.CONTAINER_RESTRICTED, effects=(Effect.WORKSPACE_WRITE,)):
    return types.SimpleNamespace(
        node_id="probe",
        binding_id=None,
        required_effects=frozenset(effects),
        isolation=level,
        hard_deadline_ms=15000,
    )


def _plan():
    return types.SimpleNamespace(connection_bindings=())


def _envelope(level=IsolationLevel.CONTAINER_RESTRICTED, effects=(Effect.WORKSPACE_WRITE,)):
    return ExecutionEnvelope(
        isolation=level,
        transport=None,
        allowed_effects=frozenset(effects),
        network_mode=NetworkMode.DENY,
        network_destinations=(),
    )


def _worker(tmp_path, resolver, caps):
    return SandboxedNodeWorker(
        identity=types.SimpleNamespace(run_id="run-1"),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        resolver=resolver,
        capabilities=caps,
        workspace_root=tmp_path / "work",
        organization_id=_ORG,
        project_id=_PROJ,
    )


def _read_artifact(store, digest):
    with store.open(ArtifactRef(digest, _ORG, _PROJ), ArtifactAccess(_ORG, _PROJ)) as handle:
        return json.loads(handle.read().decode("utf-8"))


_LIVE = probe_platform()
_needs_native = pytest.mark.skipif(
    not (_LIVE.seatbelt or _LIVE.bubblewrap),
    reason="no native OS sandbox (Seatbelt/bubblewrap) on this host",
)


@_needs_native
def test_live_sandbox_denies_network_and_isolates_home(tmp_path):
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c", _PROBE),
        declared_outputs={"result.json": "application/json"},
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope())

    assert len(result.output_artifact_digests) == 1
    assert result.observed_route is None and result.observed_transport is None
    payload = _read_artifact(worker.artifact_store, result.output_artifact_digests[0])
    assert payload["network"] == "denied_by_sandbox", payload
    # HOME must be the per-node isolated home, never the operator's real HOME.
    assert payload["home"] and "/work/run-1/probe-" in payload["home"]
    assert worker.mechanism_for("probe") in {"seatbelt", "bubblewrap"}
    # E3 receipt: the honest provider id + per-dimension controls are published.
    assert worker.provider_for("probe") == "native"
    controls = worker.controls_for("probe")
    assert controls is not None
    assert controls.net is Control.ENFORCED and controls.fs_write is Control.ENFORCED
    assert controls.kernel is Control.NOT_ENFORCED  # native Seatbelt shares the host kernel
    assert result.isolation_provider_id == "native"
    assert result.enforced_controls is not None
    assert result.enforced_controls["net"] == "enforced"
    assert result.enforced_controls["kernel"] == "not_enforced"


@pytest.mark.skipif(not _LIVE.seatbelt, reason="RC-LOCKDOWN loopback egress cage needs macOS Seatbelt")
def test_live_allowlist_cages_egress_to_the_loopback_proxy(tmp_path):
    # REAL end-to-end (loopback only, no external egress): under ALLOWLIST the process must reach the
    # loopback egress proxy AND NOTHING ELSE — the OS cage is the enforcement, the proxy is the allowlist.
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c", _ALLOWLIST_PROBE),
        declared_outputs={"result.json": "application/json"},
    )
    envelope = ExecutionEnvelope(
        isolation=IsolationLevel.CONTAINER_RESTRICTED,
        transport=None,
        allowed_effects=frozenset({Effect.EXTERNAL_WRITE}),
        network_mode=NetworkMode.ALLOWLIST,
        network_destinations=(NetworkDestination("api.example.com", 443),),
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    result = worker.execute(
        plan=_plan(),
        node=_node(level=IsolationLevel.CONTAINER_RESTRICTED, effects=(Effect.EXTERNAL_WRITE,)),
        envelope=envelope,
    )
    payload = _read_artifact(worker.artifact_store, result.output_artifact_digests[0])
    assert payload["proxy"].startswith("http://127.0.0.1:"), payload  # proxy env injected
    assert payload["to_proxy_v4"] == "reachable", payload             # IPv4 loopback hole IS our proxy
    # The Seatbelt `localhost` token also admits ::1:port; our proxy dual-binds it, so the child hits
    # OUR proxy there too — not a co-resident colluder (dual-audit MAJOR-1 closed).
    assert payload["to_proxy_v6"] == "reachable", payload
    assert payload["to_other"] == "denied_by_sandbox", payload        # every other egress is caged
    controls = worker.controls_for("probe")
    assert controls is not None
    assert controls.egress is Control.ENFORCED and controls.net is Control.ENFORCED


@_needs_native
def test_live_sandbox_confines_writes_outside_workspace(tmp_path):
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c", _WRITE_OUTSIDE),
        declared_outputs={"result.json": "application/json"},
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope())
    payload = _read_artifact(worker.artifact_store, result.output_artifact_digests[0])
    assert payload["outside"] == "denied", payload


def test_fail_closed_when_no_mechanism_can_enforce(tmp_path):
    caps = PlatformCapabilities(
        platform="linux", docker_available=False, process_groups=True, rlimits=True,
    )
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-c", "open('result.json','w').write('x')"),
        declared_outputs={"result.json": "text/plain"},
    )
    worker = _worker(tmp_path, _Resolver(spec), caps)
    with pytest.raises(GraphIntegrityError):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())


@_needs_native
def test_missing_declared_output_fails_closed(tmp_path):
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c", "pass"),
        declared_outputs={"result.json": "application/json"},
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    with pytest.raises(GraphIntegrityError):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())


@_needs_native
def test_undeclared_output_is_rejected(tmp_path):
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c",
              "open('result.json','w').write('{}'); open('extra.txt','w').write('x')"),
        declared_outputs={"result.json": "application/json"},
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    with pytest.raises(GraphIntegrityError):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())


def test_spec_requires_argv_and_outputs():
    with pytest.raises(GraphIntegrityError):
        NodeExecutionSpec(argv=(), declared_outputs={"a": "text/plain"})
    with pytest.raises(GraphIntegrityError):
        NodeExecutionSpec(argv=("/bin/true",), declared_outputs={})


def test_spec_requires_absolute_argv0():
    with pytest.raises(GraphIntegrityError):
        NodeExecutionSpec(argv=("python3", "-c", "pass"), declared_outputs={"a": "text/plain"})


@_needs_native
def test_crafted_node_id_cannot_escape_workspace_root(tmp_path):
    """A node_id full of traversal characters is reduced to a safe component and
    the per-node workspace still resolves inside the controller-owned root."""
    spec = NodeExecutionSpec(
        argv=(sys.executable, "-I", "-B", "-c", "open('result.json','w').write('{}')"),
        declared_outputs={"result.json": "application/json"},
    )
    worker = _worker(tmp_path, _Resolver(spec), _LIVE)
    evil = types.SimpleNamespace(
        node_id="../../../../etc/evil",
        binding_id=None,
        required_effects=frozenset({Effect.WORKSPACE_WRITE}),
        isolation=IsolationLevel.CONTAINER_RESTRICTED,
        hard_deadline_ms=15000,
    )
    result = worker.execute(plan=_plan(), node=evil, envelope=_envelope())
    assert len(result.output_artifact_digests) == 1
    # Nothing was created outside the workspace root.
    assert not (tmp_path / "etc").exists()
    created = list((tmp_path / "work" / "run-1").iterdir())
    assert all((tmp_path / "work").resolve() in d.resolve().parents for d in created)
