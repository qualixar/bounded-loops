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

## What is wired today: local_cli honors `open` (default) and `allowlist` (real cage)

Reading `local_cli_worker.py` in full (before wiring anything) surfaced a hard architectural
tension: `LocalCliConnectorWorker` runs the CLI with the operator's REAL `HOME`/`TMPDIR` *by
design*, so the CLI finds its own subscription-login config — in direct tension with a naive
cage's isolated-`HOME` requirement. **DECISION (Varun): build the real cage rather than refuse
it.** `LocalCliConnectorWorker` now reuses the SAME Seatbelt SBPL builder + loopback egress
proxy `SandboxedNodeWorker`/the `https` transport already use, resolving the tension by
confining writes to the workdir **plus** the real `HOME`/`TMPDIR` (never an isolated empty
one) while confining network to the loopback proxy. See "The caged path" below for the design.

* **`open` (default)** — wired, unchanged, byte-for-byte today's behavior.
* **`allowlist`** — **honored**: the local_cli node runs REALLY CAGED (live-verified on macOS
  Seatbelt — see Tests below). Fails closed at preflight if this host cannot deliver the cage
  (no Seatbelt / no egress proxy) — the refusal names `open` as the danger it refuses to fall
  back to, never a silent downgrade.
* **`broker`** — **still refused for any plan containing a `local_cli` node.** Confirmed
  against `egress_broker.py` and the connector transports: a `local_cli` node's subscription
  CLI authenticates out-of-band and talks to its own vendor over its own TLS; the no-secret
  `EgressBroker` (a single-use lease bound to one declared destination/method/effect) has
  nothing to mediate. This is architecturally incoherent, not a missing feature, and did not
  change with the ALLOWLIST decision.

Both the BROKER refusal and the ALLOWLIST-without-a-cage refusal fire at **preflight**
(`build_execution_controller`, before any store/worker is built, before `controller.run()`) as
a `GraphValidationError` — every existing caller (`execute_graph_run`,
`LocalGraphRuntimeFacade.resume`/`.approve`) already wraps that call in
`except GraphValidationError`, so it always surfaces as a clean, actionable refusal — never a
mid-run traceback, never a silent downgrade to `open`.

