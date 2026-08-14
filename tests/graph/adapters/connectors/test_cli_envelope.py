"""Reading real subscription-CLI envelopes. Every shape here came off a live invocation.

Before this, a local-CLI node reported nothing, so `--max-tokens` on the flagship path paid for
the call and THEN failed the run as unmeasurable. That was found by running `bl graph run
--execute` for real — no unit test reached it, because the fixtures all use a worker that
reports usage.
"""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.connectors.cli_envelope import (
    parse_claude_envelope,
    parse_grok_envelope,
)

# Captured from `claude -p --output-format json` on 2026-08-13, trimmed to the fields read.
_CLAUDE = {
    "result": "OK",
    "total_cost_usd": 0.282617,
    "duration_ms": 3581,
    "usage": {
        "input_tokens": 2,
        "output_tokens": 4,
        "cache_creation_input_tokens": 40413,
        "cache_read_input_tokens": 6351,
    },
}

# Captured from `grok -p "..." --output-format json` on 2026-08-13.
_GROK = {
    "text": "OK",
    "total_cost_usd": 0.180474,
    "total_cost_usd_ticks": 1804740000,
    "usage": {
        "input_tokens": 90082, "cache_read_input_tokens": 128,
        "cache_creation_input_tokens": 0, "output_tokens": 41,
        "reasoning_tokens": 36, "total_tokens": 90251,
    },
}


def test_claude_cache_tokens_are_counted() -> None:
    """The finding that made this module necessary.

    A one-word reply reported input_tokens 2 against 40413 cache-creation and 6351 cache-read
    tokens. Counting only input_tokens under-counts 47_001 as 2 — four orders of magnitude, in
    the direction that lets a cap permit unauthorised spend.
    """
    envelope = parse_claude_envelope(json.dumps(_CLAUDE), reported_by="cli:claude")

    assert envelope is not None
    assert envelope.reply == "OK"
    assert envelope.usage is not None
    assert envelope.usage.input_tokens == 2 + 40413 + 6351
    assert envelope.usage.total_tokens == 46_770
    assert envelope.usage.reported_by == "cli:claude"


def test_claude_cost_comes_from_the_provider_and_rounds_up() -> None:
    """The CLI reports its own charge, so a cost cap on this path needs no price table."""
    envelope = parse_claude_envelope(json.dumps(_CLAUDE), reported_by="cli:claude")

    assert envelope is not None and envelope.usage is not None
    # $0.282617 is 282_617 micro-USD exactly.
    assert envelope.usage.cost_microunits == 282_617
    assert envelope.usage.wallclock_ms == 3581


def test_grok_preserves_the_providers_own_total() -> None:
    """`total_tokens` is Grok's arithmetic: 90082 + 128 + 0 + 41 = 90251.

    Input is derived by subtraction so that total is preserved to the token — re-summing the
    parts would silently disagree the day Grok adds a category.
    """
    envelope = parse_grok_envelope(json.dumps(_GROK), reported_by="cli:grok")

    assert envelope is not None
    assert envelope.reply == "OK"
    assert envelope.usage is not None
    assert envelope.usage.total_tokens == 90_251
    assert envelope.usage.output_tokens == 41


def test_grok_cost_uses_integer_ticks_never_the_float() -> None:
    """Ticks are integers at 1e-10 USD, so micro-USD needs no float on this path at all."""
    envelope = parse_grok_envelope(json.dumps(_GROK), reported_by="cli:grok")

    assert envelope is not None and envelope.usage is not None
    assert envelope.usage.cost_microunits == 180_474  # 1_804_740_000 // 10_000


def test_the_wrong_cli_shape_is_not_parsed_as_the_other() -> None:
    """Both CLIs emit JSON; only the right parser should recognise each."""
    assert parse_grok_envelope(json.dumps(_CLAUDE), reported_by="x") is None
    assert parse_claude_envelope(json.dumps(_GROK), reported_by="x") is None


def test_an_unreadable_envelope_degrades_to_unmeasured_never_a_crash() -> None:
    """A CLI upgrade must not break runs. The caller falls back to raw stdout, and a declared
    cap then fails closed — the honest outcome when metering cannot be read."""
    for stdout in ("", "not json", "[]", '"a string"', '{"result": 5}', '{"text": null}'):
        assert parse_claude_envelope(stdout, reported_by="x") is None
        assert parse_grok_envelope(stdout, reported_by="x") is None


def test_a_hostile_usage_block_yields_no_usage_but_keeps_the_reply() -> None:
    """A negative count would REFUND budget. The reply is still returned: the call did happen."""
    hostile = {**_CLAUDE, "usage": {**_CLAUDE["usage"], "output_tokens": -5}}  # type: ignore[dict-item]

    envelope = parse_claude_envelope(json.dumps(hostile), reported_by="cli:claude")

    assert envelope is not None
    assert envelope.reply == "OK"
    assert envelope.usage is None


