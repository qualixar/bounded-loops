"""Real BYOK HTTP forwarder — end-to-end against a localhost mock provider + SSRF/security (RB).

Every network test runs against a real HTTP (or real self-signed TLS) server bound to
127.0.0.1 — no external egress, no real credential. The ``example.invalid`` destination in
the pinning test can never be resolved, so a request that arrives PROVES the forwarder used
the pinned IP and never performed a DNS lookup.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.connectors.credentials import MappingCredentialResolver, ProviderCredential
from bounded_loops.graph.adapters.connectors.http_forwarder import HttpConnectorForwarder
from bounded_loops.graph.adapters.connectors.request_document import ConnectorRequestDocument, encode_request_document
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.connector_forward import ConnectorInvocation
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease
from bounded_loops.graph.domain.errors import GraphValidationError


class _MockProvider:
    """A real HTTP(S) server on 127.0.0.1 that records the request and returns a canned reply."""

    def __init__(self, *, status: int = 200, body: bytes = b'{"ok":true}',
                 location: str | None = None, tls: tuple[Path, Path] | None = None) -> None:
        self.received: list[dict] = []
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length) if length else b""
                provider.received.append({
                    "method": self.command,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": payload,
                })
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def log_message(self, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(tls[0]), keyfile=str(tls[1]))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockProvider":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Generate a throwaway self-signed localhost certificate with openssl (skip if absent)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not available for a live TLS handshake test")
    directory = tmp_path_factory.mktemp("tls")
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    completed = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(cert),
         "-days", "3650", "-nodes", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        capture_output=True,
    )
    if completed.returncode != 0:
        pytest.skip("openssl could not generate a test certificate")
    return cert, key


def _artifact_body(tmp_path: Path) -> LocalArtifactBody:
    store = LocalArtifactStore(tmp_path / "artifacts")
    return LocalArtifactBody(store, organization_id="o", project_id="p", producer_attempt="attempt-1")


def _lease(destination: str, *, binding_id: str = "binding-1") -> CredentialLease:
    return CredentialLease(
        lease_id="lease:x", grant_id="grant:x", run_id="run-1", node_id="n1", attempt=1,
        connection_id="conn-1", binding_id=binding_id, effects=frozenset({Effect.EXTERNAL_WRITE}),
        destination=destination, expires_at="2999-01-01T00:00:00+00:00",
    )


def _invocation(body: LocalArtifactBody, doc: ConnectorRequestDocument, destination: str) -> ConnectorInvocation:
    digest = body.store(encode_request_document(doc))
    return ConnectorInvocation(destination=destination, method="POST", effect=Effect.EXTERNAL_WRITE, payload_digest=digest)


def test_forwards_to_the_pinned_ip_without_resolving_the_host(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider(body=b'{"reply":"hi"}') as provider:
        destination = f"example.invalid:{provider.port}"  # unresolvable -> proves no DNS lookup
        doc = ConnectorRequestDocument(path="/v1/echo", headers={"content-type": "application/json"}, body=b'{"q":1}', scheme="http")
        invocation = _invocation(body, doc, destination)
        result = HttpConnectorForwarder(artifact_body=body).forward(
            lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",),
        )
    assert result.ok is True
    assert result.provider_status == 200
    assert result.response_digest is not None
    assert body.fetch(result.response_digest) == b'{"reply":"hi"}'
    assert len(provider.received) == 1
    seen = provider.received[0]
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/echo"
    assert seen["host"] == f"example.invalid:{provider.port}"  # Host = destination, not the pinned IP
    assert seen["body"] == b'{"q":1}'
    assert seen["headers"]["content-type"] == "application/json"


def test_sends_a_credential_over_tls_and_verifies_the_certificate(tmp_path: Path, tls_cert: tuple[Path, Path]):
    cert, key = tls_cert
    body = _artifact_body(tmp_path)
    fake_key = "test-key-not-real-9999"
    resolver = MappingCredentialResolver({"binding-1": ProviderCredential({"x-api-key": fake_key})})
    client_context = ssl.create_default_context(cafile=str(cert))
    with _MockProvider(body=b'{"tls":true}', tls=(cert, key)) as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/v1/messages", headers={"content-type": "application/json"}, body=b'{"m":1}', scheme="https")
        invocation = _invocation(body, doc, destination)
        forwarder = HttpConnectorForwarder(artifact_body=body, credential_resolver=resolver, tls_context=client_context)
        result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is True and result.provider_status == 200
    seen = provider.received[0]
    assert seen["headers"]["x-api-key"] == fake_key  # credential reached the wire over TLS
    assert seen["host"] == f"localhost:{provider.port}"


def test_tls_rejects_a_hostname_mismatch(tmp_path: Path, tls_cert: tuple[Path, Path]):
    cert, key = tls_cert
    body = _artifact_body(tmp_path)
    client_context = ssl.create_default_context(cafile=str(cert))
    with _MockProvider(body=b"{}", tls=(cert, key)) as provider:
        destination = f"wrong-host.invalid:{provider.port}"  # cert is for localhost, not this name
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="https")
        invocation = _invocation(body, doc, destination)
        forwarder = HttpConnectorForwarder(artifact_body=body, tls_context=client_context)
        result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is False  # SNI/cert hostname mismatch -> handshake fails -> closed


def test_tls_rejects_an_untrusted_certificate(tmp_path: Path, tls_cert: tuple[Path, Path]):
    cert, key = tls_cert
    body = _artifact_body(tmp_path)
    with _MockProvider(body=b"{}", tls=(cert, key)) as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="https")
        invocation = _invocation(body, doc, destination)
        forwarder = HttpConnectorForwarder(artifact_body=body)  # default context does not trust the self-signed cert
        result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is False


def test_refuses_to_send_a_credential_over_http(tmp_path: Path):
    body = _artifact_body(tmp_path)
    resolver = MappingCredentialResolver({"binding-1": ProviderCredential({"x-api-key": "test-key-not-real"})})
    with _MockProvider() as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="http")
        invocation = _invocation(body, doc, destination)
        forwarder = HttpConnectorForwarder(artifact_body=body, credential_resolver=resolver)
        result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is False
    assert "non-TLS" in result.reason
    assert provider.received == []  # nothing was ever sent


def test_no_pinned_address_is_closed(tmp_path: Path):
    body = _artifact_body(tmp_path)
    doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="http")
    invocation = _invocation(body, doc, "localhost:1")
    result = HttpConnectorForwarder(artifact_body=body).forward(
        lease=_lease("localhost:1"), invocation=invocation, pinned_ips=(),
    )
    assert result.ok is False and "no pinned" in result.reason


def test_rejects_a_non_ip_pinned_address(tmp_path: Path):
    body = _artifact_body(tmp_path)
    doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="http")
    invocation = _invocation(body, doc, "localhost:80")
    result = HttpConnectorForwarder(artifact_body=body).forward(
        lease=_lease("localhost:80"), invocation=invocation, pinned_ips=("not-an-ip",),
    )
    assert result.ok is False and "literal IP" in result.reason


def test_unreachable_pinned_address_is_closed(tmp_path: Path):
    body = _artifact_body(tmp_path)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    destination = f"localhost:{dead_port}"
    doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="http")
    invocation = _invocation(body, doc, destination)
    forwarder = HttpConnectorForwarder(artifact_body=body, timeout=2.0)
    result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is False and "could not reach" in result.reason


def test_non_2xx_is_closed_but_the_response_is_stored(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider(status=503, body=b'{"err":"down"}') as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"{}", scheme="http")
        invocation = _invocation(body, doc, destination)
        result = HttpConnectorForwarder(artifact_body=body).forward(
            lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",),
        )
    assert result.ok is False and result.provider_status == 503
    assert result.response_digest is not None
    assert body.fetch(result.response_digest) == b'{"err":"down"}'


def test_does_not_follow_redirects(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider(status=302, body=b"", location="http://evil.invalid/") as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"{}", scheme="http")
        invocation = _invocation(body, doc, destination)
        result = HttpConnectorForwarder(artifact_body=body).forward(
            lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",),
        )
    assert result.provider_status == 302 and result.ok is False
    assert len(provider.received) == 1  # only the original request; the Location was not followed


def test_a_response_over_the_cap_is_closed(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider(body=b"x" * 100) as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"{}", scheme="http")
        invocation = _invocation(body, doc, destination)
        forwarder = HttpConnectorForwarder(artifact_body=body, max_response_bytes=16)
        result = forwarder.forward(lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",))
    assert result.ok is False and "exceeded" in result.reason


def test_headers_apply_the_credential_and_it_wins_over_the_document():
    doc = ConnectorRequestDocument(path="/p", headers={"content-type": "application/json", "x-role": "doc"}, body=b"")
    cred = ProviderCredential({"x-api-key": "test-key-not-real", "x-role": "cred"})
    headers = HttpConnectorForwarder._headers(doc, cred)
    assert headers["content-type"] == "application/json"
    assert headers["x-api-key"] == "test-key-not-real"
    assert headers["x-role"] == "cred"  # the credential wins over a colliding document header
    assert headers["Connection"] == "close"


def test_the_default_tls_context_verifies_certificates(tmp_path: Path):
    forwarder = HttpConnectorForwarder(artifact_body=_artifact_body(tmp_path))
    assert forwarder._tls_context.check_hostname is True
    assert forwarder._tls_context.verify_mode == ssl.CERT_REQUIRED


def test_rejects_an_injected_tls_context_that_skips_verification(tmp_path: Path):
    body = _artifact_body(tmp_path)
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(GraphValidationError):
        HttpConnectorForwarder(artifact_body=body, tls_context=insecure)


def test_rejects_a_method_bearing_crlf(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider() as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"", scheme="http")
        digest = body.store(encode_request_document(doc))
        invocation = ConnectorInvocation(
            destination=destination, method="POST\r\nX-Evil: 1", effect=Effect.EXTERNAL_WRITE, payload_digest=digest,
        )
        result = HttpConnectorForwarder(artifact_body=body).forward(
            lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.1",),
        )
    assert result.ok is False and "method" in result.reason
    assert provider.received == []  # the smuggled request line was never sent


def test_fails_over_to_the_next_pinned_address(tmp_path: Path):
    body = _artifact_body(tmp_path)
    with _MockProvider(body=b'{"ok":1}') as provider:
        destination = f"localhost:{provider.port}"
        doc = ConnectorRequestDocument(path="/p", headers={}, body=b"{}", scheme="http")
        invocation = _invocation(body, doc, destination)
        # nothing listens on 127.0.0.2:<port> -> connect refused -> fail over to 127.0.0.1
        forwarder = HttpConnectorForwarder(artifact_body=body, timeout=3.0)
        result = forwarder.forward(
            lease=_lease(destination), invocation=invocation, pinned_ips=("127.0.0.2", "127.0.0.1"),
        )
    assert result.ok is True and result.provider_status == 200
    assert len(provider.received) == 1  # sent exactly once, to the reachable address
