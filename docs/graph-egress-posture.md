# Egress posture — config contract and wiring (Slice 2)

Config surface for the connector-node outbound-network default, AND (as of this revision)
its actual wiring into `bl graph run --execute`. Implementation:
`bounded_loops/graph/adapters/enforcement/egress_posture.py` (posture resolution + the
generic, transport-agnostic capability decision — module docstring there is authoritative)
and `bounded_loops/graph/application/egress_posture_policy.py` (where that generic decision
meets a `local_cli` connector node's actual runtime capability — see "What is wired today"
below). This file is a skimmable mirror of both for the Slice 4 installer author.

## What this is, and what it is not

`bl graph run --execute` previously hardcoded every admitted `local_cli` connector node to
`NetworkMode.OPEN` unconditionally (no cage, full outbound) — see README.md /
RELEASE-READINESS.md: *"local_cli stays network-OPEN ... Making ALLOWLIST their default
tier is the later-phase item."* `execute_graph.py::_build_policy` now calls
`resolve_local_cli_egress_decision` (via `build_execution_controller`) instead of
hardcoding `NetworkMode.OPEN` directly — the posture is real, wired, and tested end-to-end.

## What is wired today: local_cli honors ONLY the `open` posture

Reading `local_cli_worker.py` in full (before wiring anything) surfaced a hard architectural
constraint: `LocalCliConnectorWorker` runs the CLI **unwrapped** — no Seatbelt profile, no
loopback egress proxy — and it inherits the operator's REAL `HOME`/`TMPDIR` *by design*, so
the CLI finds its own subscription-login config. It also carries its own defense-in-depth
guard that refuses any envelope but `NetworkMode.OPEN`. So:

* **`open` (default)** — wired, unchanged, byte-for-byte today's behavior.
* **`allowlist`** — **refused for any plan containing a `local_cli` node**, UNCONDITIONALLY,
  before host capabilities are even consulted. This is deliberate: checking Seatbelt/
  egress-proxy availability first would wrongly imply "get a Mac with Seatbelt and this
  works" — it would not, because `LocalCliConnectorWorker` has no cage-wrapping integration
  on ANY host. Making this actually work would mean adding real Seatbelt + egress-proxy
  support to that worker — a substantial, separate feature in real tension with its
  "inherit the real HOME so the CLI finds its own credentials" design, not something this
  wiring invents unasked.
* **`broker`** — **refused for any plan containing a `local_cli` node.** Confirmed against
  `egress_broker.py` and the connector transports: a `local_cli` node's subscription CLI
  authenticates out-of-band and talks to its own vendor over its own TLS; the no-secret
  `EgressBroker` (a single-use lease bound to one declared destination/method/effect) has
  nothing to mediate. There is no coherent way to route an entire subprocess's arbitrary
  outbound calls through a broker built for one authorized HTTP request at a time.

Both refusals fire at **preflight** (`build_execution_controller`, before any store/worker is
built, before `controller.run()`) as a `GraphValidationError` — every existing caller
(`execute_graph_run`, `LocalGraphRuntimeFacade.resume`/`.approve`) already wraps that call in
`except GraphValidationError`, so it always surfaces as a clean, actionable refusal — never a
mid-run traceback, never a silent downgrade to `open`.

**A plan with no `local_cli` node is completely unaffected** — including the ALLOWLIST
host-capability check, which is *skipped entirely* (not merely "not triggered") so an
https-only run's success never depends on a fact (Seatbelt availability) that has nothing to
do with how `https` actually works. `https` keeps its own independent, credential-broker-
mediated, per-node `ALLOWLIST` construction, untouched by any of this.

**Known gap (flagged, not fixed in this wiring pass):** `resolve_egress_posture()` parses
`BOUNDED_LOOPS_EGRESS_ALLOWLIST` whenever it is *set*, independent of the resolved posture. A
malformed allowlist value left over from a different posture (or a typo) will raise even for
a plan with no `local_cli` node and even when posture isn't `allowlist` — a narrower version
of the same "irrelevant config affecting an unrelated run" class of issue as the capability
leak above, but in the already-reviewed resolution module rather than this wiring. Not
changed here without an explicit decision, since it touches already-approved precedence
logic outside this pass's granted scope.

## The three postures (generic decision — `egress_posture.py`)

| Posture | Value | Generic decision | Wired behavior for `local_cli` |
|---|---|---|---|
| Open (**default**) | `open` | No cage. Outbound unrestricted. | Wired: today's unchanged behavior. |
| Allowlist (opt-in) | `allowlist` | The Seatbelt loopback-proxy cage (`sandbox.py` / `egress_proxy.py` / `providers/native.py`); fails closed without Seatbelt + the egress proxy. | **Refused for `local_cli`, unconditionally** — see above. Available to any FUTURE consumer whose worker can actually apply the cage. |
| Broker (BYOK) | `broker` | Route through the existing no-secret `EgressBroker` (the `https` transport's own mechanism). | **Refused for `local_cli`** — architecturally incoherent, see above. |

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

## Consuming the resolved config

`execute_graph.py::build_execution_controller` (used by both `execute_graph_run` and
`LocalGraphRuntimeFacade.resume`/`.approve`) calls this once, before any store/worker is built:

```python
from bounded_loops.graph.application.egress_posture_policy import resolve_local_cli_egress_decision

# Raises GraphValidationError for a local_cli plan under allowlist/broker (see above);
# for anything else, returns the generic decision (always OPEN for a local_cli plan today).
local_cli_decision = resolve_local_cli_egress_decision(plan, environ=environ, capabilities=caps)
```

`_build_policy` then uses `local_cli_decision.network_mode` for the `local_cli` branch
(always `NetworkMode.OPEN` today) with a defensive backstop raise if it is ever anything
else — unreachable via `build_execution_controller`'s own preflight refusal, but a real
guard for the `LocalGraphRuntimeFacade` path too, since it calls `build_execution_controller`
directly without a separate preflight step.

A future consumer whose worker CAN apply the cage (not `LocalCliConnectorWorker`) would use
`decide_egress_posture` directly, as originally documented:

```python
from bounded_loops.graph.adapters.enforcement import decide_egress_posture, resolve_egress_posture

config = resolve_egress_posture(environ=os.environ)
decision = decide_egress_posture(config)  # probes PlatformCapabilities
if decision.requires_broker:
    ...  # route through EgressBroker / ConnectorInvoker
else:
    envelope = ExecutionEnvelope(..., network_mode=decision.network_mode,
                                  network_destinations=decision.network_destinations)
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

- `tests/graph/adapters/enforcement/test_egress_posture.py` — 45 tests, 100% line coverage
  of `egress_posture.py` (resolution + the generic capability decision).
- `tests/graph/application/test_egress_posture_policy.py` — 11 unit tests for
  `resolve_local_cli_egress_decision` (the local_cli-specific refusal rules above).
- `tests/graph/application/test_execute_graph_egress_posture.py` — 10 end-to-end tests
  driving a real plan through `execute_graph_run`: default backward compat, ALLOWLIST/BROKER
  preflight refusal (with and without a cage-capable host, converging on the same message),
  and a misconfigured posture value failing closed cleanly.
- `tests/graph/application/test_execute_graph_byok.py` — 2 tests added proving an `https`
  node succeeds identically under `allowlist` (with NO cage on the host) and `broker`
  posture — the case that caught the capability-leak gap during development.
