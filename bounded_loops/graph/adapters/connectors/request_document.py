"""Canonical, content-addressed connector request document (RB).

A connector node's request specifics BEYOND the broker-authorized ``(destination, method)``
— the URL path, non-secret request headers, the request body, and the scheme — live in
this document, serialized canonically and stored in the artifact store. The invocation
carries only the document's digest, so the request is content-addressed and tamper-evident.

Host and port are NEVER taken from the document (they come only from the lease-authorized
destination). The document cannot smuggle a Host header, a protocol-relative or absolute
path, a CRLF-injected header, or a credential header — every such attempt is refused, so a
request can neither be redirected to another host nor carry a secret inside an artifact.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from types import MappingProxyType
from typing import Mapping

from bounded_loops.graph.adapters.connectors._headers import (
    reject_control,
    validate_header_name,
    validate_header_value,
)
from bounded_loops.graph.domain.errors import GraphValidationError

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# Credentials belong on the resolver path, never inside a stored request artifact.
_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


@dataclass(frozen=True)
class ConnectorRequestDocument:
    """The full HTTP request layered on top of an authorized ``(destination, method)``."""

    path: str
    headers: Mapping[str, str]
    body: bytes
    scheme: str = "https"

    def __post_init__(self) -> None:
        if not isinstance(self.scheme, str) or self.scheme.lower() not in _ALLOWED_SCHEMES:
            raise GraphValidationError("connector_request", "/scheme", "scheme must be http or https")
        object.__setattr__(self, "scheme", self.scheme.lower())
        if not isinstance(self.path, str) or not self.path.startswith("/") or self.path.startswith("//"):
            raise GraphValidationError("connector_request", "/path", "path must be an absolute path beginning with a single '/'")
        reject_control(self.path, "/path", "path")
        if " " in self.path:
            raise GraphValidationError("connector_request", "/path", "path must not contain a space")
        if not isinstance(self.body, (bytes, bytearray)):
            raise GraphValidationError("connector_request", "/body", "body must be bytes")
        if not isinstance(self.headers, Mapping):
            raise GraphValidationError("connector_request", "/headers", "headers must be a mapping")
        cleaned: dict[str, str] = {}
        seen: set[str] = set()
        for name, value in self.headers.items():
            validate_header_name(name, "/headers")
            if name.lower() in _CREDENTIAL_HEADERS:
                raise GraphValidationError("connector_request", "/headers", "credential headers must come from the resolver, not a request document")
            if name.lower() in seen:
                raise GraphValidationError("connector_request", "/headers", "duplicate header name")
            validate_header_value(value, "/headers")
            seen.add(name.lower())
            cleaned[name] = value
        object.__setattr__(self, "headers", MappingProxyType(cleaned))
        object.__setattr__(self, "body", bytes(self.body))


def encode_request_document(document: ConnectorRequestDocument) -> bytes:
    """Serialize canonically (sorted keys, base64 body) so the digest is deterministic."""
    payload = {
        "body_b64": base64.b64encode(document.body).decode("ascii"),
        "headers": {name: document.headers[name] for name in sorted(document.headers)},
        "path": document.path,
        "scheme": document.scheme,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def decode_request_document(data: bytes) -> ConnectorRequestDocument:
    """Parse + re-validate a stored request document; any malformed field fails closed."""
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphValidationError("connector_request", "/document", "request document is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise GraphValidationError("connector_request", "/document", "request document must be a JSON object")
    body_b64 = raw.get("body_b64", "")
    if not isinstance(body_b64, str):
        raise GraphValidationError("connector_request", "/body", "body must be base64 text")
    try:
        body = base64.b64decode(body_b64, validate=True) if body_b64 else b""
    except ValueError as exc:
        raise GraphValidationError("connector_request", "/body", "body is not valid base64") from exc
    headers = raw.get("headers", {})
    if not isinstance(headers, dict):
        raise GraphValidationError("connector_request", "/headers", "headers must be a JSON object")
    return ConnectorRequestDocument(
        path=raw.get("path", ""),
        headers=headers,
        body=body,
        scheme=raw.get("scheme", "https"),
    )
