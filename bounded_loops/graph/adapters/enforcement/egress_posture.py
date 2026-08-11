"""Egress posture — the deployment-level default for connector-node outbound network.

CONTEXT (why this module exists)
---------------------------------
`bl graph run --execute` today hardcodes every admitted `local_cli` connector node
(the user's own logged-in subscription agent CLI) to ``NetworkMode.OPEN`` — no OS
cage, full outbound network (`execute_graph.py::_build_policy`). That is the
correct default: a logged-in subscription CLI (`claude`, `codex`, `grok`, ...)
needs open egress to reach its own model and tools, and that is the 70-80% case.
The OS egress cage this project ships (`sandbox.py` / `egress_proxy.py` /
`providers/native.py`, real and proven live on macOS Seatbelt) is real, but is not
yet SELECTABLE as a per-deployment default — "Making ALLOWLIST their default tier
is the later-phase item" (README.md / RELEASE-READINESS.md).

This module is that later-phase item's CONFIG SURFACE: it resolves a first-class
``EgressPosture`` for a deployment and turns it into an honest, fail-closed
decision. It does NOT rewire `execute_graph.py` itself — that wiring (replacing the
hardcoded ``NetworkMode.OPEN`` in ``_build_policy`` with a call to
``decide_egress_posture``) is a deliberately separate, later change.

============================================================================
CONFIG CONTRACT (for the Slice 4 installer and any future wiring — read this,
not the implementation, to integrate)
============================================================================

Three postures, in increasing order of restriction:

* ``open``      (DEFAULT) — no cage. Outbound network is unrestricted. This is the
  explicit, documented default for a trusted-local subscription-CLI connector.
* ``allowlist`` (opt-in)  — the existing Seatbelt loopback-proxy cage is applied;
  outbound is admitted ONLY to the configured allowlist hosts. FAILS CLOSED: if
  this host cannot deliver the cage (no Seatbelt / no egress proxy), resolution
  REFUSES to run rather than silently downgrading to ``open``.
* ``broker``    (BYOK)    — outbound is not run as an open- or allowlist-network
  subprocess at all; the node's calls must be routed through the existing
  no-secret ``EgressBroker`` (the same mechanism the ``https`` connector
  transport already uses). Host capabilities are irrelevant to this posture.

Selection precedence (highest wins), resolved INDEPENDENTLY for the posture and
for the allowlist:

    1. explicit function argument   (``resolve_egress_posture(posture, ...)``)
    2. environment variable         (below)
    3. the egress config file       (below)
    4. default                      (``open`` / empty allowlist)

The allowlist is parsed/validated ONLY when it is actually relevant — the resolved posture
is ``allowlist``, or the caller explicitly passed allowlist hosts in this call (then any
posture/allowlist mismatch is a genuine caller contradiction, caught by
``EgressPostureConfig``'s own invariant). A stray or malformed ``BOUNDED_LOOPS_EGRESS_ALLOWLIST``
left over from a different posture is never even looked at, and so can never fail an
unrelated run (CRIT finding, fixed: an irrelevant, unused config value must never affect an
unrelated run's outcome — see ``test_malformed_allowlist_env_var_is_never_parsed_*`` in
``test_egress_posture.py``).

A tier that is genuinely ABSENT (unset env var, missing config file, no explicit
argument) falls through to the next tier. A tier that is PRESENT but INVALID (an
unrecognized posture string, a malformed allowlist entry, a config file that
exists but is not valid JSON / not an object / holds an unknown key / is a
symlink) RAISES ``GraphValidationError`` — it is never treated as absent, because
that would silently downgrade a misconfiguration toward the least-restrictive
(``open``) posture. An environment variable that is SET but EMPTY is treated as
absent (common shell pattern: ``export VAR=`` before it is conditionally filled).

Environment variables (follow the ``BOUNDED_LOOPS_*`` convention used throughout
this project — see ``trust_store.py`` / ``kill_switch.py`` / ``composition.py``;
NOTE this project's actual env-reading precedent lives there, not in
``adapters/_env.py``, which is the unrelated subprocess-env ALLOWLIST):

    BOUNDED_LOOPS_EGRESS_POSTURE    "open" | "allowlist" | "broker"
    BOUNDED_LOOPS_EGRESS_ALLOWLIST  comma-separated hosts, e.g.
                                     "api.anthropic.com,internal.example.com:8443"
                                     (a bare host defaults to port 443)
    BOUNDED_LOOPS_EGRESS_CONFIG     override the config file PATH (default below);
                                     mainly for test isolation, mirrors
                                     BOUNDED_LOOPS_TRUST_STORE.

Config file (installer-written; JSON; default path ``~/.bounded-loops/egress.json``,
override via ``BOUNDED_LOOPS_EGRESS_CONFIG``):

    {
      "posture": "allowlist",
      "allowlist": ["api.anthropic.com", "internal.example.com:8443"]
    }

  * Both keys are optional (each falls through independently per the precedence
    above); no OTHER key is accepted — an unknown key raises.
  * ``posture`` must be one of the three values above.
  * ``allowlist`` must be a JSON array of strings, each ``host`` or ``host:port``
    (a bare host defaults to port 443). Every entry must be an exact PUBLIC
    hostname — no IP literals, no wildcards (the same constraint
    ``NetworkDestination`` already enforces everywhere else in this engine).
  * A symlinked config file is refused (raise), never silently followed.

Consuming the resolved config:

    config = resolve_egress_posture(environ=os.environ)          # or inject explicit_posture=...
    decision = decide_egress_posture(config)                     # probes PlatformCapabilities
    # decision.requires_broker is True  -> route this node through EgressBroker /
    #                                       ConnectorInvoker (the https-transport path),
    #                                       do NOT build a sandbox network envelope.
    # otherwise                          -> decision.network_mode / .network_destinations
    #                                       plug directly into ExecutionEnvelope.

A short markdown mirror of this contract lives at ``docs/graph-egress-posture.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
from typing import Mapping, Sequence

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities, probe_platform
from bounded_loops.graph.application.egress_broker import split_destination
from bounded_loops.graph.application.execution_policy import NetworkDestination, NetworkMode
from bounded_loops.graph.domain.errors import GraphValidationError

_ENV_POSTURE = "BOUNDED_LOOPS_EGRESS_POSTURE"
_ENV_ALLOWLIST = "BOUNDED_LOOPS_EGRESS_ALLOWLIST"
_ENV_CONFIG_PATH = "BOUNDED_LOOPS_EGRESS_CONFIG"
_DEFAULT_CONFIG_PATH = Path.home() / ".bounded-loops" / "egress.json"
_DEFAULT_ALLOWLIST_PORT = 443
_CONFIG_FILE_KEYS = frozenset({"posture", "allowlist"})


class EgressPosture(str, Enum):
    """A deployment's default outbound-network policy for connector nodes."""

    OPEN = "open"
    ALLOWLIST = "allowlist"
    BROKER = "broker"


