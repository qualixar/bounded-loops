"""``bl loops`` — browse and filter the shipped loop catalog.

``bl loops list`` is the node catalog: it reads every loop in the bounded-loops
``loops/`` directory (found via the nearest repo root or the current project's
own ``loops/`` directory), applies optional filters, and prints a rich table
showing name, roles, gate kind, keyless status, and the one-line description.

Keyless definition: runner.default is ``stub`` or ``shell`` (no API key
required). The four framework-example loops (adk-example, autogen-example,
crewai-example, langgraph-example) use ``python_callable`` with real framework
imports — they are excluded by ``--keyless``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bounded_loops.application.manifest import load as manifest_load
from bounded_loops.domain.errors import ManifestError

# Runners that need no API key.
_KEYLESS_RUNNERS = frozenset({"stub", "shell"})


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``loops`` subcommand and its children onto *subparsers*."""
    loops_parser = subparsers.add_parser(
        "loops",
        help="Browse the shipped loop catalog.",
        description=(
            "Commands for browsing the bounded-loops catalog. "
            "Use `bl loops list` to see all 68 shipped loops with optional "
            "role and gate-kind filters."
        ),
    )
    loops_sub = loops_parser.add_subparsers(dest="loops_cmd", metavar="ACTION")

    # ── bl loops install ──────────────────────────────────────────────────────
    install_parser = loops_sub.add_parser(
        "install",
        help="Copy a shipped loop into this project so you can run and edit it.",
        description=(
            "Copies a loop from the bundled catalog into your project's workspace. "
            "`bl run` writes its ledger BESIDE the loop, so a loop has to live somewhere "
            "writable — which site-packages is not. Nothing is downloaded: the catalog "
            "ships inside the package and this works offline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bl loops install bug-fix-red-green      # into .bounded-loops/loops/\n"
            "  bl loops install a11y --dest ./loops    # somewhere else\n"
            "  bl loops install bug-fix-red-green --overwrite\n"
        ),
    )
    install_parser.add_argument("name", help="Loop name, as shown by `bl loops list`.")
    install_parser.add_argument(
        "--dest",
        metavar="DIR",
        default=None,
        help="Where to put it (default: this project's .bounded-loops/loops/).",
    )
    install_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing copy. Refused unless the target is itself a loop package.",
    )

    # ── bl loops list ─────────────────────────────────────────────────────────
    list_parser = loops_sub.add_parser(
        "list",
        help="List catalog loops with optional role/gate filtering.",
        description=(
            "Discovers all loops in the bounded-loops catalog and the current "
            "project, then prints a filterable table: name, role(s), gate kind, "
            "keyless status, and one-line description. Returns 0 always — lint "
            "failures surface via `bl lint`, not here."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  bl loops list                      # all catalog loops\n"
            "  bl loops list --role security      # security-tagged loops only\n"
            "  bl loops list --gate pytest        # pytest-gated loops only\n"
            "  bl loops list --keyless            # no API key required\n"
            "  bl loops list --role finance --gate command\n"
            "  bl loops list --json               # machine-readable JSON\n"
        ),
    )
    list_parser.add_argument(
        "--role",
        metavar="ROLE",
        default=None,
        help=(
            "Filter: show only loops whose role list contains ROLE. "
            "Example: --role security"
        ),
    )
    list_parser.add_argument(
        "--gate",
        metavar="GATE_KIND",
        default=None,
        help=(
            "Filter: show only loops with this gate kind. "
            "Example: --gate pytest"
        ),
    )
    list_parser.add_argument(
        "--keyless",
        action="store_true",
        help=(
            "Filter: show only loops that need no API key "
            "(runner.default is stub or shell)."
        ),
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit results as a JSON array instead of a table.",
    )
    list_parser.set_defaults(func=_cmd_loops_list)
    install_parser.set_defaults(func=_cmd_loops_install)

    loops_parser.set_defaults(func=_cmd_loops_no_action)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def _cmd_loops_no_action(args: argparse.Namespace) -> int:
    """Printed when `bl loops` is invoked with no sub-action."""
    _err(
        "bl loops: no action given. "
        "Available actions: list, install. "
        "Run `bl loops list --help` for usage."
    )
    return 1


