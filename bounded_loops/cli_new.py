"""``bl new`` — scaffold a loop from a bundled template.

Split out of ``cli.py`` in P3 for the 800-line cap, and cohesive on its own: this is the only part
of the CLI that WRITES into the user's project rather than reading it, and the only part that
touches packaged template resources.
"""

from __future__ import annotations

import argparse
import importlib.resources
import sys
import re
from pathlib import Path

_TEMPLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ── bl new ─────────────────────────────────────────────────────────────────────

def _templates_root() -> importlib.resources.abc.Traversable:
    """Resolves against the INSTALLED PACKAGE, not the user's cwd. Works identically whether bounded_loops is installed from
    a wheel or run from a source checkout — importlib.resources abstracts
    the difference. NEVER use _find_repo_root(Path.cwd()) here: that keys
    off the CALLER's own project root, which is dead on arrival for an end
    user who pip installs bounded-loops and runs `bl new` from their own,
    unrelated project."""
    return importlib.resources.files("bounded_loops") / "_templates"


def _cmd_new(args: argparse.Namespace) -> int:
    """
    bl new <template> <destination> [--name NAME]

    Algorithm:
    1. If args.list: list template dirs under the PACKAGED _templates/ root;
       tolerate the root not existing (empty list, not an error).
    2. Validate BOTH positionals are present (nargs="?" allows omission for
       --list alone, but the run path must reject a missing/None value with
       a clean message, not an uncaught TypeError).
    3. Validate <template> is a single, non-traversing name (regex above) —
       reject BEFORE joining it onto any path.
    4. Resolve the packaged template dir; if missing, error, exit 1.
    5. If <destination> already exists, error, exit 2 (never overwrite).
    6. Copy the template tree to <destination>, stripping the .tmpl suffix
       from the FINAL path component only, substituting {{LOOP_NAME}} in
       every file's content, skipping any symlink encountered in the walk.
    7. chmod +x whatever *.sh files actually exist in the destination
       (never assume run.sh/wreck.sh are both present).
    8. Print the destination path and next-steps hint; return 0.
    """
    if args.list:
        root = _templates_root()
        if not root.is_dir():
            return 0   # no templates bundled — empty list is not an error
        for entry in sorted(p.name for p in root.iterdir() if p.is_dir()):
            print(entry)
        return 0

    # fix: nargs="?" means argparse won't enforce these — the
    # handler must, with a clean message, not a TypeError from Path(None).
    if args.template is None or args.destination is None:
        _err("bl new: <template> and <destination> are required (or use --list).")
        return 1

    # fix: reject path traversal BEFORE building any path.
    # fullmatch (not match) — match()+bare "$" lets a trailing newline slip
    # through; low-stakes here since it'd just fail to resolve to a real
    # directory, but the validation should actually mean what it claims.
    if not _TEMPLATE_NAME_RE.fullmatch(args.template):
        _err(
            f"bl new: {args.template!r} is not a valid template name "
            "(letters, digits, '-', '_' only — no path separators)."
        )
        return 1

    root = _templates_root()
    template_dir = root / args.template
    if not template_dir.is_dir():
        _err(f"bl new: template '{args.template}' not found. "
             f"Run 'bl new --list' to see available templates.")
        return 1

    dest = Path(args.destination).resolve()
    if dest.exists():
        _err(f"bl new: destination '{dest}' already exists — refusing to overwrite.")
        return 2

    loop_name = args.name or dest.name
    with importlib.resources.as_file(template_dir) as real_template_dir:
        _copy_template(real_template_dir, dest, loop_name)

    # fix: discover *.sh files actually present rather than
    # hard-coding run.sh/wreck.sh — a template that legitimately omits one
    # no longer crashes here after already creating a half-scaffolded dest.
    for sh in dest.rglob("*.sh"):
        sh.chmod(0o755)

    print(f"Created loop at {dest}")
    print(f"Next: cd {dest} && ./run.sh")
    return 0


def _copy_template(template_dir: Path, dest: Path, loop_name: str) -> None:
    dest.mkdir(parents=True)
    for src_file in template_dir.rglob("*"):
        # fix: skip symlinks entirely — a contributed template
        # (this project explicitly invites community loop/template PRs) could
        # otherwise ship a symlink pointing outside the template tree, and
        # rglob + read_text would follow it, copying an arbitrary file (e.g.
        # ~/.ssh/id_rsa) into the generated loop. Mirrors the same precaution
        # composition._make_scratch_workspace already applies to loop seed/
        # dirs.
        if src_file.is_symlink():
            continue
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(template_dir)
        # fix: strip ".tmpl" only as a suffix of the FINAL path
        # component, never a whole-string substring replace (the original
        # `.replace(".tmpl", "")` would mangle e.g. "a.tmpld/file.py.tmpl"
        # into "ad/file.py" — verified concretely).
        dest_rel = rel.with_name(rel.name.removesuffix(".tmpl"))
        dest_file = dest / dest_rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        content = src_file.read_text(encoding="utf-8")
        dest_file.write_text(content.replace("{{LOOP_NAME}}", loop_name), encoding="utf-8")


