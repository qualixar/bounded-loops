"""No-secret egress broker — authorize one outbound request behind a lease (C1, ADR-12 D5).

The broker is the controller-owned credential proxy that a ``CredentialLease``
references. A worker or transport that wants to reach an external destination
presents the lease; the broker decides — FAIL-CLOSED — whether that request is
authorized and to which PINNED public address(es) a forwarder may connect. It
never hands a credential value to the node: the credential injection and byte
forwarding are performed by a separate, deployment-owned forwarder that consumes
this decision (and the lease's ``binding_id``) against a local keychain / KMS. The
node process only ever holds the opaque lease.

The broker resolves the LEASE's OWN destination — the caller cannot supply a host
that diverges from what the lease authorized. Every guarantee is fail-closed:
  * single-use   — a ``lease_id`` authorizes exactly one request (atomically, even
    under concurrency); a denied request never burns the lease.
  * time-bound   — an expired (or unparseable-expiry) lease is refused.
  * effect-bound — the request's effect must be one the lease grants.
  * method / size — only an allowed method and at most ``max_bytes``.
  * SSRF / DNS-rebind — the lease destination's host is resolved ONCE; EVERY
    resolved address must be a globally-routable public unicast address
    (``is_global`` plus explicit refusal of private / loopback / link-local incl.
    169.254.169.254 / CGNAT / multicast / reserved / unspecified, and IPv4-mapped
    forms). The broker returns the PINNED addresses the forwarder must connect to
    without re-resolving, so a rebind between check and connect cannot redirect to
    an internal address.
Any resolver error, empty result, mixed public/non-public result, or unexpected
exception denies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import socket
import threading
from typing import Protocol

from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease

_DEFAULT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
_DEFAULT_MAX_BYTES = 8 * 1024 * 1024
# Ranges that report ``is_global == True`` yet must never be an egress target.
_EXTRA_DENY_NETWORKS = (
    ipaddress.ip_network("192.88.99.0/24"),  # 6to4 relay anycast (IPv4)
    ipaddress.ip_network("fec0::/10"),  # deprecated IPv6 site-local
    ipaddress.ip_network("2001:20::/28"),  # ORCHIDv2 (non-routable experimental)
)


@dataclass(frozen=True)
class EgressRequest:
    """One concrete outbound request a node wants to make.

    It names only the authorized ``destination`` (which must equal the lease's) and
    the request specifics; the host actually contacted is derived from the lease
    destination, never from caller-supplied input, so it cannot diverge.
    """

    destination: str
    method: str
    effect: Effect
    declared_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.destination, str) or not self.destination:
            raise ValueError("egress request requires a destination")
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
    def resolve(self, host: str, port: int | None) -> tuple[str, ...]: ...


class SystemResolver:
    """``getaddrinfo``-based resolver. Resolving a name is a lookup, not egress; the
    broker still refuses any non-public result and pins what was resolved here."""

    def resolve(self, host: str, port: int | None) -> tuple[str, ...]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        addresses = (str(info[4][0]) for info in infos)  # sockaddr[0] is the address
        return tuple(dict.fromkeys(addresses))  # de-dup, preserve order


def _is_public_unicast(ip: str) -> bool:
    """True only for a globally-routable public unicast address.

    ``is_global`` is the authoritative positive test (it excludes CGNAT 100.64/10,
    TEST-NET, and the rest of the IANA special-purpose registry); the explicit
    negations are belt-and-suspenders across Python versions. IPv4-mapped IPv6 is
    unwrapped so ``::ffff:10.0.0.1`` is judged as ``10.0.0.1``.
    """
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if not addr.is_global:
        return False
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return False
    return not any(addr.version == net.version and addr in net for net in _EXTRA_DENY_NETWORKS)


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("empty timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _split_destination(destination: str) -> tuple[str, int | None]:
    """Parse ``host`` / ``host:port`` / ``[ipv6]:port`` into (host, port). Raises on a
    malformed value so the broker denies fail-closed."""
    text = destination.strip()
    if not text:
        raise ValueError("empty destination")
    if text.startswith("["):  # bracketed IPv6, optionally with :port
        host, sep, rest = text[1:].partition("]")
        if not sep or not host:
            raise ValueError("malformed bracketed destination")
        if rest == "":
            return host, None
        if not rest.startswith(":"):
            raise ValueError("malformed bracketed destination")
        return host, _port(rest[1:])
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        if not host:
            raise ValueError("malformed destination")
        return host, _port(port_text)
    return text, None  # bare host (or bare IPv6 without a port)


def _port(text: str) -> int:
    port = int(text)  # ValueError propagates → deny
    if not (1 <= port <= 65535):
        raise ValueError("port out of range")
    return port


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
        self._lock = threading.Lock()
        self._consumed: dict[str, datetime] = {}  # lease_id -> expiry, pruned as leases expire

    def authorize(
        self, *, lease: CredentialLease, request: EgressRequest, now: datetime | None = None,
    ) -> EgressDecision:
        try:
            return self._authorize(lease=lease, request=request, now=now)
        except Exception:  # noqa: BLE001 — never fail open; any unexpected error denies
            return EgressDecision(False, "egress authorization failed closed on an unexpected error")

    def _authorize(
        self, *, lease: CredentialLease, request: EgressRequest, now: datetime | None,
    ) -> EgressDecision:
        moment = now if now is not None else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        try:
            expires = _parse_iso(lease.expires_at)
        except ValueError:
            return EgressDecision(False, "lease has an unparseable expiry")
        if moment >= expires:
            return EgressDecision(False, "lease has expired")
        if request.destination != lease.destination:
            return EgressDecision(False, "request destination is not the lease's authorized destination")
        if request.effect not in lease.effects:
            return EgressDecision(False, "request effect is not authorized by the lease")
        if request.method.upper() not in self._allowed_methods:
            return EgressDecision(False, f"method {request.method!r} is not allowed")
        if request.declared_bytes > self._max_bytes:
            return EgressDecision(False, "request exceeds the maximum egress byte cap")
        # Resolve the LEASE's own destination host — never caller-supplied input.
        try:
            host, port = _split_destination(lease.destination)
        except ValueError:
            return EgressDecision(False, "lease destination is malformed")
        try:
            resolved = self._resolver.resolve(host, port)
        except Exception:  # noqa: BLE001 — any resolver failure denies (fail-closed)
            return EgressDecision(False, "destination host could not be resolved")
        if not resolved:
            return EgressDecision(False, "destination host did not resolve to any address")
        if not all(_is_public_unicast(ip) for ip in resolved):
            return EgressDecision(False, "destination resolves to a non-public address (SSRF denied)")
        # Atomically consume the single-use lease (exactly-once even under concurrency).
        with self._lock:
            # Prune with the EARLIER of the caller's moment and the broker's own
            # clock, so a caller-supplied future `now` can never evict a still-valid
            # consumed lease (which would silently defeat single-use).
            self._prune(min(moment, datetime.now(timezone.utc)))
            if lease.lease_id in self._consumed:
                return EgressDecision(False, "lease already consumed (single-use)")
            self._consumed[lease.lease_id] = expires
        return EgressDecision(True, "", tuple(resolved))

    def _prune(self, moment: datetime) -> None:
        """Drop consumed entries whose lease has expired — bounds memory to live leases.
        Caller holds ``self._lock``."""
        expired = [lease_id for lease_id, expiry in self._consumed.items() if moment >= expiry]
        for lease_id in expired:
            del self._consumed[lease_id]
