"""Real BYOK HTTP connector forwarder — the connector that actually makes the call (RB).

Implements ``ConnectorForwardPort`` on the Python standard library only (``http.client``,
``ssl``, ``socket``) — no third-party HTTP/TLS surface. Given an already-authorized
single-use lease, a content-addressed invocation, and the broker's PINNED public
addresses, it:
  * fetches the request DOCUMENT (path/headers/body/scheme) from the artifact store by
    ``payload_digest`` and decodes it — host and port are NEVER taken from the document,
  * resolves the provider credential for the lease's ``binding_id`` out-of-band,
  * connects the TCP socket ONLY to a PINNED address (which must be a literal IP, so no
    name is ever resolved — no DNS rebind), while presenting the destination host for the
    Host header, the TLS SNI, and certificate verification,
  * refuses to transmit a credential over a non-TLS connection,
  * does NOT follow redirects (a 3xx is returned as the provider status),
  * caps the response size and stores the response body, returning its digest.

No credential value and no request/response bytes are logged, stored in a receipt, or
returned in an error string. Any failure is a CLOSED ``ConnectorResult`` — never a silent
success.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time

from bounded_loops.graph.adapters.connectors.artifact_body import ArtifactBodyPort
from bounded_loops.graph.adapters.connectors.credentials import CredentialResolverPort, ProviderCredential
from bounded_loops.graph.adapters.connectors.request_document import ConnectorRequestDocument, decode_request_document
from bounded_loops.graph.adapters.connectors.provider_usage import extract_provider_usage
from bounded_loops.graph.application.connector_forward import ConnectorInvocation, ConnectorResult
from bounded_loops.graph.application.egress_broker import split_destination
from bounded_loops.graph.domain.connections import CredentialLease
from bounded_loops.graph.domain.errors import GraphValidationError

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_READ_CHUNK = 65536
_DEFAULT_PORTS = {"http": 80, "https": 443}
# HTTP methods are alphabetic tokens — a stricter guard than the invocation's, so a
# CRLF/space-bearing method can never reach the request line (defense in depth).
_METHOD = re.compile(r"^[A-Za-z]+$")


class _ForwardDenied(Exception):
    """A handled, safe-to-surface closed reason (never carries secret material)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 connection whose TCP socket goes ONLY to the pinned IP, while the Host
    header still names the authorized destination host."""

    def __init__(self, host: str, *, port: int, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection: TCP to the pinned IP; SNI + certificate verification bound to the
    authorized destination host (never the IP)."""

    def __init__(self, host: str, *, port: int, pinned_ip: str, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port=port, context=context, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._ssl_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)