**A plan with no `local_cli` node is completely unaffected** — including the ALLOWLIST
host-capability check, which is *skipped entirely* (not merely "not triggered") so an
https-only run's success never depends on a fact (Seatbelt availability) that has nothing to
do with how `https` actually works. `https` keeps its own independent, credential-broker-
mediated, per-node `ALLOWLIST` construction, untouched by any of this. Verified live: a
mismatch here was caught DURING development (an earlier version of this wiring made an
https-only run's success depend on Seatbelt availability — fixed before landing).

**Manifest requirement:** `validate_execution_envelope`'s network-effect floor
(`_NETWORK_EFFECTS`) applies to `local_cli` exactly as it already does to `https` — a
`local_cli` node must declare `external_write` (or `financial`/`irreversible`) to be
envelope-eligible for `allowlist`. A node declaring only `workspace_write` stays `open`-only
(compiler-enforced, not this wiring's choice).

**Known gap (flagged, not fixed in this pass):** `resolve_egress_posture()` used to parse
`BOUNDED_LOOPS_EGRESS_ALLOWLIST` whenever it was *set*, independent of the resolved posture —
**this has been fixed** (it is now parsed only when the resolved posture is `allowlist`, or
the caller explicitly passed allowlist hosts). See the CHANGELOG-equivalent in the module
docstring and the regression tests in `test_egress_posture.py`.

## The caged path (`local_cli_worker.py`)

* **Reuse, not reinvention:** `build_seatbelt_allowlist_profile` and `seatbelt_argv`
  (`sandbox.py`) plus `LoopbackEgressProxy` (`egress_proxy.py`) — the exact same functions
  `SandboxedNodeWorker` calls. No new cage mechanism was written.
* **Filesystem:** writable = `(workdir, HOME, TMPDIR)` where `HOME`/`TMPDIR` are the
  operator's REAL values (resolved from the env dict, falling back to `Path.home()` /
  `tempfile.gettempdir()` if absent, and then explicitly written back into that env dict so
  the child's actual environment always agrees with what the cage allows). Reads are never
  confined by this profile (true for every other caller of it too) — this is a NET NEW
  network restriction on top of unchanged (broad) filesystem trust for an already-trusted
  local tool, not a filesystem-confinement regression from today's fully-open `open` mode.
* **Network:** the SAME 6 proxy env vars `SandboxedNodeWorker` sets
  (`HTTPS_PROXY`/`https_proxy`/`HTTP_PROXY`/`http_proxy`/`ALL_PROXY`/`all_proxy` →
  `http://127.0.0.1:<port>`) point a cooperating HTTP client at the right place; the Seatbelt
  cage (`(deny network*)` + one loopback carve-out) is the actual enforcement — a
  non-cooperating or malicious client cannot bypass it by ignoring or overriding those vars.
* **Fail-closed ordering:** the proxy is started, and the `proxy` variable assigned, BEFORE
  the (fallible) Seatbelt-profile build — so a profile-build failure still stops the proxy in
  `finally`, never a leaked listener.
* **Diagnostics:** a caged CLI blocked from a non-allowlisted host and exiting non-zero hits
  the EXISTING non-zero-exit handling verbatim — a bounded, secret-redacted diagnostic, never
  a silent empty/degraded reply.

## The three postures (generic decision — `egress_posture.py`)

| Posture | Value | Generic decision | Wired behavior for `local_cli` |
|---|---|---|---|
| Open (**default**) | `open` | No cage. Outbound unrestricted. | Wired: today's unchanged behavior. |
| Allowlist (opt-in) | `allowlist` | The Seatbelt loopback-proxy cage (`sandbox.py` / `egress_proxy.py` / `providers/native.py`); fails closed without Seatbelt + the egress proxy. | **Honored — runs really caged** (live-verified). Fails closed without the cage. |
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

# Raises GraphValidationError for a local_cli plan under broker, or under allowlist without
# the cage (see above); otherwise returns the generic decision — OPEN or ALLOWLIST (with the
# resolved destinations) for a local_cli plan.
local_cli_decision = resolve_local_cli_egress_decision(plan, environ=environ, capabilities=caps)
```

`_build_policy` uses `local_cli_decision.network_mode`/`.network_destinations` for the
`local_cli` branch directly (OPEN or ALLOWLIST, isolation lifted the same way `https`'s is
under ALLOWLIST) with a defensive backstop raise for anything else — unreachable via
`build_execution_controller`'s own preflight refusal, but a real guard for the
`LocalGraphRuntimeFacade` path too, since it calls `build_execution_controller` directly
without a separate preflight step. `LocalCliConnectorWorker.execute()` then builds the actual
Seatbelt-caged launch (see "The caged path" above) when the envelope says ALLOWLIST.

`decision.requires_broker` exists precisely so a caller cannot mistake
`network_mode is None` (true only for `broker`) for "no restriction."

## Host admission check

`EgressPostureConfig.allowlist_admits(hostname, port) -> bool` is an exact
`(hostname, port)` membership test against the resolved allowlist — meaningful
only under `allowlist` posture (always `False` for `open`/`broker`, and `False`
rather than a raise for input that cannot even form a valid destination, e.g. an
IP literal).

## Tests

- `tests/graph/adapters/enforcement/test_egress_posture.py` — 49 tests, 100% line coverage of
  `egress_posture.py` (resolution + the generic capability decision), including the CRIT-#2
  regression tests (a stray `BOUNDED_LOOPS_EGRESS_ALLOWLIST` never affects a non-allowlist run).
- `tests/graph/application/test_egress_posture_policy.py` — unit tests for
  `resolve_local_cli_egress_decision`: OPEN unaffected, ALLOWLIST honored (with/without the
  cage), BROKER still refused, a plan with no `local_cli` node fully unaffected.
- `tests/graph/adapters/connectors/test_local_cli_worker.py` — hermetic tests (a stand-in CLI,
  no subscription/quota) for the caged-argv construction and fail-closed-without-cage path,
  PLUS live tests (`skipif` without real Seatbelt + the egress proxy, mirroring
  `test_sandboxed_worker.py`'s own idiom) that ACTUALLY run `sandbox-exec`: the proxy is
  reachable, every other destination is OS-denied, `HOME` stays readable, and a blocked CLI
  fails closed with a redacted diagnostic.
- `tests/graph/application/test_execute_graph_egress_posture.py` — end-to-end tests driving a
  real plan through `execute_graph_run`: default backward compat, a live ALLOWLIST-caged
  success run (network-probe stand-in, proxy reachable / other destinations denied),
  ALLOWLIST-without-the-cage and BROKER preflight refusal, and a misconfigured posture value
  failing closed cleanly.
- `tests/graph/application/test_execute_graph_byok.py` — 2 tests proving an `https` node
  succeeds identically under `allowlist` (with NO cage on the host) and `broker` posture — the
  case that caught the capability-leak gap during development of the PRIOR (refusal) pass;
  still green after this pass honors ALLOWLIST for `local_cli`.
