"""
`--redact` — the operator surface for receipt redaction.

A separate module for the same reason `cli_preconditions` and `cli_retention` are:
`cli.py` sits against an 800-line ceiling that the repo's own layering test
enforces, and this is the third time that test has caught a feature pushing it
over. Keeping argparse wiring beside the policy it constructs is also the clearer
arrangement.

The policy itself lives in `adapters.io.receipt_redaction`, which does not import
argparse — a CLI concern must not leak into an adapter.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bounded_loops.adapters.io.receipt_redaction import RedactionMode, RedactionPolicy


def add_redact_argument(parser: argparse.ArgumentParser) -> None:
    """Add `--redact` to a parser that runs a loop."""
    parser.add_argument(
        "--redact",
        metavar="MODE",
        choices=[m.value for m in RedactionMode],
        default=RedactionMode.OFF.value,
        help=(
            "Redact receipts before they are written and hashed: 'off' (default, full "
            "audit record), 'paths' (rewrite absolute paths), 'strict' (also replace "
            "captured output with its digest). Cannot be applied retroactively — the "
            "fields are inside the hash chain."
        ),
    )


def policy_from_args(args: argparse.Namespace, *, workspace_root: Path) -> RedactionPolicy:
    """Build the policy a run should use.

    `getattr` rather than `args.redact`, because this is called from a code path
    shared with entry points that do not define the flag; a missing attribute must
    mean OFF rather than AttributeError.
    """
    return RedactionPolicy.from_mode(
        getattr(args, "redact", RedactionMode.OFF.value),
        workspace_root=workspace_root,
    )
