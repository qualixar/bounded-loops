"""Loopback destination-allowlisted CONNECT proxy — the egress chokepoint for RC-LOCKDOWN.

``NetworkMode.ALLOWLIST`` is enforced by two cooperating parts:

* the OS cage (Seatbelt allow-loopback-only / Linux net-namespace, built elsewhere) denies the
  node process ALL outbound sockets EXCEPT to this proxy on ``127.0.0.1:<port>``; and
* this proxy admits an HTTP ``CONNECT`` tunnel ONLY to an admitted :class:`NetworkDestination`
  (exact ``host:port``) that resolves to a public-unicast address, connects to the PINNED resolved
  IP (never re-resolving — so a DNS rebind between check and connect cannot redirect inward), and
  denies + logs everything else.

Together they cage a possibly-compromised sandboxed process to exactly the admitted destinations:
the process cannot route anywhere but the proxy, and the proxy is a fail-closed allowlist.

Fail-closed and DoS-resistant by construction:
* binds the loopback (``127.0.0.1`` AND ``::1``) ONLY, on one port — never a routable interface, and
  BOTH loopback families the Seatbelt ``localhost`` token admits ARE this proxy, so a co-resident
  process cannot listen on ``[::1]:<port>`` and catch a compromised child's egress;
* only the ``CONNECT`` method (with an ``HTTP/`` version) is honored; any other request is refused;
* an unknown destination, an IP-literal / malformed host, a non-public / mixed resolution, a resolver
  error, an oversized/malformed request head, or any unexpected error → refused (and logged), never
  a tunnel; every decision is handed to the injected log sink;
* a short header deadline defeats slow-loris; a bounded connection semaphore caps concurrency (503
  when saturated) so a flood cannot exhaust threads and starve a legitimate tunnel;
* ``start()``/``stop()`` are lock-guarded and the proxy is safely restartable.

The public-unicast test and destination parser are REUSED from the egress broker so the OS cage and
the app-layer credential broker admit destinations under one policy, not two that can drift.
"""

from __future__ import annotations

import errno
import selectors
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

from bounded_loops.graph.application.egress_broker import (
    NameResolver,
    SystemResolver,
    is_public_unicast,
    split_destination,
)
from bounded_loops.graph.application.execution_policy import NetworkDestination

_CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\n\r\n"
_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
_UNAVAILABLE = b"HTTP/1.1 503 Service Unavailable\r\n\r\n"

_MAX_REQUEST_HEAD = 8192  # a CONNECT request head longer than this is refused
_BUF = 65536


class EgressLogSink(Protocol):
    def __call__(self, *, allowed: bool, destination: str, reason: str) -> None: ...


def _noop_log(*, allowed: bool, destination: str, reason: str) -> None:  # noqa: ARG001
    return None


