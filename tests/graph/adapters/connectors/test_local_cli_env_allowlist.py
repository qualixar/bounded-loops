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


def test_forwarding_a_name_takes_both_the_provider_declaration_and_the_operator_grant() -> None:
    """Two independent keys, intersected — P3.

    Verified on a real host: `codex` fails without one granted key because the MCP servers it
    launches read it, while `claude`/`grok`/`muse`/`agy` need no grant at all. That key now
    reaches the CLI only when the PROVIDER declares the name and the OPERATOR allows it.

    Before P3 the operator variable alone sufficed on this path, while the base engine had always
    also required the workload to ask. One product, one security concern, two different answers —
    and the permissive one was the path that runs a network-connected agent CLI over data the
    operator did not necessarily write.
    """
    declared = CliProfile("codex", env_grant=("AZURE_HM_API_KEY",))
    allow = {"BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW": "AZURE_HM_API_KEY"}

    both = _worker({**_BENIGN, **_SECRETS, **allow})._child_env(declared)

    assert both["AZURE_HM_API_KEY"] == _SECRETS["AZURE_HM_API_KEY"]
    assert "GITHUB_TOKEN" not in both, "a grant must not widen beyond the names granted"


def test_a_provider_declaration_alone_forwards_nothing() -> None:
    """A hostile or careless provider entry cannot open the channel by itself."""
    env = _worker({**_BENIGN, **_SECRETS})._child_env(
        CliProfile("codex", env_grant=("AZURE_HM_API_KEY",)),
    )

    assert "AZURE_HM_API_KEY" not in env


def test_an_operator_grant_alone_forwards_nothing() -> None:
    """Nor can a forgotten export in a shell profile: the provider has to ask for it too."""
    source = {**_BENIGN, **_SECRETS, "BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW": "GITHUB_TOKEN"}

    env = _worker(source)._child_env(CliProfile("claude"))

    assert "GITHUB_TOKEN" not in env
    assert "AZURE_HM_API_KEY" not in env


def test_the_legacy_cli_grant_variable_still_works_on_this_path() -> None:
    """An operator who already set the old name keeps their grant across the upgrade.

    Deprecated, not broken: silently dropping a security grant on upgrade makes a working
    deployment fail for reasons that point nowhere near the rename.
    """
    source = {**_BENIGN, **_SECRETS, "BOUNDED_LOOPS_CLI_ENV_GRANT": "AZURE_HM_API_KEY"}

    env = _worker(source)._child_env(CliProfile("codex", env_grant=("AZURE_HM_API_KEY",)))

    assert env["AZURE_HM_API_KEY"] == _SECRETS["AZURE_HM_API_KEY"]


def test_the_base_engine_does_not_honour_the_graph_specific_legacy_alias() -> None:
    """Unification must not widen a subsystem that never read that name."""
    from bounded_loops.adapters._env import operator_env_grants

    source = {"BOUNDED_LOOPS_CLI_ENV_GRANT": "GITHUB_TOKEN"}

    assert operator_env_grants(source) == frozenset()
    assert operator_env_grants(source, include_legacy_cli_alias=True) == frozenset({"GITHUB_TOKEN"})


def test_shipped_profiles_grant_nothing_by_default() -> None:
    """No shipped profile may pre-grant a variable: a grant is an operator decision."""
    from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES

    # Proven non-empty first: this is a security guard over whatever the profile table holds, and
    # an empty table would satisfy "no profile pre-grants a variable" without examining one.
    assert len(CLI_PROFILES) >= 5, (
        f"only {len(CLI_PROFILES)} CLI profile(s) shipped; this guard inspects the table, so an "
        "empty one would pass it while checking nothing"
    )

    for name, profile in CLI_PROFILES.items():
        assert profile.env_grant == (), f"{name} ships with a pre-granted variable"


def test_an_operator_grant_no_provider_declares_is_reported_not_silent(caplog) -> None:
    """The direction that breaks a working pre-P3 deployment.

    Until P3 the operator variable alone forwarded a variable, so an operator who set it while
    relying on a shipped profile (all of which declare nothing) had a working setup. Under the
    intersection that same config forwards nothing. Naming it is the difference between a
    five-minute fix and debugging an auth failure several layers inside the CLI.
    """
    import logging

    source = {**_BENIGN, **_SECRETS, "BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW": "GITHUB_TOKEN"}

    with caplog.at_level(logging.WARNING):
        env = _worker(source)._child_env(CliProfile("claude"))

    assert "GITHUB_TOKEN" not in env
    assert "GITHUB_TOKEN" in caplog.text
    assert "declares none of them" in caplog.text
    # The warning names the variable, never its value.
    assert _SECRETS["GITHUB_TOKEN"] not in caplog.text


def test_the_legacy_alias_does_not_restore_the_old_one_key_behaviour_for_a_SHIPPED_profile() -> None:
    """The compatibility claim, tested against the shape the product actually ships.

    The audit's point: the legacy-alias test above uses a CUSTOM profile that declares the name, so
    it proved the variable is still read — not that a real 0.4.x deployment keeps working. Every
    shipped profile declares nothing, so an operator who set only the old variable and relied on a
    built-in provider LOSES the grant. That is the intended fix, and the docs now say so; this test
    exists so nobody re-reads the alias as backwards compatibility.
    """
    from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES

    source = {**_BENIGN, **_SECRETS, "BOUNDED_LOOPS_CLI_ENV_GRANT": "AZURE_HM_API_KEY"}

    env = _worker(source)._child_env(CLI_PROFILES["codex"])

    assert "AZURE_HM_API_KEY" not in env
