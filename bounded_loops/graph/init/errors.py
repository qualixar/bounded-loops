"""The one expected-failure exception type for the `bl graph init` package.

Mirrors ``ConsoleOpenError`` in ``console/server.py``: a small, dedicated type for
THIS package's "clean, user-facing refusal" surface (bad host, symlinked config
path, unwritable directory, ...) — never a raw ``OSError``/``GraphValidationError``
leaking a traceback to the CLI. ``cmd_graph_init`` catches exactly this type at each
call site and turns it into a one-line ``error: ...`` message plus exit code 2.
"""

from __future__ import annotations


class GraphInitError(Exception):
    """A clean, expected `bl graph init` failure — always safe to print to the user."""