def _cmd_loops_list(args: argparse.Namespace) -> int:
    """
    bl loops list [--role ROLE] [--gate GATE_KIND] [--keyless] [--json]

    Algorithm:
    1. Discover loop.yaml paths (bounded-loops catalog + current project loops).
    2. Load each manifest; record parse errors without crashing.
    3. Apply --role, --gate, --keyless filters.
    4. Print table or JSON.
    5. Return 0 always.
    """
    raw_entries = _collect_loop_entries()

    # Apply filters
    role_filter: str | None = getattr(args, "role", None)
    gate_filter: str | None = getattr(args, "gate", None)
    keyless_only: bool = getattr(args, "keyless", False)

    entries = [
        e for e in raw_entries
        if _matches_filters(e, role_filter, gate_filter, keyless_only)
    ]

    emit_json: bool = getattr(args, "emit_json", False)
    if emit_json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        _print_catalog_table(entries, role_filter, gate_filter, keyless_only)

    return 0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _collect_loop_entries() -> list[dict]:
    """
    Collect all loop entries from:
    1. The bounded-loops catalog (loops/ relative to the nearest repo root that
       has a loops/ directory with loop.yaml files).
    2. The current project's own loops/ and direct loop directories.

    Returns a list of dicts with: name, roles, gate_kind, keyless, description,
    path, error (None on success).
    """
    yaml_files: list[Path] = _discover_catalog_yamls()
    entries: list[dict] = []
    seen: set[str] = set()

    for yaml_file in yaml_files:
        loop_dir = yaml_file.parent
        key = str(loop_dir.resolve())
        if key in seen:
            continue
        seen.add(key)

        entry = _load_entry(loop_dir)
        entries.append(entry)

    return entries


def _discover_catalog_yamls() -> list[Path]:
    """
    Collect loop.yaml paths from the catalog and the current project.

    Strategy:
    - Walk UP from cwd looking for directories named ``loops`` that contain
      at least one ``*/loop.yaml`` — this finds both a bounded-loops source
      checkout (loops/ at the repo root) and a user project with its own
      loops/ directory.
    - Also search cwd/*/loop.yaml and cwd/loops/*/loop.yaml directly.
    - Deduplicate by resolved loop directory path.
    - Sort for deterministic output.
    """
    found: dict[str, Path] = {}
    cwd = Path.cwd()

    # Search cwd and the loops/ subdirectory directly
    for base in [cwd, cwd / "loops"]:
        _collect_from_base(base, found)

    # Walk UP looking for repo roots that have a loops/ directory
    for ancestor in cwd.parents:
        loops_dir = ancestor / "loops"
        if loops_dir.is_dir():
            _collect_from_base(loops_dir, found)
            # One level is enough — stop after the first loops/ directory
            # above cwd that has actual loops.
            if found:
                break

    # Nothing on disk: fall back to the catalog bundled in the wheel. Without this a
    # `pip install bounded-loops` user ran `bl loops list`, got "No loops found", and was
    # told to "run from a bounded-loops source checkout" — the package advertises 68 loops
    # and then asks you to go and clone them.
    if not found:
        from bounded_loops.catalog_access import packaged_catalog_root

        packaged = packaged_catalog_root()
        if packaged is not None:
            _collect_from_base(packaged, found)

    return [found[k] for k in sorted(found)]


def _collect_from_base(base: Path, found: dict[str, Path]) -> None:
    """Add loop.yaml paths found one level under *base* into *found*."""
    if not base.is_dir():
        return
    # A directory that IS a loop (has its own loop.yaml)
    own = base / "loop.yaml"
    if own.is_file():
        found.setdefault(str(base.resolve()), own)
    # Children of base
    for yaml_file in base.glob("*/loop.yaml"):
        found.setdefault(str(yaml_file.parent.resolve()), yaml_file)


def _load_entry(loop_dir: Path) -> dict:
    """Load one loop manifest and return a catalog entry dict."""
    try:
        manifest = manifest_load(loop_dir)
        runner_kind = manifest.runner_kind
        return {
            "name":        manifest.name,
            "roles":       list(manifest.raw.get("role", [])),
            "gate_kind":   manifest.gate_kind,
            "keyless":     runner_kind in _KEYLESS_RUNNERS,
            "runner":      runner_kind,
            "rung":        manifest.rung.value,
            "description": _one_line(manifest.raw.get("description", "")),
            "path":        str(loop_dir),
            "error":       None,
        }
    except ManifestError as exc:
        return {
            "name":        loop_dir.name,
            "roles":       [],
            "gate_kind":   "?",
            "keyless":     False,
            "runner":      "?",
            "rung":        "?",
            "description": "",
            "path":        str(loop_dir),
            "error":       str(exc),
        }


