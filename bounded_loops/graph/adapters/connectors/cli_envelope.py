"""Reading a subscription CLI's machine-readable envelope: the reply, and what it cost.

A local-CLI node runs the operator's own logged-in agent CLI. In text mode those CLIs print
the reply and nothing else, so a spend cap on the flagship path could never be metered — a run
with ``--max-tokens`` paid for the call and THEN failed as unmeasurable. Asking for the CLI's
JSON envelope instead yields both the reply and its real usage.

Two things make this worth the extra parsing:

* **The CLI reports actual cost.** ``total_cost_usd`` is the provider's own charge, so a cost
  cap on this path needs no price table at all.
* **Cache tokens dominate, and ignoring them under-counts catastrophically.** A measured
  ``claude -p`` reply had ``input_tokens: 2`` against ``cache_creation_input_tokens: 40413``
  and ``cache_read_input_tokens: 6351`` — an honest total of 47_001 against a naive 2. Counting
  only ``input_tokens`` under-counts by four orders of magnitude, in the direction that lets a
  cap permit unauthorised spend.

Every parser here is defensive: a CLI that changes its output shape must degrade to "unmeasured"
and let the caller fail closed, never crash a run and never invent a number.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Mapping

from bounded_loops.graph.domain.usage import MICROUNITS_PER_USD, WorkerUsage

#: Every token field on Anthropic's usage block. Cache tokens are consumed tokens: they are
#: billed (creation at a premium, reads at a discount) and they count against a token budget.
#: Listed explicitly so a NEW field the CLI starts reporting is visibly absent here rather than
#: silently folded in — a token cap must not drift because a provider added a category.
_CLAUDE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True)
class CliEnvelope:
    """One CLI invocation's reply text plus what it consumed."""

    #: The reply, exactly as the CLI would have printed it in text mode. This becomes the
    #: node's output artifact, so the artifact contract is unchanged by asking for JSON.
    reply: str
    usage: WorkerUsage | None


def parse_claude_envelope(stdout: str, *, reported_by: str) -> CliEnvelope | None:
    """Parse ``claude -p --output-format json``. ``None`` when it is not that shape.

    Returning ``None`` rather than raising keeps a CLI upgrade from breaking runs: the caller
    falls back to treating stdout as the reply, and a declared spend cap then fails closed as
    unmeasurable — which is the honest outcome when the metering cannot be read.
    """
    document = _load(stdout)
    if document is None:
        return None
    reply = document.get("result")
    if not isinstance(reply, str):
        return None
    return CliEnvelope(reply=reply, usage=_claude_usage(document, reported_by=reported_by))


def _claude_usage(document: Mapping[str, object], *, reported_by: str) -> WorkerUsage | None:
    block = document.get("usage")
    if not isinstance(block, Mapping):
        return None
    output = _count(block.get("output_tokens"))
    inputs = [
        _count(block.get(field))
        for field in _CLAUDE_TOKEN_FIELDS
        if field != "output_tokens"
    ]
    if output is None or any(value is None for value in inputs):
        # A missing or unusable field means the total would be a guess. Unmeasured, so a cap
        # fails closed, rather than a partial sum presented as the whole.
        return None
    return WorkerUsage(
        # Every input-side category summed: prompt, cache writes, cache reads. All are tokens
        # the provider counted and billed.
        input_tokens=sum(value for value in inputs if value is not None),
        output_tokens=output,
        cost_microunits=_cost_microunits(document.get("total_cost_usd")),
        wallclock_ms=_count(document.get("duration_ms")),
        reported_by=reported_by,
    )