def test_a_partial_claude_usage_block_is_not_a_partial_total() -> None:
    """One missing category means the total would be a guess, so nothing is reported."""
    partial = {**_CLAUDE, "usage": {"input_tokens": 2, "output_tokens": 4}}

    envelope = parse_claude_envelope(json.dumps(partial), reported_by="cli:claude")

    assert envelope is not None and envelope.usage is None


# Captured from `agy -p "..." --output-format json` on 2026-08-13.
_AGY = {
    "response": "OK\n",
    "status": "ok",
    "usage": {
        "input_tokens": 22501, "output_tokens": 237, "thinking_tokens": 231,
        "cache_read_tokens": 0, "total_tokens": 22738,
    },
}


def test_agy_reports_tokens_but_no_cost() -> None:
    """agy meters tokens and nothing else, so a cost cap on it needs an operator price table."""
    from bounded_loops.graph.adapters.connectors.cli_envelope import parse_agy_envelope

    envelope = parse_agy_envelope(json.dumps(_AGY), reported_by="cli:agy")

    assert envelope is not None
    assert envelope.reply == "OK\n"
    assert envelope.usage is not None
    assert envelope.usage.total_tokens == 22_738
    assert envelope.usage.output_tokens == 237
    assert envelope.usage.cost_microunits is None


def test_agy_thinking_tokens_are_not_double_counted() -> None:
    """231 thinking against 237 output: a subset of output, so adding it would count twice."""
    from bounded_loops.graph.adapters.connectors.cli_envelope import parse_agy_envelope

    envelope = parse_agy_envelope(json.dumps(_AGY), reported_by="cli:agy")

    assert envelope is not None and envelope.usage is not None
    assert envelope.usage.total_tokens == 22_501 + 237  # not + 231


def test_agy_takes_the_larger_of_its_own_total_and_the_summed_parts() -> None:
    """Whether agy folds cache reads into total_tokens is unverified — the live probe had 0.

    Taking the larger cannot under-count, and under-counting is the direction that lets a cap
    permit unauthorised spend.
    """
    from bounded_loops.graph.adapters.connectors.cli_envelope import parse_agy_envelope

    understated = {**_AGY, "usage": {**_AGY["usage"], "cache_read_tokens": 5_000}}  # type: ignore[dict-item]

    envelope = parse_agy_envelope(json.dumps(understated), reported_by="cli:agy")

    assert envelope is not None and envelope.usage is not None
    # 22501 + 5000 + 237 = 27738 exceeds agy's stale total of 22738, so the sum wins.
    assert envelope.usage.total_tokens == 27_738


def test_an_unreadable_envelope_must_not_become_the_artifact(tmp_path) -> None:
    """Muse round 3 raised this as "no fix needed". It needed one.

    Falling back to raw stdout when the envelope cannot be parsed stores the JSON envelope
    ITSELF as the node's output artifact — not what text mode produces, waved through by a gate
    that only checks non-empty UTF-8, and then handed to every downstream node. That is a
    silently wrong output, which is worse than a clear failure, so the worker refuses.
    """
    from bounded_loops.graph.adapters.connectors.local_cli_worker import (
        CliProfile, LocalCliConnectorWorker,
    )

    profile = CliProfile(
        "claude", ("-p",), prompt_via="stdin",
        usage_args=("--output-format", "json"), envelope="claude",
    )
    # The parser's contract: an unreadable shape is None, never a guess.
    assert LocalCliConnectorWorker._read_envelope(profile, "this is not the envelope") is None
    assert LocalCliConnectorWorker._read_envelope(profile, '{"unexpected": "shape"}') is None
    # A profile that never asked for an envelope keeps its plain-stdout behaviour.
    plain = CliProfile("codex", ("exec",), prompt_via="arg")
    assert plain.envelope == ""
    assert LocalCliConnectorWorker._read_envelope(plain, "plain reply") is None


@pytest.mark.parametrize("cost", ["Infinity", "-Infinity", "NaN", "1e308"])
def test_a_cost_python_cannot_represent_is_unmeasured_not_an_exception(cost: str) -> None:
    """Grok round 3, P1. `json.loads` accepts Infinity and NaN; int(inf) raises.

    That exception escaped the parser, reached the controller as a plain worker fault, and got
    RETRIED — paying the provider once per attempt while a declared cap was never consulted.
    Unmeasured is the honest answer to a cost nobody can represent, and it fails a cap closed.
    """
    body = json.dumps(_CLAUDE).replace('"total_cost_usd": 0.282617', f'"total_cost_usd": {cost}')

    envelope = parse_claude_envelope(body, reported_by="cli:claude")

    assert envelope is not None, "the reply is still returned — the call did happen"
    assert envelope.usage is not None
    assert envelope.usage.cost_microunits is None