def _one_line(text: str) -> str:
    """Return the first non-empty line of *text*, stripped."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _matches_filters(
    entry: dict,
    role_filter: str | None,
    gate_filter: str | None,
    keyless_only: bool,
) -> bool:
    """Return True iff *entry* satisfies all active filters."""
    if entry["error"] is not None:
        # Always include error entries so problems are visible.
        return True
    if role_filter is not None:
        if role_filter.lower() not in [r.lower() for r in entry["roles"]]:
            return False
    if gate_filter is not None:
        if entry["gate_kind"].lower() != gate_filter.lower():
            return False
    if keyless_only and not entry["keyless"]:
        return False
    return True


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

_KEY_SYMBOL = "  "   # no key needed
_LOCK_SYMBOL = "* "  # key needed


def _print_catalog_table(
    entries: list[dict],
    role_filter: str | None,
    gate_filter: str | None,
    keyless_only: bool,
) -> None:
    """Print the catalog as a human-readable, column-aligned table."""
    if not entries:
        _print_empty_message(role_filter, gate_filter, keyless_only)
        return

    _print_filter_header(role_filter, gate_filter, keyless_only, len(entries))

    # Column widths — computed from data so the table adapts to any loop name length.
    name_w = max(len(e["name"]) for e in entries)
    name_w = max(name_w, 10)
    roles_w = 20
    gate_w = 12

    header = (
        f"  {'NAME':<{name_w}}  {'ROLE(S)':<{roles_w}}  "
        f"{'GATE':<{gate_w}}  {'KEY':<6}  DESCRIPTION"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for e in entries:
        if e["error"]:
            print(f"  {'[ERROR]':<{name_w}}  {'':<{roles_w}}  {'':<{gate_w}}  "
                  f"{'':6}  {e['error']}")
            continue
        key_col = "keyless" if e["keyless"] else "key req"
        roles_str = ", ".join(e["roles"]) if e["roles"] else "?"
        # Truncate description to fit an 80-column terminal
        desc = e["description"]
        max_desc = max(20, 100 - name_w - roles_w - gate_w - 20)
        if len(desc) > max_desc:
            desc = desc[:max_desc - 1] + "…"
        print(
            f"  {e['name']:<{name_w}}  {roles_str:<{roles_w}}  "
            f"{e['gate_kind']:<{gate_w}}  {key_col:<7}  {desc}"
        )

    print()
    totals = _summary_line(entries)
    print(f"  {totals}")


def _print_filter_header(
    role_filter: str | None,
    gate_filter: str | None,
    keyless_only: bool,
    count: int,
) -> None:
    parts: list[str] = []
    if role_filter:
        parts.append(f"role={role_filter}")
    if gate_filter:
        parts.append(f"gate={gate_filter}")
    if keyless_only:
        parts.append("keyless")
    filter_str = ", ".join(parts)
    label = f"({filter_str})" if filter_str else "(all)"
    print(f"\nLoop catalog {label} — {count} loop(s)\n")


def _print_empty_message(
    role_filter: str | None,
    gate_filter: str | None,
    keyless_only: bool,
) -> None:
    parts: list[str] = []
    if role_filter:
        parts.append(f"role={role_filter!r}")
    if gate_filter:
        parts.append(f"gate={gate_filter!r}")
    if keyless_only:
        parts.append("--keyless")
    if parts:
        print(f"No loops match: {', '.join(parts)}.")
        print("Run `bl loops list` without filters to see the full catalog.")
    else:
        print("No loops found.")
        print(
            "Run from a bounded-loops source checkout, or create loops with "
            "`bl loop new <name>`."
        )


def _summary_line(entries: list[dict]) -> str:
    ok = [e for e in entries if not e["error"]]
    keyless_count = sum(1 for e in ok if e["keyless"])
    gate_counts: dict[str, int] = {}
    for e in ok:
        gate_counts[e["gate_kind"]] = gate_counts.get(e["gate_kind"], 0) + 1
    gate_summary = "  ".join(
        f"{k}:{v}" for k, v in sorted(gate_counts.items(), key=lambda x: -x[1])
    )
    return (
        f"{len(ok)} loop(s)  keyless: {keyless_count}  "
        f"gate breakdown: {gate_summary}"
    )


# ---------------------------------------------------------------------------
# bl loops install
# ---------------------------------------------------------------------------

def _cmd_loops_install(args: argparse.Namespace) -> int:
    """Copy a bundled loop into the project so it can actually be run.

    Before 0.6.1 there was nothing to copy: the catalog lived only in the git repository, so
    `pip install bounded-loops` produced an engine with no loops and the README had to tell
    people to clone the repo to get what the package advertises.
    """
    from bounded_loops.catalog_access import install_loop, packaged_loop_names

    if args.dest is not None:
        destination = Path(args.dest)
    else:
        from bounded_loops.workspace import discover, ensure

        workspace = discover()
        ensure(workspace)
        destination = workspace.root / "loops"

    try:
        installed = install_loop(args.name, destination, overwrite=args.overwrite)
    except FileExistsError as exc:
        _err(f"{exc} already exists — pass --overwrite to replace it")
        return 2
    except LookupError as exc:
        _err(str(exc))
        names = packaged_loop_names()
        if names:
            close = [n for n in names if args.name.lower() in n.lower()][:5]
            if close:
                print("Did you mean: " + ", ".join(close), file=sys.stderr)
            print(
                f"{len(names)} loops are available — run `bl loops list`.", file=sys.stderr
            )
        return 2
    except OSError as exc:
        _err(f"could not install {args.name!r}: {exc}")
        return 2

    print(f"installed {args.name} -> {installed}")
    print()
    print("Run it:")
    print(f"  bl run {installed}")
    return 0