def _cost_microunits(raw: object) -> int | None:
    """``total_cost_usd`` to integer micro-USD, rounded UP.

    A float arrives here because that is what the CLI emits; it is converted once, at the
    boundary, and never used for arithmetic afterwards. Rounding up rather than truncating for
    the usual reason: under-charging a cap lets spend past it.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    # json.loads accepts the Infinity and NaN tokens, and a huge finite value overflows to inf
    # on the multiply below. int(inf) raises OverflowError and int(nan) raises ValueError — which
    # escaped this parser, reached the controller as a plain worker fault, and got RETRIED,
    # paying the provider once per attempt. Unmeasured is the honest answer to a cost nobody can
    # represent, and it fails a declared cap closed instead of spending against it.
    if not math.isfinite(raw) or raw < 0:
        return None
    exact = raw * MICROUNITS_PER_USD
    if not math.isfinite(exact):
        return None
    rounded = int(exact)
    return rounded if rounded == exact else rounded + 1


def _load(stdout: str) -> Mapping[str, object] | None:
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(document, Mapping) or not all(isinstance(key, str) for key in document):
        return None
    return document


def _count(value: object) -> int | None:
    # bool first: it is an int subclass, so ``true`` would otherwise read as one token.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


#: Grok reports ``total_cost_usd_ticks`` as an INTEGER at 1e-10 USD, alongside the same figure
#: as a float. The integer is used: micro-USD is ticks // 10_000 exactly, so the float — and
#: every rounding question that comes with it — is never touched.
_TICKS_PER_MICROUNIT = 10_000


def parse_grok_envelope(stdout: str, *, reported_by: str) -> CliEnvelope | None:
    """Parse ``grok -p --output-format json``. ``None`` when it is not that shape.

    The reply is under ``text`` (not ``result``), and ``usage.total_tokens`` is the provider's
    own arithmetic over prompt + cache + output — verified live: 90082 input + 128 cache_read +
    0 cache_creation + 41 output = 90251 total. That total is preserved exactly rather than
    re-summed here, so it cannot drift from what Grok itself billed.
    """
    document = _load(stdout)
    if document is None:
        return None
    reply = document.get("text")
    if not isinstance(reply, str):
        return None
    return CliEnvelope(reply=reply, usage=_grok_usage(document, reported_by=reported_by))


def _grok_usage(document: Mapping[str, object], *, reported_by: str) -> WorkerUsage | None:
    block = document.get("usage")
    if not isinstance(block, Mapping):
        return None
    total = _count(block.get("total_tokens"))
    output = _count(block.get("output_tokens"))
    if total is None or output is None or output > total:
        return None
    return WorkerUsage(
        # Input is derived by subtraction so the provider's own total is preserved to the token.
        # Re-summing the parts would silently disagree the day Grok adds a category.
        input_tokens=total - output,
        output_tokens=output,
        cost_microunits=_ticks_to_microunits(document.get("total_cost_usd_ticks")),
        reported_by=reported_by,
    )


def _ticks_to_microunits(raw: object) -> int | None:
    """Integer ticks (1e-10 USD) to integer micro-USD, rounded UP.

    No float anywhere on this path. Rounding up for the usual reason: under-charging a cap is
    what lets spend past it.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return -(-raw // _TICKS_PER_MICROUNIT)

#: agy's own token categories. ``thinking_tokens`` is not added: on a live reply it was 231
#: against 237 output tokens, i.e. a SUBSET of output, and adding it would double-count.
_AGY_INPUT_FIELDS = ("input_tokens", "cache_read_tokens")


def parse_agy_envelope(stdout: str, *, reported_by: str) -> CliEnvelope | None:
    """Parse ``agy -p --output-format json``. ``None`` when it is not that shape.

    The reply is under ``response``. agy reports NO cost, so a cost cap on this provider needs an
    operator price table — and fails closed without one, which is the honest outcome.
    """
    document = _load(stdout)
    if document is None:
        return None
    reply = document.get("response")
    if not isinstance(reply, str):
        return None
    return CliEnvelope(reply=reply, usage=_agy_usage(document, reported_by=reported_by))


def _agy_usage(document: Mapping[str, object], *, reported_by: str) -> WorkerUsage | None:
    block = document.get("usage")
    if not isinstance(block, Mapping):
        return None
    output = _count(block.get("output_tokens"))
    if output is None:
        return None
    parts = [_count(block.get(field)) for field in _AGY_INPUT_FIELDS]
    if any(value is None for value in parts):
        return None
    summed = sum(value for value in parts if value is not None)
    reported_total = _count(block.get("total_tokens"))
    # Whichever is LARGER. On the live reply the two agreed (22501 + 0 + 237 = 22738), but the
    # cache field was zero, so whether agy folds cache reads into its own total is unverified.
    # Taking the larger cannot under-count, and under-counting is the direction that lets a cap
    # permit unauthorised spend.
    if reported_total is not None and reported_total > summed + output:
        summed = reported_total - output
    return WorkerUsage(
        input_tokens=summed,
        output_tokens=output,
        # No cost field: a cost cap here needs an operator price table, or it fails closed.
        reported_by=reported_by,
    )
