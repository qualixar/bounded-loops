"""`bl graph console` handler — the browser-facing twin of `bl graph approve`.

Serves a minimal, loopback-only HTML page listing one run's paused
approval-checkpoint nodes with Approve/Reject buttons. Every click drives
``LocalGraphRuntimeFacade.approve()`` — Slice 1's durable machinery — through
``ConsoleServer`` in ``server.py``; this module adds no approval logic of its
own, only the CLI argument handling and the print/serve/close lifecycle.

LOCAL TRUST POSTURE — stated once here, in the operator's terminal, and again
on the page itself (see ``console_template.html``): the printed token is the
ONLY thing gating this console. It is meant to be read by the human running
this command; anyone else on this machine who obtains it can decide approvals
for this run until the console exits. A HOSTED deployment must not reuse this
command's model — it needs real auth, TLS, and a role-checking authorizer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bounded_loops.graph.console.server import ConsoleOpenError, ConsoleServer, open_console_run


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def cmd_graph_console(args: argparse.Namespace) -> int:
    """bl graph console --run <dir> [--port <port>].

    Opens *dir* as a single run (flat-addressed, exactly like `bl graph approve`),
    binds a loopback-only HTTP server, prints the URL (with its one-time token) the
    operator opens in a browser, then serves until every paused approval node is
    decided or the operator quits with Ctrl-C.
    """
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph console: '{run_dir}' is not a directory")
        return 2

    try:
        identity, facade = open_console_run(run_dir)
    except ConsoleOpenError as exc:
        _err(f"graph console: {exc}")
        return 2

    port = int(getattr(args, "port", 0) or 0)
    try:
        server = ConsoleServer(identity=identity, facade=facade, port=port)
    except (OSError, OverflowError) as exc:
        _err(f"graph console: cannot bind a loopback socket on port {port} — {exc}")
        return 2

    return _serve(server)


def _serve(server: ConsoleServer) -> int:
    print(f"Approval console ready:  {server.console_url}")
    print("LOCAL posture: this token gates other LOCAL processes on this machine only.")
    print("A hosted deployment needs real auth, TLS, and a role-checking authorizer.")
    print("Press Ctrl-C to stop — it also stops on its own once every pending node is decided.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("Approval console closed.")
    return 0
