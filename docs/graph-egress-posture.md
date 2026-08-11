# Egress posture — config contract (Slice 2)

Config surface for the connector-node outbound-network default. Implementation:
`bounded_loops/graph/adapters/enforcement/egress_posture.py` (the module docstring
there is the authoritative source — this file is a skimmable mirror of it for the
Slice 4 installer author and for wiring `_build_policy` in `execute_graph.py`).

## What this is, and what it is not

`bl graph run --execute` today hardcodes every admitted `local_cli` connector node
to `NetworkMode.OPEN` (no cage, full outbound) — see README.md /
RELEASE-READINESS.md: *"local_cli stays network-OPEN ... Making ALLOWLIST their
default tier is the later-phase item."*

This module resolves a configurable **egress posture** and turns it into an
honest, fail-closed decision. **It does not rewire `execute_graph.py` itself** —
that (replacing the hardcoded `NetworkMode.OPEN` with a call to
`decide_egress_posture`) is a separate, later change.

## The three postures

| Posture | Value | Behavior | Capability requirement |
|---|---|---|---|
| Open (**default**) | `open` | No cage. Outbound unrestricted. The correct default for a trusted-local, logged-in subscription CLI (`claude`, `codex`, `grok`, ...) — the 70–80% case. | None |
| Allowlist (opt-in) | `allowlist` | The existing Seatbelt loopback-proxy cage (`sandbox.py` / `egress_proxy.py` / `providers/native.py`) is applied; outbound admitted ONLY to configured hosts. | Seatbelt **and** the loopback egress proxy. **Fails closed** (refuses to run) if either is unavailable — never silently downgrades to `open`. |
| Broker (BYOK) | `broker` | Not a sandboxed subprocess at all — calls must route through the existing no-secret `EgressBroker` (the same mechanism the `https` connector transport already uses). | None — host capabilities are irrelevant to this posture. |

## Selection precedence

Resolved **independently** for the posture and for the allowlist. Highest wins;
an absent tier falls through, a present-but-invalid value at any tier raises
(never silently falls through):

```
1. explicit function argument   resolve_egress_posture(EgressPosture.X, ...)
2. environment variable         (below)
3. egress config file           (below)
4. default                      open / empty allowlist
```

An env var that is **set but empty** (`export BOUNDED_LOOPS_EGRESS_POSTURE=`) is
treated as absent, not as an invalid value.

## Environment variables

| Variable | Format | Notes |
|---|---|---|
| `BOUNDED_LOOPS_EGRESS_POSTURE` | `open` \| `allowlist` \| `broker` | |
| `BOUNDED_LOOPS_EGRESS_ALLOWLIST` | comma-separated hosts, e.g. `api.anthropic.com,internal.example.com:8443` | A bare host defaults to port `443`. Blank entries (trailing commas) are skipped. |
| `BOUNDED_LOOPS_EGRESS_CONFIG` | absolute path | Overrides the config file **path** (default below). Mirrors `BOUNDED_LOOPS_TRUST_STORE`; mainly for test isolation / custom install locations. |

Naming follows this project's real env-reading precedent (`trust_store.py` /
`kill_switch.py` / `composition.py` — `BOUNDED_LOOPS_<NAME>`, read fresh via
`os.environ.get` on every call). `adapters/_env.py` is unrelated — it is the
subprocess env ALLOWLIST, not an app-config-reading convention.

## Config file (installer-written)

Default path: `~/.bounded-loops/egress.json` (override via
`BOUNDED_LOOPS_EGRESS_CONFIG`).

```json
{
  "posture": "allowlist",
  "allowlist": ["api.anthropic.com", "internal.example.com:8443"]
}
```

- Both keys are optional; no other key is accepted (an unknown key raises).
- `posture` must be one of the three values above.
- `allowlist` must be a JSON array of strings, each `host` or `host:port`. Every
  entry must be an exact **public hostname** — no IP literals, no wildcards
  (the same constraint `NetworkDestination` enforces everywhere else in this
  engine).
- A symlinked config file is refused, never silently followed.
- A file that exists but is unreadable, not valid JSON, not a JSON object, or
  has an unknown key **raises** — it is never treated as "absent." A corrupt or
  tampered installer-written file must never silently downgrade resolution to a
  less-restrictive tier.

## Consuming the resolved config (for the future `execute_graph.py` wiring)

```python
from bounded_loops.graph.adapters.enforcement import (
    decide_egress_posture, resolve_egress_posture,
)

config = resolve_egress_posture(environ=os.environ)   # or explicit_posture=... for an override
decision = decide_egress_posture(config)               # probes PlatformCapabilities

if decision.requires_broker:
    ...  # route through EgressBroker / ConnectorInvoker (the https-transport path);
         # do NOT build a sandbox network envelope for this node
else:
    envelope = ExecutionEnvelope(
        ...,
        network_mode=decision.network_mode,             # NetworkMode.OPEN | .ALLOWLIST
        network_destinations=decision.network_destinations,
    )
```

`decision.requires_broker` exists precisely so a caller cannot mistake
`network_mode is None` (true only for `broker`) for "no restriction."

## Host admission check

`EgressPostureConfig.allowlist_admits(hostname, port) -> bool` is an exact
`(hostname, port)` membership test against the resolved allowlist — meaningful
only under `allowlist` posture (always `False` for `open`/`broker`, and `False`
rather than a raise for input that cannot even form a valid destination, e.g. an
IP literal).

## Tests

`tests/graph/adapters/enforcement/test_egress_posture.py` — 45 tests, 100% line
coverage of `egress_posture.py`.