@dataclass(frozen=True)
class EgressPostureConfig:
    """The resolved, immutable egress-posture configuration for one process.

    Only ``ALLOWLIST`` may carry allowlist destinations — carrying one under
    ``OPEN``/``BROKER`` would be a silently-ignored value, which this project's
    fail-fast convention never allows (mirrors ``_validate_network``'s own
    "open network takes no destination allowlist" rule).
    """

    posture: EgressPosture
    allowlist: tuple[NetworkDestination, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.posture, EgressPosture):
            raise GraphValidationError("egress_posture", "/egress/posture", "posture must be an EgressPosture")
        object.__setattr__(self, "allowlist", tuple(self.allowlist))
        if len(set(self.allowlist)) != len(self.allowlist):
            raise GraphValidationError("egress_posture", "/egress/allowlist", "allowlist destinations must be unique")
        if self.posture is not EgressPosture.ALLOWLIST and self.allowlist:
            raise GraphValidationError(
                "egress_posture", "/egress/allowlist", "only allowlist posture may carry an allowlist",
            )

    def allowlist_admits(self, hostname: str, port: int) -> bool:
        """True iff exactly ``(hostname, port)`` is on the resolved ALLOWLIST.

        Always False outside ALLOWLIST posture: OPEN has no allowlist concept
        (egress is unrestricted, a different question entirely) and BROKER is
        authorized per-request by ``EgressBroker`` leases, not by a host list.
        An input that cannot even form a valid destination (an IP literal, a
        malformed host) fails CLOSED (returns False) rather than raising — an
        admission CHECK must never throw for merely-unexpected input.
        """
        if self.posture is not EgressPosture.ALLOWLIST:
            return False
        try:
            destination = NetworkDestination(hostname=hostname, port=port)
        except GraphValidationError:
            return False
        return destination in self.allowlist


@dataclass(frozen=True)
class EgressPostureDecision:
    """What one node worker must actually do, given the resolved posture and this
    host's real capabilities — an honest disclosure, mirroring ``EnforcedControls``
    elsewhere in the enforcement layer, never a bare enum value.

    ``requires_broker`` exists so a future integrator cannot mistake
    ``network_mode is None`` (which applies ONLY to BROKER) for "no restriction" and
    fall back to some default network mode — the field makes the "route this
    through the broker, do not build a sandbox network envelope" instruction
    impossible to silently miss.
    """

    posture: EgressPosture
    network_mode: NetworkMode | None
    network_destinations: tuple[NetworkDestination, ...]
    requires_broker: bool
    rationale: str


