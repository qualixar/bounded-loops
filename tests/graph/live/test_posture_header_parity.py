"""Every response carries the same hardening set, and every unknown verb gets the same refusal.

Both properties were *almost* true, which is the interesting part. Each had been through a fix that
addressed the reported instance and left the general case open:

* The SSE stream was reported as skipping the shared hardening headers. The fix re-listed them
  inside `_send_sse_headers` rather than calling the shared helper — so two copies existed and
  immediately diverged. The stream omitted `Pragma: no-cache` and the whole `Content-Security-Policy`
  while its docstring asserted that no streaming surface could forget them.
* Non-standard verbs were reported as returning 501 instead of 405. Explicit `do_*` handlers were
  added for the standard verbs, which leaves `FROB` — or anything else — still reaching
  `BaseHTTPRequestHandler`'s 501 default, before any token check.

These tests assert the general property in both cases, so the next near-miss fails here.
"""

from __future__ import annotations

import inspect
import re

from bounded_loops.graph.live import posture

#: Headers the shared helper sends. Derived from the source rather than hardcoded, so adding one
#: to `_send_hardening_headers` automatically extends what the stream is required to carry.
_HEADER_CALL = re.compile(r'send_header\(\s*"([^"]+)"')


def _headers_sent_by(function) -> set[str]:
    return set(_HEADER_CALL.findall(inspect.getsource(function)))


def test_the_stream_sends_every_header_the_shared_helper_sends() -> None:
    """The parity check. It fails on a re-listed copy that has fallen behind.

    Satisfied either by calling `_send_hardening_headers` (what the code does now) or by listing
    every header verbatim — the test does not care how, only that nothing is missing. That is
    deliberate: pinning the implementation would forbid a future refactor that stays correct.
    """
    handler = posture.LoopbackHandler
    shared = _headers_sent_by(handler._send_hardening_headers)
    stream_source = inspect.getsource(handler._send_sse_headers)

    delegates = "_send_hardening_headers()" in stream_source
    listed = set(_HEADER_CALL.findall(stream_source))

    missing = set() if delegates else shared - listed
    assert not missing, (
        f"the SSE stream is a token-bearing response missing {sorted(missing)}. Call "
        "_send_hardening_headers() rather than maintaining a second copy of the list"
    )


def test_the_shared_helper_still_carries_the_headers_that_matter() -> None:
    """Guards the test above from passing vacuously.

    If `_send_hardening_headers` were ever emptied, the parity check would succeed against nothing.
    These four are the ones the token-in-query-string design depends on.
    """
    shared = _headers_sent_by(posture.LoopbackHandler._send_hardening_headers)

    for required in (
        "Referrer-Policy",          # the token lives in the query string
        "Cache-Control",            # never written to disk cache
        "Pragma",                   # the one the stream copy had dropped
        "Content-Security-Policy",  # the other one
    ):
        assert required in shared, f"{required} is no longer sent on every response"


def test_an_unlisted_verb_is_refused_the_same_way_as_a_listed_one() -> None:
    """`FROB` must reach the 405 path, not `BaseHTTPRequestHandler`'s 501 default.

    Asserted through attribute lookup, which is exactly how the base class dispatches
    (`getattr(self, "do_" + command)`), so this exercises the real mechanism rather than a
    stand-in for it.
    """
    handler = posture.LoopbackHandler.__new__(posture.LoopbackHandler)

    for verb in ("FROB", "PROPFIND", "MKCOL", "SEARCH", "BREW"):
        resolved = getattr(handler, f"do_{verb}", None)
        assert callable(resolved), (
            f"do_{verb} does not resolve, so the base class answers 501 — a response shape "
            "distinguishable from 405 and produced before any token check"
        )


def test_the_catch_all_does_not_swallow_unrelated_missing_attributes() -> None:
    """`__getattr__` intercepting everything would mask real typos in the handler.

    Only `do_*` is manufactured; anything else must raise as normal.
    """
    handler = posture.LoopbackHandler.__new__(posture.LoopbackHandler)

    for name in ("_send_hardnening_headers", "totally_unrelated", "handle_one_reqest"):
        try:
            getattr(handler, name)
        except AttributeError:
            continue
        raise AssertionError(f"{name} resolved; a typo would be silently swallowed")
