"""Interactive egress-posture / allowlist / confirmation prompts for `bl graph init`.

Every prompt takes an injectable ``input_fn`` (default the builtin ``input``) —
mirrors this project's own "probe unless injected" convention
(``capabilities.probe_platform`` / ``decide_egress_posture``) so production code
reads real stdin for free while tests inject a deterministic stub, never real
interactive I/O. Every gate loops on an unrecognized answer rather than guessing
at the user's intent — an ambiguous answer is a re-prompt, never a silent guess.
"""

from __future__ import annotations

from typing import Callable

from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture
from bounded_loops.graph.init.config_writer import canonicalize_allowlist_entries
from bounded_loops.graph.init.errors import GraphInitError

_POSTURE_PROMPT_TEXT = (
    "Egress posture — controls what network access your connector CLI gets:\n"
    "  [1] open       (DEFAULT) — your CLI runs free — recommended\n"
    "  [2] allowlist  — lockdown: restrict where your CLI can connect — advanced, "
    "may block egress you don't list\n"
    "  [3] broker     — BYOK-mediated egress; no direct network from this process\n"
)

_POSTURE_ALIASES: dict[str, EgressPosture] = {
    "1": EgressPosture.OPEN,
    "open": EgressPosture.OPEN,
    "2": EgressPosture.ALLOWLIST,
    "allowlist": EgressPosture.ALLOWLIST,
    "3": EgressPosture.BROKER,
    "broker": EgressPosture.BROKER,
}

_ALLOWLIST_WARNING_TEXT = (
    "ALLOWLIST posture restricts your CLI to EXACTLY the hosts you list below — any\n"
    "host not listed will be BLOCKED. This may block egress you don't list; add every\n"
    "host your connector CLI needs to reach.\n"
)


def prompt_egress_posture(
    *,
    input_fn: Callable[[str], str] = input,
    default: EgressPosture = EgressPosture.OPEN,
) -> EgressPosture:
    """Prompt for the egress posture. Blank input accepts *default* (OPEN unless
    a caller deliberately overrides it) — the zero-friction, recommended path."""
    print(_POSTURE_PROMPT_TEXT, end="")
    while True:
        raw = input_fn(f"Choose 1-3, or type the name [{default.value}]: ").strip().lower()
        if raw == "":
            return default
        choice = _POSTURE_ALIASES.get(raw)
        if choice is not None:
            return choice
        print("Please enter 1, 2, 3, open, allowlist, or broker.")


def prompt_allowlist_hosts(*, input_fn: Callable[[str], str] = input) -> tuple[str, ...]:
    """Prompt for allowlist hosts (only reached once ALLOWLIST posture is chosen).
    Shows the "may block egress you don't list" warning first, then loops until
    every comma-separated entry canonicalizes cleanly (or the line is left blank,
    which is accepted as a valid — if maximally restrictive — empty allowlist)."""
    print(_ALLOWLIST_WARNING_TEXT, end="")
    while True:
        raw = input_fn("Enter allowlist hosts, comma-separated (host or host:port). Leave blank for none: ")
        candidates = [host.strip() for host in raw.split(",") if host.strip()]
        if not candidates:
            return ()
        try:
            return canonicalize_allowlist_entries(candidates)
        except GraphInitError as exc:
            print(f"  {exc} — try again.")


def _confirm(prompt_text: str, *, default: bool, input_fn: Callable[[str], str]) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input_fn(f"{prompt_text} {suffix}: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def confirm_overwrite(*, input_fn: Callable[[str], str] = input) -> bool:
    """Default NO — an existing config is never silently clobbered; a garbled or
    blank answer means "do not overwrite", never a guessed "yes"."""
    return _confirm("Overwrite it?", default=False, input_fn=input_fn)


def confirm_write(*, input_fn: Callable[[str], str] = input) -> bool:
    """Default YES — the wizard's final step, after the user already walked
    through every prior prompt; blank accepts the summary just shown."""
    return _confirm("Write this configuration?", default=True, input_fn=input_fn)
