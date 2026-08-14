"""`bl graph watch` handler — live SSE Arena for one run directory.

Opens ``--run`` as a single, already-started run directory and serves a live
Graph Arena that refreshes automatically as the controller appends new events to
``controller-events.jsonl``.  The page is identical to ``bl graph arena`` but
connected to an SSE stream: it shows spend/budget and node-state changes in
near-real time without a browser reload.

LOCAL TRUST POSTURE — identical to ``bl graph console``:
  * Binds ``127.0.0.1`` only.
  * One-time token per invocation, printed to stdout.  No file, no log.
  * Anyone on this machine who reads the token can read live run state.
  * A hosted deployment needs real auth, TLS, and access control; this command
    is not a substitute for that.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bounded_loops.graph.live.sse_server import WatchOpenError, WatchServer, open_watch_run


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_watch(args: argparse.Namespace) -> int:
    """bl graph watch --run <dir> [--port <port>].

    Opens *dir* as a single run, binds a loopback-only SSE server, prints the
    URL (with its one-time token) to open in a browser, then streams
    ArenaProjection snapshots to any connected browser tab until the run
    reaches a terminal state or the operator quits with Ctrl-C.

    The token is printed once to stdout and is NEVER written to disk or
    echoed in any log.  Do not forward this token to untrusted parties.
    """
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph watch: '{run_dir}' is not a directory")
        return 2

    try:
        identity, facade = open_watch_run(run_dir)
    except WatchOpenError as exc:
        _err(f"graph watch: {exc}")
        return 2

    port: int = int(getattr(args, "port", 0) or 0)
    try:
        server = WatchServer(identity=identity, facade=facade, run_dir=run_dir, port=port)
    except (OSError, OverflowError) as exc:
        _err(f"graph watch: cannot bind a loopback socket on port {port} — {exc}")
        return 2

    return _serve(server)


def _serve(server: WatchServer) -> int:
    # The token is printed once here — it is NEVER written to a file, echoed
    # in a log, or passed to any other function.  The caller (a human) opens
    # the URL in their local browser.
    print(f"Live Arena ready:  {server.watch_url}")
    print("LOCAL posture: this token gates other LOCAL processes on this machine only.")
    print("A hosted deployment needs real auth, TLS, and a role-checking authorizer.")
    print("Streaming: page updates automatically. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Live Arena closed.")
    return 0
