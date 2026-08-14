"""``bl monitor`` — serve the bounded-loops monitor on a loopback port.

Prints one URL, containing the session token, and blocks until interrupted. Nothing is written
outside `.bounded-loops/`, no port other than 127.0.0.1 is bound, and the token dies with the
process.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from bounded_loops.domain.errors import ManifestError
from bounded_loops.workspace import ensure


def register(graph_subs: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register `monitor` onto a subparser collection."""
    parser = graph_subs.add_parser(
        "monitor",
        help="Watch what your agent is doing, configure graphs, approve gates, run.",
        description=(
            "The live window onto what your orchestrator is doing to this project: run states, "
            "the graph as it executes, spend against ceilings, and the receipt behind every "
            "node. Also where you configure any authorable field, approve a human gate, and "
            "press Run. Loopback only, protected by a token generated for this one "
            "invocation.\n\n"
            "It takes no instructions. You describe work to your ORCHESTRATOR — Claude Code, "
            "Codex, Cursor, a CLI — which composes graphs over MCP using the shipped skill. "
            "This monitors and configures what that produced."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="<n>",
        help="Port to bind on 127.0.0.1 (default: an ephemeral one the OS picks).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the URL without opening a browser.",
    )
    parser.set_defaults(func=cmd_monitor)


def cmd_monitor(args: argparse.Namespace) -> int:
    """Start the monitor and block. Returns 0 on a clean interrupt."""
    from bounded_loops.graph.jarvis.server import JarvisServer

    port = getattr(args, "port", 0) or 0
    if not 0 <= port <= 65535:
        print(f"error: --port {port} is not a port number", file=sys.stderr)
        return 2

    try:
        server = JarvisServer(port=port)
    except OSError as exc:
        print(f"error: cannot bind 127.0.0.1:{port} — {exc}", file=sys.stderr)
        return 2

    try:
        created = ensure(server.workspace)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        server.server_close()
        return 2

    url = server.app_url
    print("bounded-loops monitor")
    print("=" * 40)
    print(f"  workspace : {server.workspace.root}")
    print(f"              chosen because {server.workspace.reason}")
    if created:
        print(f"  created   : {len(created)} missing part(s) of the workspace layout")
    print(f"  listening : 127.0.0.1:{server.server_address[1]}  (loopback only)")
    print()
    print("  Open this URL — the token in it is this session's only credential:")
    print(f"  {url}")
    print()
    print("  The token is not stored anywhere and dies with this process. Do not paste the URL")
    print("  into a chat, an issue, or a shared terminal: anything holding it can drive this")
    print("  console until you stop it. Ctrl-C to stop.")
    print()

    if not getattr(args, "no_browser", False):
        # Best-effort. A headless or locked-down environment simply gets the printed URL, which
        # is why the URL is printed BEFORE this is attempted rather than instead of it.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - never let a browser launcher stop the server
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMonitor stopped. The session token is now invalid.")
    finally:
        server.server_close()
    return 0
