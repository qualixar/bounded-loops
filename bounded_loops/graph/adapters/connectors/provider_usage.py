"""Reading token counts out of a provider response, and nothing else out of it.

The forwarder is the only component that ever holds a provider's response bytes — everything
downstream sees a content digest. So usage has to be extracted here or not at all.

What crosses back is integers. No text, no message content, no header values: the
no-response-bytes-through-the-graph rule is what keeps a run's audit trail free of model
output, and a "usage" block is only exempt from it because a count is not content.

This parses UNTRUSTED input. Anything it cannot read confidently becomes ``None``, meaning
unmeasured — which makes a budgeted node fail closed with a clear message rather than run on
numbers of unknown provenance. Refusing to guess is the whole posture.
"""

from __future__ import annotations

import json
from typing import Mapping

from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.usage import WorkerUsage

#: The three shapes every major provider uses, as ``(container, input key, output key)``.
#: Tried in order; the first container present in the body wins. Listed explicitly rather
#: than sniffed by key name so an unrecognised body reads as unmeasured instead of being
#: pattern-matched into a plausible-looking wrong answer.
_SHAPES: tuple[tuple[str, str, str], ...] = (
    # OpenAI chat/completions, and every API that clones it (Azure OpenAI, OpenRouter,
    # Together, Groq, vLLM, llama.cpp's server).
    ("usage", "prompt_tokens", "completion_tokens"),
    # Anthropic messages.
    ("usage", "input_tokens", "output_tokens"),
    # Google Gemini generateContent.
    ("usageMetadata", "promptTokenCount", "candidatesTokenCount"),
)


def extract_provider_usage(
    body: bytes, *, reported_by: str, wallclock_ms: int | None = None,
) -> WorkerUsage | None:
    """Token counts from one provider response, or ``None`` when they cannot be read.

    ``None`` covers a non-JSON body, a body with no usage block, a shape this does not know,
    and any value that is not a plain non-negative integer. A provider that reports a negative
    or non-integer count is either broken or hostile, and either way its numbers must not enter
    a spend total — so the attempt reads as unmeasured and a budgeted node refuses it.
    """
    document = _parse(body)
    if document is None:
        return None
    for container, input_key, output_key in _SHAPES:
        block = document.get(container)
        if not isinstance(block, Mapping):
            continue
        input_tokens, input_ok = _count(block.get(input_key))
        output_tokens, output_ok = _count(block.get(output_key))
        if not input_ok or not output_ok:
            # A field is PRESENT but not a usable count: negative, fractional, a string, a
            # bool. Absent and invalid are not the same thing — dropping just the bad field
            # and keeping its neighbour would manufacture a "partial measurement" out of a
            # response that has already shown it cannot be trusted. Discard the whole block.
            break
        if input_tokens is None and output_tokens is None:
            # Right container, wrong dialect — keep looking. Anthropic and OpenAI share the
            # key "usage" with different field names, so the first match on the container
            # name is not necessarily the right shape.
            continue
        if not input_tokens and not output_tokens:
            # Present, but both zero. A real call cannot consume zero input tokens, so this is
            # not the dialect that carries this response's counts — a proxy that emits BOTH
            # dialects put OpenAI's zeros first and hid Anthropic's real 9999/9999, which
            # under-charges. Keep looking; if every shape is zero the caller ends up with no
            # token counts at all, and a declared token cap then fails closed.
            continue
        try:
            return WorkerUsage(
                input_tokens=input_tokens, output_tokens=output_tokens,
                wallclock_ms=wallclock_ms, reported_by=reported_by,
            )
        except GraphIntegrityError:
            # Belt and braces: _count has already screened the values, so reaching here means
            # WorkerUsage enforces something this module does not know about. Unmeasured rather
            # than propagated, and never an exception — a bad response must not crash a run.
            break
    if wallclock_ms is None:
        return None
    # No usable provider counts, but elapsed time was measured HERE, by us, and is unaffected
    # by whatever the provider sent. Reporting it is honest and useful; it does NOT satisfy a
    # token or cost cap, because measurability is checked per dimension.
    return WorkerUsage(wallclock_ms=wallclock_ms, reported_by=reported_by)


def _parse(body: bytes) -> Mapping[str, object] | None:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, Mapping) or not all(isinstance(key, str) for key in document):
        return None
    return document


def _count(value: object) -> tuple[int | None, bool]:
    """``(count, usable)`` — distinguishing an ABSENT field from an INVALID one.

    ``(None, True)``  the provider did not report this quantity; legitimate.
    ``(None, False)`` the provider reported something that is not a count; the block is junk.

    Collapsing those two into a bare ``None`` is what let a response containing a negative
    count be read as a partial measurement of the field next to it.
    """
    if value is None:
        return (None, True)
    # bool first: it is an int subclass, so ``true`` in a JSON body would otherwise read as
    # one token.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return (None, False)
    return (value, True)
