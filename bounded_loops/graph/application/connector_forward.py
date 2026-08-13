"""Frontier-connector invocation over the no-secret egress broker (C1, ADR-12 D5).

The seam that lets a node actually reach its ADMITTED connector without the graph
ever holding a credential. Every step is fail-closed:

    grant --mint_lease--> single-use lease --egress_broker.authorize--> pinned IPs
         --ConnectorForwardPort.forward--> content-addressed result

The controller only ever holds the opaque lease and a content-addressed request
digest. Credential injection AND byte forwarding are performed by a separate,
DEPLOYMENT-OWNED forwarder (``ConnectorForwardPort``) that:
  * resolves the credential for the lease's ``binding_id`` from a local
    keychain / KMS out-of-band (never via the graph), and
  * connects ONLY to the broker's PINNED addresses (never re-resolving the host),
    fetching the request body from the artifact store by its digest.
No credential value and no request/response bytes ever pass through this module.
Local models are just one connector among frontier connectors here — the product
invokes nothing by default; an author binds an admitted connection, and this path
carries it. Frontier connectors are first-class; a local model is only a connector
whose transport happens to be on-box.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Protocol

from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.application.egress_broker import EgressBroker, EgressRequest
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease, ExecutionGrant
from bounded_loops.graph.domain.usage import WorkerUsage
from bounded_loops.graph.domain.errors import GraphValidationError

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ConnectorInvocation:
    """One request a node wants its connector to make.

    Carries only a content-addressed ``payload_digest`` (the request body lives in
    the artifact store), the grant/lease-authorized ``destination``, and the request
    specifics — never inline bytes and never a credential.
    """

    destination: str
    method: str
    effect: Effect
    payload_digest: str
    declared_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.destination, str) or not self.destination:
            raise GraphValidationError("connector_invocation", "/destination", "destination is required")
        if not isinstance(self.method, str) or not self.method:
            raise GraphValidationError("connector_invocation", "/method", "method is required")
        if not isinstance(self.effect, Effect):
            raise GraphValidationError("connector_invocation", "/effect", "effect must be an Effect")
        if not isinstance(self.payload_digest, str) or not _DIGEST.fullmatch(self.payload_digest):
            raise GraphValidationError("connector_invocation", "/payload_digest", "payload_digest must be a sha256 digest")
        if isinstance(self.declared_bytes, bool) or not isinstance(self.declared_bytes, int) or self.declared_bytes < 0:
            raise GraphValidationError("connector_invocation", "/declared_bytes", "declared_bytes must be a non-negative int")


@dataclass(frozen=True)
class ConnectorResult:
    """A forwarder's content-addressed result — no inline response bytes.

    ``usage`` is the one thing read OUT of the response body and carried back inline, and it
    is integers only. A token count is metering metadata, not content, so it does not breach
    the no-response-bytes rule — and without it spend could not be metered at all, since the
    forwarder is the only component that ever holds the body.
    """

    ok: bool
    reason: str
    response_digest: str | None = None
    provider_status: int | None = None
    usage: WorkerUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise GraphValidationError("connector_result", "/ok", "ok must be a bool")
        if not isinstance(self.reason, str):
            raise GraphValidationError("connector_result", "/reason", "reason must be a string")
        if self.response_digest is not None and (
            not isinstance(self.response_digest, str) or not _DIGEST.fullmatch(self.response_digest)
        ):
            raise GraphValidationError("connector_result", "/response_digest", "response_digest must be a sha256 digest")
        if self.provider_status is not None and (
            isinstance(self.provider_status, bool) or not isinstance(self.provider_status, int)
        ):
            raise GraphValidationError("connector_result", "/provider_status", "provider_status must be an int")
        if self.usage is not None and not isinstance(self.usage, WorkerUsage):
            # A bare dict here would bypass WorkerUsage's own validation, which is the only
            # thing standing between a spend total and a negative charge that refunds budget.
            raise GraphValidationError("connector_result", "/usage", "usage must be a WorkerUsage")


class ConnectorForwardPort(Protocol):
    """A deployment-owned forwarder.

    It receives an already-AUTHORIZED single-use lease and the broker's PINNED
    addresses, resolves the credential for the lease's ``binding_id`` from a local
    keychain / KMS out-of-band, fetches the request body from the artifact store by
    digest, and connects ONLY to a pinned address (never re-resolving the host). No
    credential and no bytes flow through the graph.
    """

    def forward(
        self, *, lease: CredentialLease, invocation: ConnectorInvocation, pinned_ips: tuple[str, ...],
    ) -> ConnectorResult: ...


class ConnectorInvoker:
    """Orchestrate a fail-closed, no-secret connector call.

    Mint a single-use lease from the grant, authorize egress through the broker
    (obtaining the pinned addresses), and only then hand the opaque lease + pinned
    addresses to the deployment-owned forwarder. This object never touches a
    credential value, never sees request/response bytes, and forwards only after the
    broker has authorized — so a denied request, a refused lease, or a broken
    forwarder is a closed failure, never a silent egress.
    """

    def __init__(
        self,
        *,
        credential_broker: OpaqueCredentialBroker,
        egress_broker: EgressBroker,
        forwarder: ConnectorForwardPort,
    ) -> None:
        self._credential_broker = credential_broker
        self._egress_broker = egress_broker
        self._forwarder = forwarder

    def invoke(
        self,
        *,
        grant: ExecutionGrant,
        invocation: ConnectorInvocation,
        run_id: str,
        node_id: str,
        attempt: int,
        now: datetime | None = None,
    ) -> ConnectorResult:
        try:
            lease = self._credential_broker.mint_lease(
                grant,
                run_id=run_id,
                node_id=node_id,
                attempt=attempt,
                effect=invocation.effect,
                destination=invocation.destination,
                now=now,
            )
        except GraphValidationError as exc:
            return ConnectorResult(False, f"credential lease refused: {exc.message}")
        decision = self._egress_broker.authorize(
            lease=lease,
            request=EgressRequest(
                destination=invocation.destination,
                method=invocation.method,
                effect=invocation.effect,
                declared_bytes=invocation.declared_bytes,
            ),
            now=now,
        )
        if not decision.allowed:
            return ConnectorResult(False, f"egress denied: {decision.reason}")
        try:
            result = self._forwarder.forward(
                lease=lease, invocation=invocation, pinned_ips=decision.pinned_ips,
            )
        except Exception:  # noqa: BLE001 — a forwarder failure is closed, never a silent success
            return ConnectorResult(False, "connector forward failed")
        if not isinstance(result, ConnectorResult):
            return ConnectorResult(False, "connector forwarder returned an invalid result")
        return result
