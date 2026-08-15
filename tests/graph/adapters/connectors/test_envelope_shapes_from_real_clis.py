"""The parsers, against the FULL envelope shape the real CLIs emit — not a simplified fixture.

**Runs with no CLI installed, no login, and no network.** The envelopes below are committed data.
Nothing here shells out.

Why this exists alongside `test_cli_envelope.py`, which already covers these parsers well: those
fixtures are hand-built and minimal — a `usage` block with the four keys the parser reads. The real
envelopes are much larger and carry sibling structures that a naive parser can trip over, most
importantly a per-model `modelUsage` breakdown that repeats the same token counts under different
keys. A parser that summed indiscriminately would double-count every call, and a minimal fixture
would never reveal it.

**Provenance.** Key paths and value TYPES were captured on 2026-08-15 by invoking each binary with
its shipped profile and recording the structure. The VALUES here are synthetic — no session id,
request id, conversation id or account field from a real response is committed, and the identifier
fields are kept only as placeholders because their PRESENCE is part of the shape. What is asserted
is that the parsers read the right numbers out of the right places in a realistically-shaped
document.

When a CLI changes its envelope, this is the test that should fail. That is the whole point: the
previous record of these shapes was a prose comment saying "verified live", which no reader could
re-check and no change could invalidate.
"""

from __future__ import annotations

import json

from bounded_loops.graph.adapters.connectors.cli_envelope import ENVELOPE_PARSERS

# ── captured shapes, synthetic values ────────────────────────────────────────

CLAUDE_ENVELOPE = {
    "type": "result",
    "subtype": "success",
    "result": "ok",
    "session_id": "PLACEHOLDER-SESSION",
    "uuid": "PLACEHOLDER-UUID",
    "num_turns": 1,
    "stop_reason": "end_turn",
    "terminal_reason": "done",
    "permission_denials": [],
    "time_to_request_ms": 120,
    "ttft_ms": 300,
    "ttft_stream_ms": 310,
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 2,
        "output_tokens": 5,
        "cache_creation_input_tokens": 40413,
        "cache_read_input_tokens": 6351,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 40413,
        },
        "output_tokens_details": {"thinking_tokens": 0},
        "server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 0},
        "service_tier": "standard",
        "speed": "fast",
        "inference_geo": "unknown",
        # The sibling that a summing parser would double-count.
        "iterations": [
            {
                "type": "assistant",
                "input_tokens": 2,
                "output_tokens": 5,
                "cache_creation_input_tokens": 40413,
                "cache_read_input_tokens": 6351,
            }
        ],
    },
    # Per-model breakdown repeating the SAME call under different key spellings.
    "modelUsage": {
        "claude-sonnet-4-6": {
            "inputTokens": 2,
            "outputTokens": 5,
            "cacheCreationInputTokens": 40413,
            "cacheReadInputTokens": 6351,
            "costUSD": 0.0123,
            "contextWindow": 200000,
            "maxOutputTokens": 64000,
            "canonicalModel": "claude-sonnet-4-6",
            "provider": "anthropic",
            "webSearchRequests": 0,
        },
        "claude-haiku-4-5-20251001": {
            "inputTokens": 0,
            "outputTokens": 0,
            "costUSD": 0.0,
            "maxOutputTokens": 8192,
            "provider": "anthropic",
            "webSearchRequests": 0,
        },
    },
}

GROK_ENVELOPE = {
    "text": "ok",
    "thought": "",
    "sessionId": "PLACEHOLDER-SESSION",
    "requestId": "PLACEHOLDER-REQUEST",
    "stopReason": "end_turn",
    "num_turns": 1,
    "total_cost_usd": 0.0009,
    # Integer ticks at 1e-10 USD — the field the parser must prefer over the float.
    "total_cost_usd_ticks": 9_000_000,
    "usage": {
        "input_tokens": 90082,
        "output_tokens": 41,
        "cache_creation_input_tokens": 128,
        "cache_read_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 90251,
    },
    "modelUsage": {
        "grok-4.6-build": {
            "inputTokens": 90082,
            "outputTokens": 41,
            "cacheCreationInputTokens": 128,
            "cacheReadInputTokens": 0,
            "costUSD": 0.0009,
            "modelCalls": 1,
        }
    },
}

