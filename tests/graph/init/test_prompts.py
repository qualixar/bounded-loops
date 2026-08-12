"""RED-first tests for `bl graph init`'s egress-posture / allowlist / confirmation
prompts (Slice 4). Every prompt takes an injectable `input_fn` — these tests never
touch real stdin, per the TDD gate's "never real interactive I/O in tests" rule.
"""

from __future__ import annotations

from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture
from bounded_loops.graph.init import prompts


def _stub(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


# ── prompt_egress_posture ────────────────────────────────────────────────────────


def test_prompt_egress_posture_blank_input_returns_open_by_default() -> None:
    assert prompts.prompt_egress_posture(input_fn=_stub("")) is EgressPosture.OPEN


def test_prompt_egress_posture_accepts_numeric_choices() -> None:
    assert prompts.prompt_egress_posture(input_fn=_stub("1")) is EgressPosture.OPEN
    assert prompts.prompt_egress_posture(input_fn=_stub("2")) is EgressPosture.ALLOWLIST
    assert prompts.prompt_egress_posture(input_fn=_stub("3")) is EgressPosture.BROKER


def test_prompt_egress_posture_accepts_names_case_insensitively() -> None:
    assert prompts.prompt_egress_posture(input_fn=_stub("ALLOWLIST")) is EgressPosture.ALLOWLIST
    assert prompts.prompt_egress_posture(input_fn=_stub("  broker  ")) is EgressPosture.BROKER


def test_prompt_egress_posture_reprompts_on_invalid_input() -> None:
    calls: list[str] = []

    def _fn(prompt: str) -> str:
        calls.append(prompt)
        return "yolo-open" if len(calls) == 1 else "open"

    assert prompts.prompt_egress_posture(input_fn=_fn) is EgressPosture.OPEN
    assert len(calls) == 2


def test_prompt_egress_posture_honors_an_explicit_default_override() -> None:
    assert prompts.prompt_egress_posture(input_fn=_stub(""), default=EgressPosture.BROKER) is EgressPosture.BROKER


# ── prompt_allowlist_hosts ────────────────────────────────────────────────────────


def test_prompt_allowlist_hosts_blank_input_returns_empty_tuple() -> None:
    assert prompts.prompt_allowlist_hosts(input_fn=_stub("")) == ()


def test_prompt_allowlist_hosts_parses_comma_separated_hosts() -> None:
    result = prompts.prompt_allowlist_hosts(input_fn=_stub("api.anthropic.com, internal.example.com:8443"))
    assert result == ("api.anthropic.com", "internal.example.com:8443")


def test_prompt_allowlist_hosts_dedupes_case_and_default_port_variants() -> None:
    result = prompts.prompt_allowlist_hosts(input_fn=_stub("API.Anthropic.COM, api.anthropic.com:443"))
    assert result == ("api.anthropic.com",)


def test_prompt_allowlist_hosts_reprompts_on_an_invalid_host_then_accepts() -> None:
    calls: list[str] = []

    def _fn(prompt: str) -> str:
        calls.append(prompt)
        return "203.0.113.5" if len(calls) == 1 else "api.anthropic.com"

    assert prompts.prompt_allowlist_hosts(input_fn=_fn) == ("api.anthropic.com",)
    assert len(calls) == 2


# ── confirm_overwrite (default: NO — never silently clobber) ───────────────────


def test_confirm_overwrite_blank_input_defaults_to_false() -> None:
    assert prompts.confirm_overwrite(input_fn=_stub("")) is False


def test_confirm_overwrite_accepts_yes_variants() -> None:
    for answer in ("y", "Y", "yes", "YES", "  yes  "):
        assert prompts.confirm_overwrite(input_fn=_stub(answer)) is True


def test_confirm_overwrite_accepts_no_variants() -> None:
    for answer in ("n", "N", "no", "NO"):
        assert prompts.confirm_overwrite(input_fn=_stub(answer)) is False


def test_confirm_overwrite_reprompts_on_garbage_then_accepts() -> None:
    calls: list[str] = []

    def _fn(prompt: str) -> str:
        calls.append(prompt)
        return "sure whatever" if len(calls) == 1 else "y"

    assert prompts.confirm_overwrite(input_fn=_fn) is True
    assert len(calls) == 2


# ── confirm_write (default: YES — the wizard's own final step) ─────────────────


def test_confirm_write_blank_input_defaults_to_true() -> None:
    assert prompts.confirm_write(input_fn=_stub("")) is True


def test_confirm_write_explicit_no_returns_false() -> None:
    assert prompts.confirm_write(input_fn=_stub("n")) is False


def test_confirm_write_explicit_yes_returns_true() -> None:
    assert prompts.confirm_write(input_fn=_stub("yes")) is True
