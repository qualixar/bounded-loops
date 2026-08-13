"""``bl loop`` — work with individual loops: scaffold via ``bl loop new``.

Split out of ``cli.py`` for the 800-line cap and cohesion: all loop-lifecycle
operations (scaffold, inspect) live here. The only command shipped in v1 is
``bl loop new``, which wraps the existing template machinery from ``cli_new.py``
with a friendlier interface: name-first instead of template-first, and an
explicit ``--gate`` flag so users do not have to know which template name maps
to which gate kind.

Gate-to-template mapping (v1):
    command   -> command-basic   (stdlib only, no extra deps)
    pytest    -> pytest-basic    (requires pytest, already a declared dep)
    jsonschema -> jsonschema-basic (requires jsonschema, already a declared dep)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bounded_loops.cli_new import _cmd_new

# Map the user-visible gate kind to the packaged template name.
_GATE_TO_TEMPLATE: dict[str, str] = {
    "command": "command-basic",
    "pytest": "pytest-basic",
    "jsonschema": "jsonschema-basic",
}
_DEFAULT_GATE = "command"


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``loop`` subcommand and its children onto *subparsers*."""
    loop_parser = subparsers.add_parser(
        "loop",
        help="Scaffold and inspect individual loops.",
        description=(
            "Commands for working with individual bounded-loop packages. "
            "Use `bl loop new <name>` to scaffold a complete, immediately-runnable "
            "loop directory; use `bl loops list` to browse the shipped catalog."
        ),
    )
    loop_sub = loop_parser.add_subparsers(dest="loop_cmd", metavar="ACTION")

    # ── bl loop new <name> ────────────────────────────────────────────────────
    new_parser = loop_sub.add_parser(
        "new",
        help="Scaffold a new loop package.",
        description=(
            "Creates a complete, valid, immediately-runnable loop directory. "
            "Pass --gate to choose the gate kind; the matching starter template "
            "is copied, {{LOOP_NAME}} substituted, and the result is ready to "
            "`bl run` without any editing or API key."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bl loop new my-checker                   # command gate (default)\n"
            "  bl loop new my-checker --gate pytest     # pytest gate\n"
            "  bl loop new my-checker --gate jsonschema # JSON Schema gate\n"
            "  bl loop new my-checker --dest ~/loops    # emit into ~/loops/my-checker\n"
        ),
    )
    new_parser.add_argument(
        "name",
        help=(
            "Loop name: used as the loop's `name:` in loop.yaml and as the "
            "destination directory name (unless --dest is given). "
            "Letters, digits, hyphens, underscores — no slashes."
        ),
    )
    new_parser.add_argument(
        "--gate",
        choices=list(_GATE_TO_TEMPLATE),
        default=_DEFAULT_GATE,
        metavar="GATE_KIND",
        help=(
            f"Gate kind to scaffold (default: {_DEFAULT_GATE}). "
            "command = stdlib-only Python checker, no extra deps; "
            "pytest = pytest test suite (pytest is a declared dep); "
            "jsonschema = JSON Schema validation (jsonschema is a declared dep)."
        ),
    )
    new_parser.add_argument(
        "--dest",
        metavar="DIR",
        type=Path,
        default=None,
        help=(
            "Destination directory. Default: ./<name>. "
            "The loop directory will be created at this path. "
            "Fails if the path already exists."
        ),
    )
    new_parser.set_defaults(func=_cmd_loop_new)

    loop_parser.set_defaults(func=_cmd_loop_no_action)


def _cmd_loop_no_action(args: argparse.Namespace) -> int:
    """Printed when `bl loop` is invoked with no sub-action."""
    _err(
        "bl loop: no action given. "
        "Available actions: new. "
        "Run `bl loop new --help` for usage."
    )
    return 1


def _cmd_loop_new(args: argparse.Namespace) -> int:
    """
    bl loop new <name> [--gate {command,pytest,jsonschema}] [--dest DIR]

    Algorithm:
    1. Validate <name> — must be a safe single path segment (same regex as
       bl new's template name: letters, digits, hyphens, underscores).
    2. Map gate kind → template name via _GATE_TO_TEMPLATE.
    3. Compute destination: --dest if given, else ./<name>.
    4. Build an argparse.Namespace that matches what _cmd_new expects, then
       delegate to _cmd_new — no code duplication, same copy/substitute/chmod
       logic, same packaged-resource resolution.
    5. Print extra guidance about the gate kind chosen.
    """
    import re

    name = args.name
    _LOOP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    if not _LOOP_NAME_RE.fullmatch(name):
        _err(
            f"bl loop new: {name!r} is not a valid loop name. "
            "Use letters, digits, hyphens, and underscores only "
            "(no slashes, spaces, or dots at the start)."
        )
        return 1

    gate = getattr(args, "gate", _DEFAULT_GATE)
    template = _GATE_TO_TEMPLATE[gate]

    dest: Path
    if args.dest is not None:
        # --dest is the EXACT destination path — the caller controls the full
        # path, including whether to nest under a parent directory.
        # This matches bl new's own <destination> semantics.
        dest = args.dest
    else:
        dest = Path(name)

    # Delegate to the existing _cmd_new with a synthetic Namespace so all the
    # security hardening (path-traversal rejection, overwrite guard, chmod logic)
    # is exercised exactly once, in one place.
    synthetic = argparse.Namespace(
        template=template,
        destination=dest,
        name=name,
        list=False,
    )
    rc = _cmd_new(synthetic)
    if rc == 0:
        _print_next_steps(name, gate, dest)
    return rc


def _print_next_steps(name: str, gate: str, dest: Path) -> None:
    """Print gate-specific next-steps guidance after a successful scaffold."""
    resolved = dest.resolve()
    print()
    print(f"Gate kind : {gate}")
    if gate == "command":
        print("Gate cmd  : python3 seed/check.py")
        print("           (checks seed/status.json; stub fixes it on lap 1)")
    elif gate == "pytest":
        print("Gate cmd  : pytest -q")
        print("           (stub writes seed/example.py with the correct fix on lap 1)")
    elif gate == "jsonschema":
        print("Gate      : JsonSchemaGate validates output.json against schema.json")
        print("           (stub writes a conformant output.json on lap 1)")
    print()
    print("Quick start:")
    print(f"  cd {resolved}")
    print("  bl run .         # engine run (keyless stub; reaches DONE on lap 1)")
    print("  ./run.sh         # standalone bash version, no engine required")
    print()
    print("To use a real agent:")
    print("  bl run . --runner claude-code   # or codex, shell, etc.")
    print()
    print("Edit PROMPT.md, seed/, and cassettes/default.json to build your own loop.")
