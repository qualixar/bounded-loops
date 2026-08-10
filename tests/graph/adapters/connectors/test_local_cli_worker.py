"""LocalCliConnectorWorker — run an admitted local-CLI connector's CLI freely (RC Mode 1).

Hermetic: a stand-in CLI (a tiny shell script) stands in for a real agent CLI, so the worker's
mechanism — resolve, run with the child env, capture stdout as a content-addressed artifact — is
proven deterministically with no subscription and no quota.
"""

from __future__ import annotations

import os
from pathlib import Path
import types

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CliInvocation,
    CliProfile,
    LocalCliConnectorWorker,
    StaticCliResolver,
)
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.plan import ResolvedBinding

_ORG, _PROJ = "o", "p"


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


def _worker(tmp_path, resolver, environ=None):
    return LocalCliConnectorWorker(
        identity=types.SimpleNamespace(run_id="run-1"),
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        resolver=resolver, workspace_root=tmp_path / "work",
        organization_id=_ORG, project_id=_PROJ,
        environ=environ if environ is not None else {"PATH": os.environ.get("PATH", "")},
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
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope())
    assert len(result.output_artifact_digests) == 1
    assert result.observed_transport == "local_cli"
    assert result.observed_route is not None and result.observed_route.provider_id == "anthropic"
    assert _read(worker._store, result.output_artifact_digests[0]) == b"ECHO:hello graph"


def test_rejects_a_non_local_cli_node(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(_standin(tmp_path, "#!/bin/sh\ncat\n")), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="local-CLI"):
        worker.execute(plan=_plan(transport="api_proxy"), node=_node(), envelope=_envelope())


def test_requires_an_open_network_envelope(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(_standin(tmp_path, "#!/bin/sh\ncat\n")), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="open-network"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope(mode=NetworkMode.DENY))


def test_missing_cli_binary_fails_closed(tmp_path):
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile("/no/such/cli-xyz-404"), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="not installed"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())


def test_a_failing_cli_is_a_closed_node_failure(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="x")))
    with pytest.raises(GraphIntegrityError, match="exited 3"):
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())


def test_failure_hint_redacts_a_secret_shaped_token(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\necho 'auth failed key sk-ant-abcdefghijklmnopqrstuvwxyz012345' >&2\nexit 1\n")
    worker = _worker(tmp_path, StaticCliResolver(CliInvocation(CliProfile(cli), prompt="x")))
    with pytest.raises(GraphIntegrityError) as caught:
        worker.execute(plan=_plan(), node=_node(), envelope=_envelope())
    message = str(caught.value)
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz012345" not in message
    assert "REDACTED" in message


def test_unset_env_is_applied_to_the_child(tmp_path):
    cli = _standin(tmp_path, "#!/bin/sh\nprintf 'SECRET=%s' \"$SECRET_VAR\"\n")
    resolver = StaticCliResolver(CliInvocation(CliProfile(cli, unset_env=("SECRET_VAR",)), prompt=""))
    worker = _worker(tmp_path, resolver, environ={"PATH": os.environ.get("PATH", ""), "SECRET_VAR": "leaked"})
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope())
    assert _read(worker._store, result.output_artifact_digests[0]) == b"SECRET="


def test_prompt_delivered_as_argument(tmp_path):
    cli = _standin(tmp_path, '#!/bin/sh\nprintf "ARG:%s" "$1"\n')
    resolver = StaticCliResolver(CliInvocation(CliProfile(cli, prompt_via="arg"), prompt="via-arg"))
    worker = _worker(tmp_path, resolver)
    result = worker.execute(plan=_plan(), node=_node(), envelope=_envelope())
    assert _read(worker._store, result.output_artifact_digests[0]) == b"ARG:via-arg"


def test_cli_profile_rejects_an_invalid_prompt_delivery():
    with pytest.raises(GraphValidationError):
        CliProfile("x", prompt_via="telepathy")
