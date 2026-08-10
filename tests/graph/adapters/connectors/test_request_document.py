"""ConnectorRequestDocument codec + validation (RB)."""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.connectors.request_document import (
    ConnectorRequestDocument,
    decode_request_document,
    encode_request_document,
)
from bounded_loops.graph.domain.errors import GraphValidationError


def test_roundtrip_preserves_every_field():
    doc = ConnectorRequestDocument(
        path="/v1/messages",
        headers={"content-type": "application/json", "x-trace": "abc"},
        body=b'{"model":"x"}',
        scheme="https",
    )
    restored = decode_request_document(encode_request_document(doc))
    assert restored.path == "/v1/messages"
    assert restored.scheme == "https"
    assert dict(restored.headers) == {"content-type": "application/json", "x-trace": "abc"}
    assert restored.body == b'{"model":"x"}'


def test_encoding_is_canonical_regardless_of_header_order():
    a = ConnectorRequestDocument(path="/p", headers={"b": "2", "a": "1"}, body=b"x")
    b = ConnectorRequestDocument(path="/p", headers={"a": "1", "b": "2"}, body=b"x")
    assert encode_request_document(a) == encode_request_document(b)


def test_scheme_defaults_to_https():
    assert ConnectorRequestDocument(path="/p", headers={}, body=b"").scheme == "https"


def test_scheme_is_case_normalized():
    assert ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="HTTPS").scheme == "https"


def test_empty_body_roundtrips():
    doc = ConnectorRequestDocument(path="/p", headers={}, body=b"")
    assert decode_request_document(encode_request_document(doc)).body == b""


@pytest.mark.parametrize("path", ["relative", "//evil.example", "/has space", "/bad\r\nInjected: 1", ""])
def test_rejects_unsafe_path(path):
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path=path, headers={}, body=b"")


@pytest.mark.parametrize("scheme", ["ftp", "", "https ", "file"])
def test_rejects_bad_scheme(scheme):
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme=scheme)


@pytest.mark.parametrize("name", ["Host", "content-length", "Connection", "Transfer-Encoding"])
def test_rejects_reserved_transport_headers(name):
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path="/p", headers={name: "x"}, body=b"")


@pytest.mark.parametrize("name", ["Authorization", "x-api-key", "api-key", "Proxy-Authorization"])
def test_rejects_credential_headers_in_a_document(name):
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path="/p", headers={name: "secret"}, body=b"")


def test_rejects_crlf_in_a_header_value():
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path="/p", headers={"x-foo": "a\r\nX-Evil: 1"}, body=b"")


def test_rejects_a_non_token_header_name():
    with pytest.raises(GraphValidationError):
        ConnectorRequestDocument(path="/p", headers={"bad name": "1"}, body=b"")


def test_decode_rejects_non_json():
    with pytest.raises(GraphValidationError):
        decode_request_document(b"not json {")


def test_decode_rejects_a_non_object():
    with pytest.raises(GraphValidationError):
        decode_request_document(b'"a string"')


def test_decode_rejects_bad_base64():
    with pytest.raises(GraphValidationError):
        decode_request_document(b'{"path":"/p","headers":{},"scheme":"https","body_b64":"@@@notbase64@@@"}')


def test_decode_cannot_smuggle_a_reserved_header():
    raw = b'{"path":"/p","headers":{"Host":"evil"},"scheme":"https","body_b64":""}'
    with pytest.raises(GraphValidationError):
        decode_request_document(raw)