AGY_ENVELOPE = {
    "response": "ok",
    "status": "success",
    "conversation_id": "PLACEHOLDER-CONVERSATION",
    "duration_seconds": 1.5,
    "num_turns": 1,
    "usage": {
        "input_tokens": 22501,
        "output_tokens": 237,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 22738,
    },
    # NOTE: no cost field of any kind. agy meters tokens and not money, so a cost cap on it must
    # fail closed rather than treat the call as free.
}


def _parse(envelope_name: str, document: dict):
    return ENVELOPE_PARSERS[envelope_name](
        json.dumps(document), reported_by=f"{envelope_name}-cli"
    )


# ── the reply lands in a different key for every CLI ─────────────────────────


def test_each_parser_finds_its_own_reply_key() -> None:
    """`result` / `text` / `response`. Three CLIs, three spellings, no shared convention."""
    assert _parse("claude", CLAUDE_ENVELOPE).reply == "ok"
    assert _parse("grok", GROK_ENVELOPE).reply == "ok"
    assert _parse("agy", AGY_ENVELOPE).reply == "ok"


# ── the double-counting hazard the minimal fixtures cannot show ──────────────


def test_claude_does_not_double_count_modelUsage_or_iterations() -> None:
    """The real envelope states the same tokens three times. Only `usage` may be read.

    `usage`, `usage.iterations[0]` and `modelUsage.<model>` all describe ONE call. Summing any two
    inflates every total, and a spend cap computed from an inflated total trips early — which looks
    like the safe direction until it halts a production graph that was within budget.
    """
    usage = _parse("claude", CLAUDE_ENVELOPE).usage
    assert usage is not None

    # 2 + 5 + 40413 + 6351 = 46771, counted once.
    assert usage.total_tokens == 46_771, (
        f"expected the single-count total, got {usage.total_tokens}. Doubling would give 93_542 "
        "(iterations) or similar from modelUsage."
    )


def test_grok_prefers_the_integer_ticks_over_the_float_cost() -> None:
    """9_000_000 ticks at 1e-10 USD = 0.0009 USD, reachable without float arithmetic."""
    usage = _parse("grok", GROK_ENVELOPE).usage
    assert usage is not None
    assert usage.cost_microunits == 900, f"expected 900 micro-USD, got {usage.cost_microunits}"


def test_grok_total_tokens_is_the_providers_own_arithmetic() -> None:
    """90082 + 128 + 0 + 41 = 90251 — grok states its own total and we take it."""
    usage = _parse("grok", GROK_ENVELOPE).usage
    assert usage is not None
    assert usage.total_tokens == 90_251


def test_agy_reports_tokens_and_no_money_at_all() -> None:
    """The asymmetry that a cost cap has to respect: metered in tokens, unmetered in money."""
    usage = _parse("agy", AGY_ENVELOPE).usage
    assert usage is not None
    assert usage.total_tokens == 22_738
    assert usage.cost_microunits is None, (
        "agy publishes no cost field; inventing one would meter money that was never reported"
    )


# ── shape drift is the failure this file exists to produce ───────────────────


def test_every_shipped_envelope_parser_has_a_captured_shape_here() -> None:
    """A new parser without a captured real shape is a parser tested only against its own author.

    This is the guard that keeps the file honest as providers are added: it fails on the day someone
    ships a sixth envelope parser and pins it with a hand-built fixture alone.
    """
    captured = {"claude", "grok", "agy"}
    unmeasured = set(ENVELOPE_PARSERS) - captured

    assert not unmeasured, (
        f"envelope parsers with no captured real shape: {sorted(unmeasured)}. Probe the binary, "
        "record the key paths and types, add a synthetic-value fixture above."
    )


def test_the_fixtures_carry_no_real_identifiers() -> None:
    """Committed fixtures must not contain a real session, request or conversation id.

    Envelopes are captured from live accounts, so the sanitising step is load-bearing rather than
    tidy. Asserted so a future capture cannot be pasted in raw.
    """
    for envelope in (CLAUDE_ENVELOPE, GROK_ENVELOPE, AGY_ENVELOPE):
        serialized = json.dumps(envelope)
        for field in ("session_id", "uuid", "sessionId", "requestId", "conversation_id"):
            value = envelope.get(field)
            if value is not None:
                assert str(value).startswith("PLACEHOLDER"), (
                    f"{field} looks like a real identifier: sanitise before committing"
                )
        assert "sk-" not in serialized and "Bearer" not in serialized
