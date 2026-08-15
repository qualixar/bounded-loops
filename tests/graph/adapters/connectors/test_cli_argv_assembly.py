"""How a CLI launch is assembled — pinned statically, for every shipped profile.

**No subscription, no login, no subprocess, no network.** `build_cli_argv` is pure, so these run
identically on a laptop with five agent CLIs installed and on a CI box with none. That is
deliberate: a provider test that needs the tester's own accounts passes only on the machine that
wrote it, and tells later readers nothing about whether the product works.

The rule under test is one line of ordering, and violating it does not degrade gracefully — it
breaks a provider outright:

    grok -p <PROMPT> --output-format json     works
    grok -p --output-format json <PROMPT>     "a value is required for '--single <PROMPT>'"

`-p` is an alias for `--single <PROMPT>`, so a flag placed between it and its value is consumed AS
the value. The prompt then arrives as a stray positional and the CLI exits 2 having done nothing.

That rule lived in a comment and nothing checked it. The existing `_caged_argv` tests hand in a
pre-built `inner_argv`, so the assembly itself had no coverage at all — and it was rebuilt in the
wrong order during an audit probe, reproducing the exact Grok failure by hand before anyone noticed
the guard was missing.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliProfile,
    build_cli_argv,
)

_PROMPT = "write the thing"

# There is deliberately NO global "these flags take a value" table here, and the absence is the
# point. An earlier version of this file carried one containing `-p`, which is correct for grok
# (`-p` aliases `--single <PROMPT>`) and wrong for claude (`-p` aliases `--print`, a boolean). It
# failed `claude -p --output-format json` — an invocation verified working against the real binary.
# The same flag spelling means different things per CLI, so any cross-profile rule about flag
# ARITY is guesswork dressed as a check. The rule below is about POSITION instead, which is a
# property of our own assembly and is knowable without modelling five CLIs' grammars.


@pytest.mark.parametrize("name", sorted(CLI_PROFILES))
def test_usage_args_come_after_the_prompt_for_every_shipped_profile(name: str) -> None:
    """The ordering rule, checked against each profile we actually ship rather than a fixture.

    Parametrised over `CLI_PROFILES` so a sixth provider added tomorrow is covered the moment it is
    added, with no test edit. A catalog-loaded provider gets the same guarantee: it becomes a
    `CliProfile` and flows through the same function.
    """
    profile = CLI_PROFILES[name]
    argv, stdin_text = build_cli_argv(profile, _PROMPT, binary=f"/usr/local/bin/{profile.binary}")

    if not profile.usage_args:
        pytest.skip(f"{name} declares no usage_args — unmetered by design, nothing to order")

    for flag in profile.usage_args:
        assert flag in argv, f"{name}: usage flag {flag!r} did not reach the argv at all"

    first_usage = min(argv.index(flag) for flag in profile.usage_args)
    if profile.prompt_via == "arg":
        assert argv.index(_PROMPT) < first_usage, (
            f"{name}: usage args precede the prompt. If any preceding flag takes a value, it will "
            f"swallow one — this is the exact shape that breaks grok. argv={argv}"
        )
    else:
        assert stdin_text == _PROMPT
        assert _PROMPT not in argv, f"{name}: a stdin prompt must never also appear in argv"


@pytest.mark.parametrize("name", sorted(CLI_PROFILES))
def test_an_arg_prompt_lands_immediately_after_the_profiles_own_args(name: str) -> None:
    """The positional invariant, which is what actually protects every provider.

    `profile.args` is the only place a value-taking flag can appear, because we author it. So the
    prompt must sit at exactly `len(args) + 1`: directly after the last authored flag, before
    anything else is appended. Then a trailing `--single`-style flag receives the prompt as its
    value — correct — and no usage flag can ever be interposed.

    Stated positionally rather than as a claim about which flags take values, because that varies
    per CLI and is not ours to model. See the note at the top of this file.
    """
    profile = CLI_PROFILES[name]
    if profile.prompt_via != "arg":
        pytest.skip(f"{name} passes its prompt on stdin — covered by the stdin test below")

    argv, _ = build_cli_argv(profile, _PROMPT, binary=f"/usr/local/bin/{profile.binary}")

    expected_index = 1 + len(profile.args)  # argv[0] is the binary
    assert argv[expected_index] == _PROMPT, (
        f"{name}: the prompt sits at index {argv.index(_PROMPT)}, expected {expected_index} "
        f"(immediately after profile.args). Anything between the last authored flag and the prompt "
        f"can be consumed as that flag's value. argv={argv}"
    )


def test_the_ordering_guard_actually_catches_the_broken_order() -> None:
    """Proof the two tests above can fail — built from the real grok profile, mis-assembled.

    Without this, both could be passing vacuously (e.g. if `usage_args` were empty everywhere) and
    nobody would know until a provider broke in the field, which is how this was found the first
    time.
    """
    grok = CLI_PROFILES["grok"]
    assert grok.usage_args, "this proof depends on grok declaring usage args"

    # The mis-assembly: usage_args immediately after args, prompt last.
    broken = ["/usr/local/bin/grok", *grok.args, *grok.usage_args, _PROMPT]
    correct, _ = build_cli_argv(grok, _PROMPT, binary="/usr/local/bin/grok")

    assert broken != correct, "the mis-assembly must differ from what we ship"

    # Both invariants the tests above assert must be violated by the broken order — otherwise those
    # tests could pass on a mis-assembled argv and would be guarding nothing.
    first_usage = min(broken.index(flag) for flag in grok.usage_args)
    assert broken.index(_PROMPT) > first_usage, "broken order should put the prompt after usage args"
    assert broken[1 + len(grok.args)] != _PROMPT, "broken order should displace the prompt"

    # And the shipped order must satisfy both.
    assert correct[1 + len(grok.args)] == _PROMPT
    assert correct.index(_PROMPT) < min(correct.index(flag) for flag in grok.usage_args)


def test_a_stdin_profile_keeps_the_prompt_out_of_the_process_table() -> None:
    """Why `prompt_via` exists at all, stated as a test rather than left as a convention.

    argv is world-readable via `ps` on a shared host. A prompt can carry the contents of a work
    product, so a profile that declares stdin must actually use it.
    """
    profile = CliProfile("thing", ("--go",), prompt_via="stdin", usage_args=("--json",))

    argv, stdin_text = build_cli_argv(profile, "confidential draft text", binary="/bin/thing")

    assert stdin_text == "confidential draft text"
    assert "confidential draft text" not in argv
    assert argv == ["/bin/thing", "--go", "--json"]


def test_the_resolved_binary_path_is_used_not_the_bare_name() -> None:
    """`_launch` resolves the binary with `shutil.which` before calling this, and the resolved path
    must be what runs — resolving and then launching the bare name would re-resolve against the
    child's PATH, which the sandbox rewrites."""
    profile = CLI_PROFILES["claude"]

    argv, _ = build_cli_argv(profile, _PROMPT, binary="/opt/homebrew/bin/claude")

    assert argv[0] == "/opt/homebrew/bin/claude"
