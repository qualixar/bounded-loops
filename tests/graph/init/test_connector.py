"""RED-first tests for `bl graph init`'s connector-mode prompt (Slice 4).

Connector mode is informational only in this slice (see connector.py's module
docstring for why no connector config file is written) — these tests cover the
prompt's default/aliasing/reprompt behavior and the BYOK guidance text's
no-secret contract, via an injected `input_fn`, never real stdin.
"""

from __future__ import annotations

from bounded_loops.graph.init.connector import ConnectorMode, describe_byok_pointer, prompt_connector_mode


def _stub(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


# ── ConnectorMode ────────────────────────────────────────────────────────────────


def test_connector_mode_values() -> None:
    assert ConnectorMode.LOCAL_CLI.value == "local_cli"
    assert ConnectorMode.BYOK.value == "byok"


# ── prompt_connector_mode ────────────────────────────────────────────────────────


def test_prompt_connector_mode_blank_input_returns_the_default() -> None:
    assert prompt_connector_mode(input_fn=_stub("")) is ConnectorMode.LOCAL_CLI


def test_prompt_connector_mode_accepts_numeric_choice_1() -> None:
    assert prompt_connector_mode(input_fn=_stub("1")) is ConnectorMode.LOCAL_CLI


def test_prompt_connector_mode_accepts_numeric_choice_2() -> None:
    assert prompt_connector_mode(input_fn=_stub("2")) is ConnectorMode.BYOK


def test_prompt_connector_mode_accepts_the_name_byok() -> None:
    assert prompt_connector_mode(input_fn=_stub("byok")) is ConnectorMode.BYOK


def test_prompt_connector_mode_accepts_the_name_local_cli() -> None:
    assert prompt_connector_mode(input_fn=_stub("local_cli")) is ConnectorMode.LOCAL_CLI


def test_prompt_connector_mode_is_case_and_whitespace_insensitive() -> None:
    assert prompt_connector_mode(input_fn=_stub("  BYOK  ")) is ConnectorMode.BYOK


def test_prompt_connector_mode_reprompts_on_invalid_input_then_accepts_a_good_answer() -> None:
    calls: list[str] = []

    def _fn(prompt: str) -> str:
        calls.append(prompt)
        return "nonsense" if len(calls) == 1 else "byok"

    assert prompt_connector_mode(input_fn=_fn) is ConnectorMode.BYOK
    assert len(calls) == 2  # proves it actually re-prompted rather than guessing


def test_prompt_connector_mode_honors_an_explicit_default_override() -> None:
    assert prompt_connector_mode(input_fn=_stub(""), default=ConnectorMode.BYOK) is ConnectorMode.BYOK


# ── describe_byok_pointer — no-secret contract ──────────────────────────────────


def test_describe_byok_pointer_is_a_nonempty_string() -> None:
    assert isinstance(describe_byok_pointer(), str)
    assert describe_byok_pointer().strip() != ""


def test_describe_byok_pointer_points_at_env_vars_and_admitted_connections() -> None:
    text = describe_byok_pointer().lower()
    assert "environment variable" in text
    assert "--admitted" in text
    assert "never" in text  # states the no-secret-on-disk guarantee explicitly


def test_describe_byok_pointer_never_contains_a_credential_assignment_with_a_real_looking_value() -> None:
    # The only "export FOO=" example in the text must be an obvious placeholder,
    # never something that looks like a real credential.
    text = describe_byok_pointer()
    for line in text.splitlines():
        if "export" in line and "=" in line:
            _, _, value = line.partition("=")
            assert value.strip().startswith("...") or value.strip() == ""
