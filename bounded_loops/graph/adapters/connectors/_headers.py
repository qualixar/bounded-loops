"""Shared HTTP header validation — one source of truth for the connector adapters.

Both the request-document codec and the credential resolver validate header names and
values through here, so the CRLF / NUL / request-smuggling defenses cannot drift apart.
A header a forwarder controls itself (Host, Content-Length, and the hop-by-hop framing
headers) is refused from either source — neither a request document nor a credential may
set it.
"""

from __future__ import annotations

import re

from bounded_loops.graph.domain.errors import GraphValidationError

# RFC 7230 field-name token: 1*tchar.
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CONTROL = ("\r", "\n", "\x00")

# Headers the forwarder sets itself; neither a document nor a credential may supply them
# (setting Content-Length / Transfer-Encoding is request smuggling; Host is SSRF surface).
RESERVED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})


def reject_control(value: str, pointer: str, label: str) -> None:
    """Refuse CR, LF, or NUL anywhere in ``value`` — the header-injection defense."""
    if any(char in value for char in _CONTROL):
        raise GraphValidationError("connector_header", pointer, f"{label} must not contain CR, LF, or NUL")


def validate_header_name(name: str, pointer: str) -> None:
    if not isinstance(name, str) or not _TOKEN.fullmatch(name):
        raise GraphValidationError("connector_header", pointer, "header name must be a non-empty RFC 7230 token")
    if name.lower() in RESERVED_HEADERS:
        raise GraphValidationError("connector_header", pointer, f"header {name!r} is set by the forwarder and must not be supplied")


def validate_header_value(value: str, pointer: str) -> None:
    if not isinstance(value, str):
        raise GraphValidationError("connector_header", pointer, "header value must be a string")
    reject_control(value, pointer, "header value")
