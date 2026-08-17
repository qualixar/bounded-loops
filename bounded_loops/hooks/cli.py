"""`bounded-loops-hook` — one console-script entry point for every editor hook.

Task #67. The plugin manifests invoked hooks as `python3 -m bounded_loops.hooks.X <host>`,
which resolves `python3` against the *user's* PATH rather than the environment the package
is installed in. Those are frequently not the same interpreter: pipx, uv tool, a project
venv, and Homebrew Python all put a `python3` on PATH that cannot import a package
installed somewhere else. The failure is a bare `ModuleNotFoundError` inside an editor
hook, where nobody sees it.

This is not hypothetical. The same mistake broke a whole experiment arm in this project:
a shim invoked `python3` and could not import the engine outside `uv run`, and the arm
reported every run as a halt until the launcher was fixed.

A console script is generated with a shebang pointing at the interpreter that installed
it, so `bounded-loops-hook` on PATH is by construction an interpreter that can import
`bounded_loops`.

The `python3 -m` form keeps working — each hook module still has its own `__main__`
block — so a user who already copied a command into their own configuration is not
broken by this change.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

#: Hook name as written in a plugin manifest → the module's `main`. Hyphenated names,
#: because that is the convention every other CLI surface in this package uses.
_HOOKS: dict[str, str] = {
    "verify": "bounded_loops.hooks.verify_bounded_loop",
    "graph-run-stop": "bounded_loops.hooks.graph_run_stop",
    "pretooluse-loop-package": "bounded_loops.hooks.pretooluse_loop_package",
}


def _resolve(name: str) -> Callable[[list[str]], int]:
    from importlib import import_module

    module = import_module(_HOOKS[name])
    return module.main


def main(argv: list[str] | None = None) -> int:
    """Dispatch to one hook. `bounded-loops-hook <hook-name> [host [...]]`.

    Exits 0 on an unknown hook name after printing to stderr, because a hook that fails
    hard blocks the editor action it is attached to. A misconfigured hook should be
    visible and harmless, not a wedge that stops someone saving a file.
    """
    # argv[0] is the program name when called as a console script.
    args = (sys.argv if argv is None else argv)[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "usage: bounded-loops-hook <hook> [host]\n"
            f"hooks: {', '.join(sorted(_HOOKS))}",
            file=sys.stderr,
        )
        return 0
    name, rest = args[0], args[1:]
    if name not in _HOOKS:
        print(
            f"[bounded-loops] unknown hook {name!r}; expected one of "
            f"{', '.join(sorted(_HOOKS))}. Not blocking the editor action.",
            file=sys.stderr,
        )
        return 0
    # Each hook's `main` expects sys.argv-shaped input: name first, then its arguments.
    return _resolve(name)([f"bounded-loops-hook:{name}", *rest])


if __name__ == "__main__":
    sys.exit(main())
