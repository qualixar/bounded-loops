"""The admitted CLI receives an ALLOWLISTED environment, never the whole parent env (H-1).

Before this, the connector inherited all of `os.environ` and removed a few named keys, so
any credential the operator had exported reached the CLI subprocess. An agent CLI is a
capable, network-connected process that acts on data the operator did not necessarily
write, so a prompt injection in that data could enumerate and exfiltrate the environment.
Output redaction cannot help after the fact, and its pattern misses short keys anyway.

These tests fail if the allowlist is removed, widened to the whole environment, or if a
grant stops being scoped to the names actually granted.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

from bounded_loops.adapters._env import ENV_ALLOWLIST
from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CliProfile,
    LocalCliConnectorWorker,
)
from bounded_loops.graph.domain.events import GraphRunIdentity

_SECRETS = {"AZURE_HM_API_KEY": "[test-secret-1]", "GITHUB_TOKEN": "[test-secret-2]"}
_BENIGN = {"PATH": "/usr/bin:.", "HOME": "/Users/someone", "TERM": "xterm", "USER": "someone"}


def _identity() -> GraphRunIdentity:
    return GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )


def _worker(environ: dict[str, str]) -> LocalCliConnectorWorker:
    return LocalCliConnectorWorker(
        identity=_identity(), artifact_store=None, resolver=None,  # type: ignore[arg-type]
        workspace_root=Path(tempfile.mkdtemp()),
        organization_id="org-1", project_id="project-1", environ=environ,
    )


def test_ambient_secrets_never_reach_the_cli_subprocess() -> None:
    env = _worker({**_BENIGN, **_SECRETS})._child_env(CliProfile("claude"))

    for name in _SECRETS:
        assert name not in env, f"{name} leaked into the CLI environment"
    assert set(env) <= (ENV_ALLOWLIST | {"USER", "LOGNAME", "TERM"})


def test_path_drops_relative_entries_so_the_workdir_cannot_shadow_a_binary() -> None:
    # The CLI runs with cwd=workdir; a relative PATH entry would let a file the workdir
    # happens to contain resolve ahead of the real binary.
    env = _worker({**_BENIGN, **_SECRETS})._child_env(CliProfile("claude"))

    assert "." not in env["PATH"].split(":")


def test_a_profile_grant_forwards_exactly_that_name_and_nothing_else() -> None:
    """A CLI whose own tooling reads a key can be granted it — narrowly.

    Verified on a real host: `codex` fails without one granted key because the MCP servers
    it launches read it, while `claude`/`grok`/`muse`/`agy` need no grant at all.
    """
    env = _worker({**_BENIGN, **_SECRETS})._child_env(
        CliProfile("codex", env_grant=("AZURE_HM_API_KEY",)),
    )

    assert env["AZURE_HM_API_KEY"] == _SECRETS["AZURE_HM_API_KEY"]
    assert "GITHUB_TOKEN" not in env, "a grant must not widen beyond the names granted"


def test_operator_can_grant_by_name_through_the_environment() -> None:
    source = {**_BENIGN, **_SECRETS, "BOUNDED_LOOPS_CLI_ENV_GRANT": "GITHUB_TOKEN"}

    env = _worker(source)._child_env(CliProfile("claude"))

    assert env["GITHUB_TOKEN"] == _SECRETS["GITHUB_TOKEN"]
    assert "AZURE_HM_API_KEY" not in env


def test_shipped_profiles_grant_nothing_by_default() -> None:
    """No shipped profile may pre-grant a variable: a grant is an operator decision."""
    from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES

    for name, profile in CLI_PROFILES.items():
        assert profile.env_grant == (), f"{name} ships with a pre-granted variable"