class HttpConnectorForwarder:
    """A real ``ConnectorForwardPort`` over the standard library, fail-closed and SSRF-safe."""

    def __init__(
        self,
        *,
        artifact_body: ArtifactBodyPort,
        credential_resolver: CredentialResolverPort | None = None,
        tls_context: ssl.SSLContext | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._artifact_body = artifact_body
        self._credential_resolver = credential_resolver
        # A default context verifies the certificate against the hostname (check_hostname
        # True, verify_mode CERT_REQUIRED) — a pinned IP serving a cert for another host fails
        # the handshake and the call is closed. An INJECTED context must verify too: a
        # deployment cannot silently disable verification (a client-deliverable must never
        # ship a TLS-off footgun); a custom CA bundle is fine as long as it still verifies.
        if tls_context is None:
            self._tls_context = ssl.create_default_context()
        elif not tls_context.check_hostname or tls_context.verify_mode != ssl.CERT_REQUIRED:
            raise GraphValidationError(
                "http_forwarder", "/tls_context",
                "an injected TLS context must verify certificates (check_hostname and CERT_REQUIRED)",
            )
        else:
            self._tls_context = tls_context
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    def forward(
        self, *, lease: CredentialLease, invocation: ConnectorInvocation, pinned_ips: tuple[str, ...],
    ) -> ConnectorResult:
        try:
            return self._forward(lease=lease, invocation=invocation, pinned_ips=pinned_ips)
        except _ForwardDenied as denied:
            return ConnectorResult(False, denied.reason)
        except Exception:  # noqa: BLE001 — fail closed; never leak secret state via a message/traceback
            return ConnectorResult(False, "connector forward failed")

    def _forward(
        self, *, lease: CredentialLease, invocation: ConnectorInvocation, pinned_ips: tuple[str, ...],
    ) -> ConnectorResult:
        if not pinned_ips:
            raise _ForwardDenied("no pinned address supplied to the forwarder")
        for pinned_ip in pinned_ips:
            try:
                ipaddress.ip_address(pinned_ip)
            except ValueError as exc:  # a hostname here would mean a DNS lookup — refuse
                raise _ForwardDenied("pinned address is not a literal IP") from exc
        if not _METHOD.fullmatch(invocation.method):
            raise _ForwardDenied("unsupported HTTP method")
        host, port = split_destination(lease.destination)
        document = decode_request_document(self._artifact_body.fetch(invocation.payload_digest))
        credential = self._resolve_credential(lease)
        if credential is not None and document.scheme != "https":
            raise _ForwardDenied("refusing to transmit a credential over a non-TLS connection")
        effective_port = port if port is not None else _DEFAULT_PORTS[document.scheme]
        started = time.monotonic()
        status, body = self._exchange(host, effective_port, pinned_ips, invocation.method, document, credential)
        wallclock_ms = int((time.monotonic() - started) * 1000)
        response_digest = self._artifact_body.store(body)
        ok = 200 <= status < 300
        # Usage is read from the body HERE because this is the only place the body exists;
        # everything downstream holds a digest. Integers only — see provider_usage.
        usage = extract_provider_usage(
            body, reported_by=f"provider:{host}", wallclock_ms=wallclock_ms,
        )
        return ConnectorResult(
            ok, "" if ok else f"provider returned HTTP {status}", response_digest, status, usage,
        )

    def _resolve_credential(self, lease: CredentialLease) -> ProviderCredential | None:
        if self._credential_resolver is None:
            return None
        try:
            credential = self._credential_resolver.resolve(binding_id=lease.binding_id, destination=lease.destination)
        except GraphValidationError as exc:
            raise _ForwardDenied("provider credential could not be resolved") from exc
        if credential is not None and not isinstance(credential, ProviderCredential):
            raise _ForwardDenied("credential resolver returned an invalid credential")
        return credential

    def _exchange(
        self,
        host: str,
        port: int,
        pinned_ips: tuple[str, ...],
        method: str,
        document: ConnectorRequestDocument,
        credential: ProviderCredential | None,
    ) -> tuple[int, bytes]:
        headers = self._headers(document, credential)
        connection = self._first_reachable(host, port, pinned_ips, document.scheme)
        # Once a connection is established the request is sent EXACTLY ONCE; a failure
        # after this point is closed without re-sending to another address, so a
        # non-idempotent call is never silently duplicated.
        try:
            connection.request(method, document.path, body=document.body, headers=headers)
            response = connection.getresponse()
            return response.status, self._read_capped(response)
        finally:
            connection.close()

    def _first_reachable(
        self, host: str, port: int, pinned_ips: tuple[str, ...], scheme: str,
    ) -> http.client.HTTPConnection:
        """Connect to the first reachable pinned address, retrying ONLY connect/TLS-handshake
        failures — nothing has been sent yet, so trying the next address is safe."""
        for pinned_ip in pinned_ips:
            connection = self._connect(host, port, pinned_ip, scheme)
            try:
                connection.connect()
                return connection
            except (OSError, http.client.HTTPException):
                connection.close()  # this address is unreachable — try the next
        raise _ForwardDenied("could not reach any pinned address")

    def _connect(self, host: str, port: int, pinned_ip: str, scheme: str) -> http.client.HTTPConnection:
        if scheme == "https":
            return _PinnedHTTPSConnection(
                host, port=port, pinned_ip=pinned_ip, context=self._tls_context, timeout=self._timeout,
            )
        return _PinnedHTTPConnection(host, port=port, pinned_ip=pinned_ip, timeout=self._timeout)

    def _read_capped(self, response: http.client.HTTPResponse) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_response_bytes:
                raise _ForwardDenied("provider response exceeded the maximum size")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _headers(document: ConnectorRequestDocument, credential: ProviderCredential | None) -> dict[str, str]:
        headers: dict[str, str] = dict(document.headers)
        headers["Connection"] = "close"
        if credential is not None:
            for name, value in credential.headers.items():
                headers[name] = value  # the credential wins over any document header
        return headers
