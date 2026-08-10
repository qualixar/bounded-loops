"""No-secret egress broker — authorize one outbound request behind a lease (C1, ADR-12 D5).

The broker is the controller-owned credential proxy that a ``CredentialLease``
references. A worker or transport that wants to reach an external destination
presents the lease plus the concrete request; the broker decides — FAIL-CLOSED —
whether the request is authorized and to which PINNED public address(es) the
forwarder may connect. It never hands a credential value to the node: the actual
credential injection and byte forwarding are performed by a separate,
deployment-owned forwarder that consumes this decision (and the lease's
``binding_id``) against a local keychain / KMS. The node process only ever holds
the opaque lease.

Every guarantee is fail-closed:
  * destination-bound — the request destination must equal the lease destination.
  * single-use        — a ``lease_id`` authorizes exactly one request.
  * time-bound        — an expired (or unparseable-expiry) lease is refused.
  * effect-bound      — the request's effect must be one the lease grants.
  * method / size     — only an allowed method and at most ``max_bytes``.
  * SSRF / DNS-rebind — the host is resolved ONCE; every resolved address must be a
    public unicast address (private / loopback / link-local / multicast / reserved /
    unspecified, and IPv4-mapped forms of those, are refused). The forwarder must
    connect to the PINNED addresses the broker returns and must NOT re-resolve, so a
    rebind between check and connect cannot redirect to an internal address.
Any resolver error, empty result, or mixed public/non-public result denies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import socket
from typing import Protocol

from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease

_DEFAULT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class EgressRequest:
    """One concrete outbound request a node wants to make."""

    destination: str  # the authorized allowlist key; must equal the lease destination
    host: str  # the hostname to resolve
    port: int
    method: str
    effect: Effect
    declared_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.destination, str) or not self.destination:
            raise ValueError("egress request requires a destination")
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("egress request requires a host to resolve")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError("egress request port must be in 1..65535")
        if not isinstance(self.method, str) or not self.method:
            raise ValueError("egress request requires a method")
        if not isinstance(self.effect, Effect):
            raise ValueError("egress request effect must be an Effect")
        if isinstance(self.declared_bytes, bool) or not isinstance(self.declared_bytes, int) or self.declared_bytes < 0:
            raise ValueError("declared_bytes must be a non-negative int")


@dataclass(frozen=True)
class EgressDecision:
    """The broker's fail-closed verdict for one request."""

    allowed: bool
    reason: str
    pinned_ips: tuple[str, ...] = ()


class NameResolver(Protocol):
    def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemResolver:
    """``getaddrinfo``-based resolver. Resolving a name is a lookup, not egress; the
    broker still refuses any non-public result and pins what was resolved here."""

    def resolve(self, host: str, port: int) -> tuple[str, ...]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        addresses = (str(info[4][0]) for info in infos)  # sockaddr[0] is the address
        return tuple(dict.fromkeys(addresses))  # de-dup, preserve order


def _is_public_unicast(ip: str) -> bool:
    """True only for a globally-routable public unicast address, unwrapping any
    IPv4-mapped IPv6 form so ``::ffff:10.0.0.1`` is judged as ``10.0.0.1``."""
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class EgressBroker:
    """Authorize outbound requests behind opaque single-use leases, fail-closed."""

    def __init__(
        self,
        *,
        resolver: NameResolver | None = None,
        allowed_methods: frozenset[str] = _DEFAULT_METHODS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self._resolver = resolver if resolver is not None else SystemResolver()
        self._allowed_methods = frozenset(m.upper() for m in allowed_methods)
        self._max_bytes = max_bytes
        self._consumed: set[str] = set()

    def authorize(
        self, *, lease: CredentialLease, request: EgressRequest, now: datetime | None = None,
    ) -> EgressDecision:
        moment = now if now is not None else datetime.now(timezone.utc)
        try:
            expires = _parse_iso(lease.expires_at)
        except ValueError:
            return EgressDecision(False, "lease has an unparseable expiry")
        if moment >= expires:
            return EgressDecision(False, "lease has expired")
        if lease.lease_id in self._consumed:
            return EgressDecision(False, "lease already consumed (single-use)")
        if request.destination != lease.destination:
            return EgressDecision(False, "request destination is not the lease's authorized destination")
        if request.effect not in lease.effects:
            return EgressDecision(False, "request effect is not authorized by the lease")
        if request.method.upper() not in self._allowed_methods:
            return EgressDecision(False, f"method {request.method!r} is not allowed")
        if request.declared_bytes > self._max_bytes:
            return EgressDecision(False, "request exceeds the maximum egress byte cap")
        try:
            resolved = self._resolver.resolve(request.host, request.port)
        except Exception:  # noqa: BLE001 — any resolver failure denies (fail-closed)
            return EgressDecision(False, "destination host could not be resolved")
        if not resolved:
            return EgressDecision(False, "destination host did not resolve to any address")
        if not all(_is_public_unicast(ip) for ip in resolved):
            return EgressDecision(False, "destination resolves to a non-public address (SSRF denied)")
        # Authorized: consume the single-use lease and pin the resolved addresses.
        self._consumed.add(lease.lease_id)
        return EgressDecision(True, "", tuple(resolved))
