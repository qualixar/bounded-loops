# `bl monitor` — watching a run

A local web UI over a run directory. Live DAG, per-node evidence, spend, approval controls.

```bash
bl monitor                # current workspace, ephemeral port, opens your browser
bl monitor --port 8791    # a fixed port
bl monitor --no-browser   # print the URL and stay put
```

The workspace is resolved the same way every other command resolves it — walking up for a
`.bounded-loops/`, bounded by the git root. To watch a different project, point
`BOUNDED_LOOPS_WORKSPACE` at it or run the command from inside it. The startup banner prints
which workspace it chose **and why**, so a monitor looking at the wrong runs says so in its
first four lines.

The URL it prints contains a token. That URL **is** the credential for the session.

---

## What it is, and what it deliberately is not

The monitor is a **driver over the run directory**, in the same sense the CLI and the MCP
server are. It holds no state the receipt log lacks. Three consequences worth knowing before
you rely on it:

**Closing it loses nothing.** There is no session to resume, no in-memory progress. Kill the
process mid-run and the run keeps going — it is a separate process — and the next monitor you
start reads the same receipts.

**It cannot disagree with the CLI.** Both read `controller-events.jsonl`. When a surface has
disagreed with the log in this project's history, that was the bug, and the log was the
arbiter. 0.6.0 fixed five of them.

**It is not a server.** No remote mode, no auth system, no multi-user story. It binds
`127.0.0.1` and is meant for the machine the run is on.

---

## The security posture, stated exactly

| Control | What it does |
|---|---|
| **Loopback bind** | `127.0.0.1` only, never `0.0.0.0`. A machine on your network cannot reach it. |
| **Per-invocation token** | `secrets.token_urlsafe(32)`. Printed once, never written to disk, dies with the process. Compared with `hmac.compare_digest`. |
| **Same-origin required** | Every data route also checks `Origin`/`Referer` against the exact bind address. A page in another tab cannot drive the monitor even if it obtained the token. |
| **Connection cap** | 8 concurrent, then `503`. Bounded rather than unbounded queueing. |
| **Body cap** | 520 KB, enforced before the read, not after. |
| **CSP + `X-Frame-Options: DENY`** | Everything the UI loads is packaged and same-origin — React and htm are vendored, no CDN, no outbound links — so the policy is closed rather than merely tidy. |
| **`Referrer-Policy: no-referrer`** | The token lives in the URL by design; this stops it reaching anywhere via `Referer`. |

**What this is not.** It is not hardened against someone who already has a shell on your
machine. Such a person can read the process's memory, `kill` it, or write the run directory
directly. The threat model is a browser tab and a curious process on the same host, not a
local attacker with your privileges.

**The token in the URL is a deliberate trade.** `EventSource` cannot send an `Authorization`
header, so a query-string token is what makes the live stream work at all. `no-referrer`,
`Cache-Control: no-store`, and the ephemeral lifetime are the mitigations. It will appear in
your shell history if you paste it, and in a screenshot if you take one.

---

## Reading the panels honestly

The reason to prefer this over a generic dashboard is that it declines to round anything up.

**A node the gate never evaluated says so.** An approval node succeeds because a human held
it; a join succeeds because its branches did. Neither has a gate verdict. The panel reads
`no verdict — the gate has not evaluated this node` rather than showing a pass. (Until 0.6.0
the Arena did paint a pass there. It was wrong, and it is fixed.)

**A missing ceiling reads `none`, not blank.** A blank cell reads as "nothing to worry about";
the truth is the opposite, and this is the last screen before real work starts.

**Approving names what it releases.** An approval node usually declares no effects of its own,
so "approve this" looks harmless right up until the publish it lets through. The confirm panel
lists the effects reachable downstream of the gate and flags the ones that stopping the run
will not take back — anything that leaves this machine: `external_write`, `financial`,
`irreversible`.

**Spend totals say when they are a lower bound.** If an attempt reported no usage, the total
is marked incomplete rather than presented as a measurement.

---

## Starting a run from the UI

`Execute` previews first and always. The preview shows the compiled plan's effects, every
node's ceilings, and where the graph pauses for a human. Nothing is written until you confirm
— and confirmation requires the boolean `true`, not merely a truthy value, because a client
that stringifies its booleans should not be able to turn a preview into an execution.

A run started here is an ordinary run directory. `bl graph status`, `bl graph approve`, and
`bl graph arena` all work on it, and it survives the monitor exiting.

---

## When to use the Arena instead

`bl graph arena --run <dir>` writes one self-contained HTML file — no server, no network, no
build step. Use it when the audience is someone who was not there: a reviewer, an auditor, a
teammate in another timezone. The monitor is for watching; the Arena is for sending.

---

## Troubleshooting

**The URL prints but the page will not load.** Check you are opening `127.0.0.1` and not
`localhost` — they are different origins, and the same-origin check is exact. The page will
load on `localhost` and then every data request will fail.

**`503` on everything.** Eight connections are already held. Close other monitor tabs; SSE
streams hold a connection for the life of the run.

**"No runs yet" with runs on disk.** The monitor is looking at a different workspace. Its
startup banner names the one it chose and why; `bl where` explains the same resolution. Set
`BOUNDED_LOOPS_WORKSPACE` to override.

**Nothing appears when you click a run.** Check the terminal running `bl monitor` — refusals
that cannot be rendered are printed there.
