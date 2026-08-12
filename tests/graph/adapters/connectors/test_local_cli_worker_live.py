"""LIVE local-CLI smoke — drive the real subscription CLIs through the worker (opt-in).

Skipped unless BL_LIVE_CLI=1 (spends a little subscription quota). For each CLI that is installed,
run a tiny prompt in subscription (print) mode and assert a non-empty reply is captured as a
content-addressed artifact. A CLI whose subscription login has expired fails closed with a clear
node error — surface it rather than silently pass.

Run:
    BL_LIVE_CLI=1 uv run pytest -s tests/graph/adapters/connectors/test_local_cli_worker_live.py
"""

from __future__ import annotations

import os
import pwd
import shutil
import types

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliInvocation,
    LocalCliConnectorWorker,
    StaticCliResolver,
)
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope, NetworkMode
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
from bounded_loops.graph.domain.authoring import Effect, IsolationLevel
from bounded_loops.graph.domain.plan import ResolvedBinding

_ORG, _PROJ = "o", "p"

pytestmark = pytest.mark.skipif(
    os.environ.get("BL_LIVE_CLI") != "1",
    reason="live subscription-CLI smoke is opt-in (set BL_LIVE_CLI=1)",
)


def _plan():
    binding = ResolvedBinding(
        binding_id="b1", slot_id="model", connector_id="c", connector_version="1", connection_id="conn",
        admission_digest="sha256:" + "d" * 64, route_policy_digest="sha256:" + "e" * 64,
        provider_id="p", model_target="m", region="local", fallback=False, transport="local_cli",
    )
    return types.SimpleNamespace(connection_bindings=(binding,))


@pytest.mark.parametrize("cli", sorted(CLI_PROFILES))
def test_live_subscription_cli_replies(cli, tmp_path):
    if shutil.which(CLI_PROFILES[cli].binary) is None:
        pytest.skip(f"{cli} CLI is not installed on this host")
    node = types.SimpleNamespace(
        node_id="agent", binding_id="b1", required_effects=frozenset({Effect.WORKSPACE_WRITE}),
        isolation=IsolationLevel.PROCESS_RESTRICTED, hard_deadline_ms=60000,
    )
    envelope = ExecutionEnvelope(
        IsolationLevel.PROCESS_RESTRICTED, "local_cli", frozenset({Effect.WORKSPACE_WRITE}), NetworkMode.OPEN, (),
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    # The test harness isolates HOME to a tmp dir; a real subscription CLI needs the operator's
    # real home to find its login, so point the child there (this is the run-freely posture).
    child_env = {**os.environ, "HOME": pwd.getpwuid(os.getuid()).pw_dir}
    worker = LocalCliConnectorWorker(
        identity=types.SimpleNamespace(run_id="live"), artifact_store=store,
        resolver=StaticCliResolver(CliInvocation(CLI_PROFILES[cli], "Reply with exactly one word: pong")),
        workspace_root=tmp_path / "work", organization_id=_ORG, project_id=_PROJ, environ=child_env,
    )
    result = worker.execute(plan=_plan(), node=node, envelope=envelope, attempt=1)
    with store.open(ArtifactRef(result.output_artifact_digests[0], _ORG, _PROJ), ArtifactAccess(_ORG, _PROJ)) as handle:
        reply = handle.read().decode("utf-8", "replace")
    assert result.observed_transport == "local_cli"
    assert reply.strip(), f"{cli} produced an empty reply"
    print(f"\n[LIVE {cli}] reply={reply.strip()[:80]!r}")