# ── posture / allowlist value parsing ───────────────────────────────────────────


def _posture_from_text(raw: str, *, source: str) -> EgressPosture:
    try:
        return EgressPosture(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(p.value for p in EgressPosture)
        raise GraphValidationError(
            "egress_posture", "/egress/posture",
            f"unrecognized egress posture {raw!r} from {source} (expected one of: {allowed})",
        ) from exc


def _parse_allowlist_entries(entries: Sequence[str], *, source: str) -> tuple[NetworkDestination, ...]:
    if isinstance(entries, str):
        raise GraphValidationError(
            "egress_posture", "/egress/allowlist",
            f"allowlist from {source} must be a list of hostnames, not a bare string "
            f"(iterating a str yields characters, not hosts) — did you mean [{entries!r}]?",
        )
    destinations: list[NetworkDestination] = []
    for raw in entries:
        text = raw.strip()
        if not text:
            continue
        try:
            host, port = split_destination(text)
        except ValueError as exc:
            raise GraphValidationError(
                "egress_posture", "/egress/allowlist",
                f"malformed allowlist entry {raw!r} from {source}: {exc}",
            ) from exc
        try:
            destinations.append(
                NetworkDestination(hostname=host, port=port if port is not None else _DEFAULT_ALLOWLIST_PORT),
            )
        except GraphValidationError as exc:
            raise GraphValidationError(
                "egress_posture", "/egress/allowlist",
                f"malformed allowlist entry {raw!r} from {source}: {exc.message}",
            ) from exc
    return tuple(destinations)


# ── config file (installer-written; fail-closed on any present-but-bad content) ─


def _config_path(env: Mapping[str, str]) -> Path:
    override = env.get(_ENV_CONFIG_PATH)
    return Path(override) if override else _DEFAULT_CONFIG_PATH


def _read_config_file(path: Path) -> Mapping[str, object] | None:
    """Return the parsed config object, or None iff the path does not exist.

    Anything else wrong with an EXISTING path (a symlink, an OS read error, invalid
    JSON, a non-object body, an unknown key) raises — never silently treated as
    "absent", which would let a corrupt or tampered installer-written file quietly
    downgrade resolution to a lower-precedence tier (fail-open).
    """
    if not path.exists():
        return None
    if path.is_symlink():
        raise GraphValidationError(
            "egress_posture", "/egress/config",
            f"egress config file {path} must not be a symlink (refusing to follow it)",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphValidationError(
            "egress_posture", "/egress/config", f"egress config file {path} could not be read: {exc}",
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GraphValidationError(
            "egress_posture", "/egress/config", f"egress config file {path} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise GraphValidationError(
            "egress_posture", "/egress/config", f"egress config file {path} must contain a JSON object",
        )
    unknown = frozenset(data) - _CONFIG_FILE_KEYS
    if unknown:
        raise GraphValidationError(
            "egress_posture", "/egress/config",
            f"egress config file {path} has unknown key(s) {sorted(unknown)!r} "
            f"(expected only {sorted(_CONFIG_FILE_KEYS)!r})",
        )
    return data


# ── precedence resolution ────────────────────────────────────────────────────────


def _resolve_posture(
    explicit: EgressPosture | None, env: Mapping[str, str], config_data: Mapping[str, object] | None,
) -> EgressPosture:
    if explicit is not None:
        if not isinstance(explicit, EgressPosture):
            raise GraphValidationError("egress_posture", "/egress/posture", "explicit posture must be an EgressPosture")
        return explicit
    raw_env = env.get(_ENV_POSTURE)
    if raw_env is not None and raw_env.strip():
        return _posture_from_text(raw_env, source=f"env {_ENV_POSTURE}")
    if config_data is not None and "posture" in config_data:
        raw_file = config_data["posture"]
        if not isinstance(raw_file, str):
            raise GraphValidationError(
                "egress_posture", "/egress/config", "config file 'posture' field must be a string",
            )
        return _posture_from_text(raw_file, source="config file")
    return EgressPosture.OPEN


def _resolve_allowlist(
    explicit: Sequence[str] | None, env: Mapping[str, str], config_data: Mapping[str, object] | None,
) -> tuple[NetworkDestination, ...]:
    if explicit is not None:
        return _parse_allowlist_entries(explicit, source="explicit argument")
    raw_env = env.get(_ENV_ALLOWLIST)
    if raw_env is not None and raw_env.strip():
        return _parse_allowlist_entries(raw_env.split(","), source=f"env {_ENV_ALLOWLIST}")
    if config_data is not None and "allowlist" in config_data:
        raw_file = config_data["allowlist"]
        if not isinstance(raw_file, list) or not all(isinstance(v, str) for v in raw_file):
            raise GraphValidationError(
                "egress_posture", "/egress/config", "config file 'allowlist' field must be a list of strings",
            )
        return _parse_allowlist_entries(raw_file, source="config file")
    return ()


def resolve_egress_posture(
    explicit_posture: EgressPosture | None = None,
    *,
    explicit_allowlist: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> EgressPostureConfig:
    """Resolve the egress posture for this process. See the module docstring for
    the full config contract (env vars, config file shape, precedence order).

    ``environ`` defaults to ``os.environ`` in production; tests inject a plain
    dict so resolution never touches the real environment or ``$HOME``.
    """
    env = environ if environ is not None else os.environ
    config_data = _read_config_file(_config_path(env))
    posture = _resolve_posture(explicit_posture, env, config_data)
    # Only parse/validate the allowlist when it is actually relevant: the resolved posture is
    # ALLOWLIST, or the CALLER explicitly passed allowlist hosts in THIS call (a genuine
    # caller-level contradiction if posture isn't ALLOWLIST too — caught below by
    # EgressPostureConfig's own invariant). Otherwise skip parsing entirely: a stray/leftover
    # BOUNDED_LOOPS_EGRESS_ALLOWLIST value (or a config-file "allowlist" section left over from
    # a different posture) is not relevant to the posture that actually won, and must not be
    # able to fail an unrelated run merely because it happens to be malformed (CRIT finding —
    # an irrelevant, unused config value must never affect an unrelated run's outcome).
    if posture is EgressPosture.ALLOWLIST or explicit_allowlist is not None:
        allowlist = _resolve_allowlist(explicit_allowlist, env, config_data)
    else:
        allowlist = ()
    return EgressPostureConfig(posture=posture, allowlist=allowlist)


# ── capability-aware decision ────────────────────────────────────────────────────


def decide_egress_posture(
    config: EgressPostureConfig, *, capabilities: PlatformCapabilities | None = None,
) -> EgressPostureDecision:
    """Turn a resolved posture into what one node worker must actually do here.

    Mirrors ``build_enforcer``'s "probe unless injected" convention: production
    callers get a real platform probe for free; tests always inject a fixed
    ``PlatformCapabilities``.

    FAIL CLOSED: ``ALLOWLIST`` without an available OS cage (Seatbelt AND the
    loopback egress proxy) raises ``GraphValidationError`` — it is NEVER silently
    downgraded to OPEN. ``BROKER`` never consults capabilities at all: it is not a
    sandbox-network posture (no subprocess is caged), so host capabilities are
    irrelevant to it, exactly as they are irrelevant to the existing `https`
    connector transport.
    """
    if not isinstance(config, EgressPostureConfig):
        raise GraphValidationError("egress_posture", "/egress", "decide_egress_posture requires an EgressPostureConfig")

    if config.posture is EgressPosture.OPEN:
        return EgressPostureDecision(
            posture=EgressPosture.OPEN,
            network_mode=NetworkMode.OPEN,
            network_destinations=(),
            requires_broker=False,
            rationale=(
                "OPEN egress posture: no cage applied; outbound network is unrestricted "
                "(the documented default for a trusted-local subscription-CLI connector)"
            ),
        )

    if config.posture is EgressPosture.BROKER:
        return EgressPostureDecision(
            posture=EgressPosture.BROKER,
            network_mode=None,
            network_destinations=(),
            requires_broker=True,
            rationale=(
                "BROKER egress posture: this node's outbound calls must be routed through the "
                "no-secret EgressBroker (single-use, time-bound, SSRF/DNS-rebind-denied leases), "
                "the same mechanism the https connector transport already uses — no sandbox "
                "network envelope applies"
            ),
        )

    # ALLOWLIST — fail closed unless this host can actually deliver the loopback-proxy
    # cage. Uses the SAME two capability flags the registry/native provider already gate
    # on (capabilities.py / providers/native.py), so this decision and the enforcement
    # layer's own selection can never disagree about what "available" means.
    caps = capabilities if capabilities is not None else probe_platform()
    if not (caps.seatbelt and caps.egress_proxy):
        raise GraphValidationError(
            "egress_posture", "/egress/posture",
            "ALLOWLIST egress posture was selected but this host cannot deliver the OS "
            "cage (Seatbelt + loopback egress proxy unavailable) — refusing to run rather "
            "than silently falling back to OPEN egress",
        )
    return EgressPostureDecision(
        posture=EgressPosture.ALLOWLIST,
        network_mode=NetworkMode.ALLOWLIST,
        network_destinations=config.allowlist,
        requires_broker=False,
        rationale=(
            "ALLOWLIST egress posture: Seatbelt loopback-proxy cage active, admitting "
            f"exactly {len(config.allowlist)} configured destination(s); every other "
            "destination is denied"
        ),
    )
