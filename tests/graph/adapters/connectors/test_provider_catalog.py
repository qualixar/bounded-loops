"""A provider catalog is operator-writable config, so it is treated as hostile input.

The point of the catalog is that a non-programmer can add an agent CLI without a code change and
without a release. That means the file is edited by hand, by someone who is not reading this
source, on a machine holding real subscription credentials. Every test here is about what happens
when that file is wrong — because "it works when the file is right" is the easy half.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES
from bounded_loops.graph.adapters.connectors.provider_catalog import (
    catalog_from_mapping,
    describe,
    load_provider_catalog,
    resolve_cli_profiles,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "providers.toml"
    path.write_text(body, encoding="utf-8")
    return path


_GOOD = """
[providers.mycli]
binary = "mycli"
args = ["--print"]
prompt_via = "arg"
usage_args = ["--json"]
envelope = "claude"
env_grant = ["MYCLI_REGION"]
"""


def test_a_well_formed_entry_becomes_a_usable_profile(tmp_path: Path) -> None:
    catalog = load_provider_catalog(_write(tmp_path, _GOOD))

    profile = catalog["mycli"]
    assert profile.binary == "mycli"
    assert profile.args == ("--print",)
    assert profile.prompt_via == "arg"
    assert profile.envelope == "claude"
    assert profile.env_grant == ("MYCLI_REGION",)


def test_a_catalog_cannot_carry_an_environment_VALUE(tmp_path: Path) -> None:
    """``set_env`` is refused outright rather than sanitized.

    A config file that CAN hold a value is a config file that eventually holds a credential —
    committed to a repo, copied into a ticket, pasted into a chat. The engine has never needed to
    read a credential value and this is the door through which it would learn how.
    """
    body = '[providers.mycli]\nbinary = "mycli"\nset_env = { API_KEY = "sk-live-abc" }\n'

    with pytest.raises(GraphValidationError, match="holds VALUES"):
        load_provider_catalog(_write(tmp_path, body))


def test_an_env_grant_entry_that_is_not_a_name_is_refused(tmp_path: Path) -> None:
    """``env_grant`` forwards NAMES. Something that is plainly a value means the operator has
    pasted a secret into a config file and needs to be told to rotate it, not quietly obeyed."""
    body = '[providers.mycli]\nbinary = "mycli"\nenv_grant = ["sk-ant-api03-notaname"]\n'

    with pytest.raises(GraphValidationError, match="rotate it"):
        load_provider_catalog(_write(tmp_path, body))


def test_a_typo_is_an_error_not_a_shrug(tmp_path: Path) -> None:
    """``envelop`` silently ignored would leave the operator believing their provider is metered.

    That is the worst available outcome, not a cosmetic one: every spend cap on that provider then
    fails closed as unmeasurable, which reads as "the budget is protecting me" while the real
    reason nothing is capped is a missing letter.
    """
    body = '[providers.mycli]\nbinary = "mycli"\nenvelop = "claude"\n'

    with pytest.raises(GraphValidationError, match="unknown key"):
        load_provider_catalog(_write(tmp_path, body))


def test_an_envelope_with_no_shipped_parser_is_refused(tmp_path: Path) -> None:
    """Refused at load, not at run time — where it would already have cost a provider call."""
    body = '[providers.mycli]\nbinary = "mycli"\nusage_args = ["--json"]\nenvelope = "codex"\n'

    with pytest.raises(GraphValidationError, match="no parser named"):
        load_provider_catalog(_write(tmp_path, body))


def test_an_envelope_without_usage_args_is_refused(tmp_path: Path) -> None:
    """Declaring an envelope but never asking the CLI for it means the CLI emits plain text and
    every attempt fails on an unreadable envelope — paying each time."""
    body = '[providers.mycli]\nbinary = "mycli"\nenvelope = "claude"\n'

    with pytest.raises(GraphValidationError, match="no usage_args"):
        load_provider_catalog(_write(tmp_path, body))


def test_an_entry_must_name_a_binary(tmp_path: Path) -> None:
    with pytest.raises(GraphValidationError, match="must name the binary"):
        load_provider_catalog(_write(tmp_path, '[providers.mycli]\nargs = ["--print"]\n'))


@pytest.mark.parametrize("prompt_via", ["pipe", "PROMPT", "", "stdin "])
def test_only_the_two_real_prompt_modes_are_accepted(tmp_path: Path, prompt_via: str) -> None:
    body = f'[providers.mycli]\nbinary = "mycli"\nprompt_via = "{prompt_via}"\n'

    with pytest.raises(GraphValidationError, match="prompt_via"):
        load_provider_catalog(_write(tmp_path, body))


def test_an_unknown_top_level_section_is_refused() -> None:
    with pytest.raises(GraphValidationError, match="unknown top-level section"):
        catalog_from_mapping({"providers": {}, "prices": {}})


def test_a_secret_shaped_provider_name_is_refused() -> None:
    with pytest.raises(GraphValidationError, match="secret-shaped"):
        catalog_from_mapping({"providers": {"api_key": {"binary": "x"}}})


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(GraphValidationError, match="not valid TOML"):
        load_provider_catalog(_write(tmp_path, "[providers.mycli\nbinary = "))


def test_a_missing_catalog_is_an_error_not_an_empty_catalog(tmp_path: Path) -> None:
    """Silently treating an unreadable catalog as empty would run the graph against the shipped
    five while the operator believed their own providers were loaded."""
    with pytest.raises(GraphValidationError, match="could not be read"):
        load_provider_catalog(tmp_path / "absent.toml")


def test_the_operator_catalog_outranks_a_shipped_profile(tmp_path: Path) -> None:
    """Deliberate: this is how an operator points ``claude`` at a wrapper script, or corrects a
    flag this version got wrong on their host, without waiting for a release."""
    body = '[providers.claude]\nbinary = "/opt/wrappers/claude-wrapper"\nprompt_via = "stdin"\n'

    resolved = resolve_cli_profiles(catalog_path=_write(tmp_path, body), include_plugins=False)

    assert resolved["claude"].binary == "/opt/wrappers/claude-wrapper"
    assert set(CLI_PROFILES) <= set(resolved), "an override must not drop the other providers"


def test_no_catalog_means_exactly_the_shipped_profiles() -> None:
    assert dict(resolve_cli_profiles(include_plugins=False)) == dict(CLI_PROFILES)


def test_describe_reports_metering_and_names_but_never_a_value(tmp_path: Path) -> None:
    """``bl graph providers`` is the answer to "can I put a spend cap on this provider?" — so it
    has to say which providers are unmetered, out loud."""
    lines = describe(resolve_cli_profiles(catalog_path=_write(tmp_path, _GOOD), include_plugins=False))
    joined = "\n".join(lines)

    assert "codex" in joined and "NOT metered" in joined
    assert "MYCLI_REGION" in joined
    assert "sk-" not in joined