@dataclass
class LoopbackEgressProxy:
    """A single-tenant, loopback-only, destination-allowlisted HTTP ``CONNECT`` proxy.

    Parameters
    ----------
    allowed:
        The exact destinations a tunnel may target. A CONNECT to any ``host:port`` not in this set
        is refused. Hostnames are compared case-insensitively (``NetworkDestination`` lower-cases).
    resolver:
        Name resolver; the proxy pins what this returns and connects only to those IPs.
    pin_policy:
        Per-IP admission test (default: the broker's public-unicast SSRF guard). Injected in
        hermetic tests so a loopback stand-in destination can be exercised without opening real egress.
    header_timeout:
        Deadline for a client to send the full ``CONNECT`` request head — short, to defeat slow-loris.
    connect_timeout / idle_timeout:
        Upstream connect deadline and per-tunnel idle deadline (seconds).
    max_connections:
        Bounded concurrency; a connection beyond the cap is answered ``503`` and closed, so a flood
        cannot exhaust threads and starve a legitimate tunnel.
    log:
        Sink for every allow/deny decision.
    """

    allowed: tuple[NetworkDestination, ...]
    resolver: NameResolver = field(default_factory=SystemResolver)
    pin_policy: Callable[[str], bool] = is_public_unicast
    header_timeout: float = 5.0
    connect_timeout: float = 10.0
    idle_timeout: float = 300.0
    max_connections: int = 64
    log: EgressLogSink = _noop_log

    _servers: list[socket.socket] = field(default_factory=list, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _sem: threading.BoundedSemaphore | None = field(default=None, init=False, repr=False)
    _port: int = field(default=0, init=False, repr=False)
    _live: set[socket.socket] = field(default_factory=set, init=False, repr=False)
    _live_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # TEST-04 fix: wakeup socket pair lets stop() interrupt _serve()'s
    # selector.select() immediately instead of waiting up to 0.5s for the
    # select timeout.  stop() writes one byte to _wakeup_w; _serve() detects
    # the event on _wakeup_r and exits promptly.  stop() then joins the thread
    # BEFORE closing server sockets, eliminating the FD-reuse race that caused
    # "ValueError: Invalid file descriptor: -1" in _serve's selector.register().
    _wakeup_w: socket.socket | None = field(default=None, init=False, repr=False)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> int:
        """Bind the loopback (``127.0.0.1`` AND ``::1``) on one ephemeral port, start accepting, return it."""
        with self._lock:
            if self._servers:
                raise RuntimeError("proxy already started")
            self._stop = threading.Event()  # fresh, so a restart is not dead-on-arrival
            self._sem = threading.BoundedSemaphore(self.max_connections)
            # Create the wakeup socket pair before binding the server sockets.
            # stop() writes one byte to _wakeup_w; _serve() detects the event
            # on wakeup_r (passed as an argument) and exits the select loop
            # immediately rather than waiting for the 0.5s select timeout.
            wakeup_r, wakeup_w = socket.socketpair()
            # CRIT-3 guard: if anything between socketpair() and thread.start()
            # raises, close BOTH ends of the wakeup pair and leave the proxy in a
            # clean "never started" state so stop() remains a no-op and start()
            # can be retried.  self._wakeup_w is stored only after a successful
            # bind, so a partially-initialised proxy never exposes a dangling fd.
            servers: list[socket.socket] = []
            try:
                servers, port = self._bind_dual_stack()
                self._servers = servers
                self._port = port
                self._wakeup_w = wakeup_w  # stored only after successful bind
                self._thread = threading.Thread(
                    target=self._serve,
                    args=(list(servers), self._stop, self._sem, wakeup_r),
                    name="egress-proxy",
                    daemon=True,
                )
                self._thread.start()
            except BaseException:
                _close(wakeup_r)
                _close(wakeup_w)
                for server in servers:
                    _close(server)
                self._servers = []
                self._port = 0
                self._wakeup_w = None
                self._thread = None
                raise
            return self._port

    def _bind_dual_stack(self) -> tuple[list[socket.socket], int]:
        """Bind ``127.0.0.1`` AND ``::1`` on the SAME ephemeral port.

        The Seatbelt cage admits ``localhost:<port>``, which on macOS resolves to BOTH loopback
        families — so if this proxy bound only IPv4, a co-resident colluder could listen on
        ``[::1]:<port>`` and catch a compromised child's egress, bypassing the allowlist under a
        claimed ``egress=ENFORCED`` (dual-audit MAJOR-1). Binding both families ourselves closes that
        hole: every loopback address the cage permits is this proxy. If the host has no usable IPv6
        (``::1`` unbindable by anyone), IPv4-only is safe and we proceed.

        CON-06 — TOCTOU window and residual risk
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        There is an inherent race between the v4 bind (which allocates the ephemeral port) and the
        subsequent v6 bind on the same port.  In that window — roughly one ``getsockname()`` syscall
        wide — a racing adversary could bind ``[::1]:<port>`` and intercept the IPv6 path.

        To minimise the window the v6 socket is created and configured (``socket()``,
        ``setsockopt()`` × 2) BEFORE the v4 bind, so the only operation between
        ``v4.getsockname()`` and ``v6.bind()`` is the bind syscall itself.  This reduces the
        TOCTOU window to the minimum achievable without OS-level atomic dual-bind support (which
        POSIX does not provide).

        If the v6 bind fails with EADDRINUSE the loop retries with a fresh ephemeral port (up to
        eight attempts).  Winning all eight races simultaneously requires a dedicated local adversary
        and is astronomically unlikely; the alternative (SO_REUSEPORT) would introduce its own
        sharing complexity on the v4 side.  The residual risk is accepted and documented here.
        """
        last: OSError | None = None
        for _ in range(8):
            # Pre-create and configure v6 socket BEFORE the v4 bind so the
            # TOCTOU window between getsockname() and v6.bind() is minimised
            # to a single bind syscall (CON-06 hardening).
            v6: socket.socket | None = None
            if socket.has_ipv6:
                try:
                    v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except OSError:
                    if v6 is not None:
                        v6.close()
                    v6 = None
            v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                v4.bind(("127.0.0.1", 0))  # loopback ONLY — never a routable interface
            except OSError as exc:
                v4.close()
                if v6 is not None:
                    v6.close()
                last = exc
                continue
            port = v4.getsockname()[1]
            # TOCTOU window: minimal — only v6.bind() between here and end of window.
            if v6 is not None:
                try:
                    v6.bind(("::1", port))
                except OSError as exc:
                    v6.close()
                    v6 = None
                    if exc.errno == errno.EADDRINUSE:
                        # Free on v4 but taken on v6 — retry a fresh port so BOTH families are ours.
                        v4.close()
                        last = exc
                        continue
                    # ::1 is otherwise unavailable (IPv6 disabled) → nobody can bind it → v4-only is safe.
            servers = [v4] + ([v6] if v6 is not None else [])
            for sock in servers:
                sock.listen(64)
                sock.settimeout(0.5)  # so the accept loop can observe the stop event
            return servers, port
        raise OSError(f"loopback egress proxy could not bind a common v4/v6 port: {last}")

    @property
    def port(self) -> int:
        return self._port

    def stop(self) -> None:
        """Stop the proxy, cleanly retiring the _serve thread before closing sockets.

        TEST-04 fix — ordering matters:

        OLD order: close server sockets → drain live → join thread
          Race: if the new _serve thread hadn't yet called selector.register(server, ...)
          when stop() closed the sockets, fileno() returns -1 and selector.register()
          raises ValueError in the background thread.

        NEW order: signal wakeup → drain live → join thread → close server sockets
          The wakeup socket wakes _serve() from selector.select() immediately so
          the thread exits within microseconds.  Only after join() returns (thread
          fully exited, selector.close() already called) do we close the server
          sockets — at that point no thread can ever see those FDs again.
        """
        with self._lock:
            self._stop.set()
            thread, servers = self._thread, list(self._servers)
            self._thread = None
            self._servers = []
            self._port = 0
            wakeup_w, self._wakeup_w = self._wakeup_w, None
        # Signal _serve() to wake up immediately from selector.select().
        # Writing one byte to the write end triggers a readable event on the
        # read end (registered in _serve's selector), causing prompt exit.
        if wakeup_w is not None:
            try:
                wakeup_w.send(b"\x00")
            except OSError:
                pass
            _close(wakeup_w)
        # Drain in-flight tunnels: closing the live sockets interrupts their blocking recv so the
        # handler threads finish promptly instead of lingering until idle_timeout (dual-audit m2).
        with self._live_lock:
            live = list(self._live)
            self._live.clear()
        for sock in live:
            _close(sock)
        # Join BEFORE closing server sockets.  _serve() exits as soon as it
        # observes the wakeup event (effectively immediate).  Closing sockets
        # after the join guarantees the thread has fully exited (including its
        # finally: selector.close()) before we invalidate the FDs it was using.
        if thread is not None:
            thread.join(timeout=5.0)
        for server in servers:
            try:
                server.close()
            except OSError:
                pass

    def __enter__(self) -> LoopbackEgressProxy:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ── accept loop ──────────────────────────────────────────────────────────

    def _serve(
        self,
        servers: list[socket.socket],
        stop: threading.Event,
        sem: threading.BoundedSemaphore,
        wakeup_r: socket.socket,
    ) -> None:
        """Accept loop.  Exits when ``stop`` is set OR a wakeup byte arrives on ``wakeup_r``.

        ``wakeup_r`` is the read end of the socketpair created in ``start()``.  Registering
        it in the selector lets ``stop()`` interrupt a blocking ``select()`` immediately by
        writing one byte to the write end — no waiting for the 0.5-second timeout.  The
        thread exits and calls ``selector.close()`` BEFORE ``stop()`` closes the server
        sockets, so ``selector.register()`` always sees live FDs (TEST-04 fix).
        """
        selector = selectors.DefaultSelector()
        try:
            selector.register(wakeup_r, selectors.EVENT_READ)
            for server in servers:
                selector.register(server, selectors.EVENT_READ)
            while not stop.is_set():
                for key, _mask in selector.select(timeout=0.5):
                    if key.fileobj is wakeup_r:
                        return  # stop() signalled — exit promptly
                    server = key.fileobj  # type: ignore[assignment]
                    try:
                        client, _addr = server.accept()
                    except (socket.timeout, OSError):
                        continue
                    if not sem.acquire(blocking=False):  # at capacity — refuse fast, never starve silently
                        self.log(allowed=False, destination="", reason="egress proxy at capacity")
                        _respond(client, _UNAVAILABLE)
                        _close(client)
                        continue
                    threading.Thread(
                        target=self._handle, args=(client, sem), name="egress-proxy-conn", daemon=True,
                    ).start()
        finally:
            _close(wakeup_r)
            selector.close()

    # ── per-connection ─────────────────────────────────────────────────────────

    def _handle(self, client: socket.socket, sem: threading.BoundedSemaphore) -> None:
        upstream: socket.socket | None = None
        self._track(client)
        try:
            client.settimeout(self.header_timeout)  # short deadline for the request head (anti slow-loris)
            parsed = self._read_connect_target(client)
            if parsed is None:
                _respond(client, _BAD_REQUEST)
                self.log(allowed=False, destination="", reason="not a well-formed CONNECT request")
                return
            host, port, prelude = parsed
            decision = self._authorize(host, port)
            if decision.pinned_ip is None:
                _respond(client, _FORBIDDEN)
                self.log(allowed=False, destination=f"{host}:{port}", reason=decision.reason)
                return
            try:
                upstream = socket.create_connection((decision.pinned_ip, port), timeout=self.connect_timeout)
            except OSError as exc:
                _respond(client, _BAD_GATEWAY)
                self.log(allowed=False, destination=f"{host}:{port}", reason=f"upstream connect failed: {exc}")
                return
            self._track(upstream)
            client.settimeout(self.idle_timeout)
            upstream.settimeout(self.idle_timeout)
            _respond(client, _CONNECT_ESTABLISHED)
            # Forward any bytes the client PIPELINED after the CONNECT head (e.g. the TLS ClientHello
            # sent in the same segment) — dropping them silently stalls real HTTPS clients (dual-audit M3).
            if prelude:
                upstream.sendall(prelude)
            self.log(allowed=True, destination=f"{host}:{port}", reason=f"tunnel → {decision.pinned_ip}:{port}")
            self._tunnel(client, upstream)
        except OSError:
            return
        finally:
            self._untrack(client)
            self._untrack(upstream)
            _close(upstream)
            _close(client)
            sem.release()

    def _read_connect_target(self, client: socket.socket) -> tuple[str, int, bytes] | None:
        """Read the request head; return (host, port, prelude) iff it is a valid ``CONNECT host:port``.

        ``prelude`` is any bytes the client already sent AFTER the blank line (a pipelined ClientHello);
        the caller forwards them to upstream so no pipelined data is lost."""
        head = bytearray()
        while True:
            try:
                chunk = client.recv(_BUF)
            except OSError:
                return None
            if not chunk:
                return None
            head.extend(chunk)
            if len(head) > _MAX_REQUEST_HEAD:  # checked AFTER append — the cap is a true ceiling
                return None
            if b"\r\n\r\n" in head:
                break
        raw = bytes(head)
        terminator = raw.find(b"\r\n\r\n")
        prelude = raw[terminator + 4:]
        try:
            request_line = raw[:terminator].split(b"\r\n", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            return None
        parts = request_line.split(" ")
        if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].upper().startswith("HTTP/"):
            return None
        try:
            host, port = split_destination(parts[1])
        except ValueError:
            return None
        if port is None:  # CONNECT requires an explicit authority host:port
            return None
        return host.lower(), port, prelude

    def _track(self, sock: socket.socket) -> None:
        with self._live_lock:
            self._live.add(sock)

    def _untrack(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        with self._live_lock:
            self._live.discard(sock)

    def _authorize(self, host: str, port: int) -> _Pinned:
        # An IP-literal or otherwise non-hostname target makes NetworkDestination raise; treat it as a
        # clean deny (with a 403 + log), never an unhandled crash that drops the client silently.
        try:
            destination = NetworkDestination(hostname=host, port=port)
        except Exception:  # noqa: BLE001 — a non-admissible host is simply not on the allowlist
            return _Pinned(None, "destination is not an admissible hostname")
        if destination not in self.allowed:
            return _Pinned(None, "destination is not on the admitted allowlist")
        try:
            resolved = self.resolver.resolve(host, port)
        except Exception:  # noqa: BLE001 — any resolver failure denies (fail-closed)
            return _Pinned(None, "destination host could not be resolved")
        if not resolved:
            return _Pinned(None, "destination host did not resolve to any address")
        if not all(self.pin_policy(ip) for ip in resolved):
            return _Pinned(None, "destination resolves to a non-public address (SSRF denied)")
        return _Pinned(resolved[0], "")

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        deadline = self.idle_timeout + 5.0
        a = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
        b = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
        a.start()
        b.start()
        # Bounded join: on a fully-idle tunnel the pumps end at idle_timeout; the grace lets that
        # propagate, then the caller's finally closes both sockets to interrupt anything lingering.
        a.join(deadline)
        b.join(deadline)


@dataclass(frozen=True)
class _Pinned:
    pinned_ip: str | None
    reason: str


def _respond(sock: socket.socket, payload: bytes) -> None:
    try:
        sock.sendall(payload)
    except OSError:
        pass


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes src→dst until EOF/error, then half-close dst so the reverse pump also ends."""
    try:
        while True:
            data = src.recv(_BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _close(sock: socket.socket | None) -> None:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass
