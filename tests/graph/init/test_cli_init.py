"""RED-first tests for `bl graph init` — the CLI wiring + orchestration (Slice 4).

Covers argument registration, the non-interactive (`--flags`) path, the full
interactive wizard (stdin injected via `input_fn`, never real I/O), the
existing-config overwrite gate, symlink refusal, EOFError/KeyboardInterrupt
handling, and — the non-negotiable — that every written file round-trips
cleanly through `egress_posture.resolve_egress_posture`, the exact fail-closed
reader `bl graph run` consumes. `tests/conftest.py`'s autouse fixture already
redirects HOME to a per-test tmp dir; every test here additionally passes
`--config` (or monkeypatches `BOUNDED_LOOPS_EGRESS_CONFIG`) so it is never
ambiguous which file is under test, per this slice's explicit test-isolation
requirement.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest

from bounded_loops.graph.adapters.enforcement.egress_posture import EgressPosture, resolve_egress_posture
from bounded_loops.graph.cli_graph import register
from bounded_loops.graph.init.cli_init import cmd_graph_init
from bounded_loops.graph.init.config_writer import default_config_path


def _ns(**kw: object) -> argparse.Namespace:
    kw.setdefault("posture", None)
    kw.setdefault("allowlist", None)
    kw.setdefault("connector", None)
    kw.setdefault("yes", False)
    kw.setdefault("config", None)
    return argparse.Namespace(**kw)


def _stub(*answers: str):
    it = iter(answers)
    return lambda _prompt: next(it)


# ── register() wires the init subparser ─────────────────────────────────────────


def test_register_wires_init_subcommand_with_documented_defaults() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args(["graph", "init"])
    assert args.cmd == "graph"
    assert args.graph_cmd == "init"
    assert args.posture is None
    assert args.allowlist is None
    assert args.connector is None
    assert args.yes is False
    assert args.config is None
    assert args.func is cmd_graph_init


def test_register_init_accepts_all_documented_flags() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    args = parser.parse_args([
        "graph", "init",
        "--posture", "allowlist",
        "--allowlist", "api.anthropic.com",
        "--allowlist", "internal.example.com:8443",
        "--connector", "byok",
        "--yes",
        "--config", "/tmp/custom-egress.json",
    ])
    assert args.posture == "allowlist"
    assert args.allowlist == ["api.anthropic.com", "internal.example.com:8443"]
    assert args.connector == "byok"
    assert args.yes is True
    assert args.config == "/tmp/custom-egress.json"


def test_register_init_rejects_an_unrecognized_posture_choice() -> None:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="cmd")
    register(subs)
    with pytest.raises(SystemExit):
        parser.parse_args(["graph", "init", "--posture", "yolo-open"])


# ── non-interactive: the zero-friction default path ────────────────────────────


def test_non_interactive_yes_with_no_other_flags_writes_open_and_local_cli(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}
    out = capsys.readouterr().out
    assert "posture=open" in out


def test_non_interactive_default_writes_file_mode_0600_and_dir_mode_0700(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_non_interactive_default_round_trips_through_the_real_reader(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0
    resolved = resolve_egress_posture(environ={"BOUNDED_LOOPS_EGRESS_CONFIG": str(target)})
    assert resolved.posture is EgressPosture.OPEN
    assert resolved.allowlist == ()


# ── non-interactive: allowlist posture ──────────────────────────────────────────


def test_non_interactive_allowlist_posture_collects_and_writes_hosts(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(
        posture="allowlist", allowlist=["api.anthropic.com", "internal.example.com:8443"],
        yes=True, config=str(target),
    ))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["posture"] == "allowlist"
    assert sorted(written["allowlist"]) == sorted(["api.anthropic.com", "internal.example.com:8443"])
    resolved = resolve_egress_posture(environ={"BOUNDED_LOOPS_EGRESS_CONFIG": str(target)})
    assert resolved.allowlist_admits("api.anthropic.com", 443) is True
    assert resolved.allowlist_admits("internal.example.com", 8443) is True


def test_non_interactive_allowlist_posture_supports_repeated_flag_and_commas(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(
        posture="allowlist", allowlist=["api.anthropic.com,internal.example.com:8443"],
        yes=True, config=str(target),
    ))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(written["allowlist"]) == sorted(["api.anthropic.com", "internal.example.com:8443"])


def test_non_interactive_allowlist_posture_with_no_hosts_warns_and_writes_empty(
    tmp_path: Path, capsys,
) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(posture="allowlist", yes=True, config=str(target)))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == {"posture": "allowlist", "allowlist": []}
    out = capsys.readouterr().out
    assert "warning" in out.lower()
    assert "denies all" in out.lower() or "deny" in out.lower()
    resolved = resolve_egress_posture(environ={"BOUNDED_LOOPS_EGRESS_CONFIG": str(target)})
    assert resolved.allowlist_admits("anything.example.com", 443) is False


def test_non_interactive_allowlist_flag_without_matching_posture_is_rejected(tmp_path: Path, capsys) -> None:
    # Contradiction: hosts were given, but posture resolves (default) to 'open'.
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(allowlist=["api.anthropic.com"], yes=True, config=str(target)))
    assert rc == 2
    assert not target.exists()
    assert "allowlist" in capsys.readouterr().err.lower()


def test_non_interactive_invalid_allowlist_host_is_rejected_cleanly(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(
        posture="allowlist", allowlist=["203.0.113.5"], yes=True, config=str(target),
    ))
    assert rc == 2
    assert not target.exists()
    assert "invalid allowlist entry" in capsys.readouterr().err.lower()


# ── non-interactive: connector mode (BYOK writes no secret) ────────────────────


def test_non_interactive_byok_connector_prints_pointer_and_writes_no_connector_file(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_TEST_SECRET_TOKEN", "sk-should-never-appear-anywhere")
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(connector="byok", yes=True, config=str(target)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "BYOK" in out
    assert "--admitted" in out
    # egress.json is the ONLY file this command may create, anywhere under tmp_path.
    created_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created_files == [target]
    # ...and it must never contain the secret, or any connector/credential material.
    assert "sk-should-never-appear-anywhere" not in target.read_text(encoding="utf-8")
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


def test_non_interactive_local_cli_connector_is_the_default(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0  # connector defaults to local_cli; no --connector needed for zero friction


def test_written_confirmation_states_that_connector_mode_is_not_stored(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(connector="byok", yes=True, config=str(target)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Connector mode is not stored" in out


# ── M1 (Grok, live-proven): the CLI's write must force 0600 on overwrite too ────


def test_cli_forces_0600_even_when_overwriting_a_looser_existing_mode(tmp_path: Path) -> None:
    # Grok's exact repro, driven through the real `bl graph init` command rather
    # than the config_writer function directly: pre-create at 0o666 (the old
    # in-place O_TRUNC write kept this mode after "overwriting"; content updated,
    # mode silently unchanged), then overwrite via the installer.
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    target.chmod(0o666)
    assert stat.S_IMODE(target.stat().st_mode) == 0o666  # sanity: the setup actually took

    rc = cmd_graph_init(_ns(posture="open", yes=True, config=str(target)))

    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ── existing-config overwrite gate ──────────────────────────────────────────────


def test_non_interactive_existing_config_without_yes_is_refused(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    rc = cmd_graph_init(_ns(posture="open", config=str(target)))
    assert rc == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "broker"}  # untouched
    err = capsys.readouterr().err
    assert "--yes" in err


def test_non_interactive_existing_config_with_yes_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    rc = cmd_graph_init(_ns(posture="open", yes=True, config=str(target)))
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


def test_interactive_existing_config_shown_before_the_overwrite_prompt(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("n"))
    assert rc == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "broker"}  # untouched
    out = capsys.readouterr().out
    assert "broker" in out  # the existing config's content was actually shown


def test_interactive_existing_config_overwritten_after_explicit_yes(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    target.write_text(json.dumps({"posture": "broker"}), encoding="utf-8")
    # overwrite? y -> connector [blank=local_cli] -> posture [blank=open] -> final confirm [blank=yes]
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("y", "", "", ""))
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


# ── symlink at the config path: always refused ──────────────────────────────────


def test_symlinked_config_path_is_refused_even_with_yes(tmp_path: Path, capsys) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"posture": "open"}), encoding="utf-8")
    link = tmp_path / "egress.json"
    link.symlink_to(real)
    rc = cmd_graph_init(_ns(posture="broker", yes=True, config=str(link)))
    assert rc == 2
    assert "symlink" in capsys.readouterr().err.lower()
    assert json.loads(real.read_text(encoding="utf-8")) == {"posture": "open"}  # target untouched


def test_symlinked_config_path_is_refused_interactively_too(tmp_path: Path, capsys) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"posture": "open"}), encoding="utf-8")
    link = tmp_path / "egress.json"
    link.symlink_to(real)
    rc = cmd_graph_init(_ns(config=str(link)), input_fn=_stub())  # no prompt should even be reached
    assert rc == 2
    assert "symlink" in capsys.readouterr().err.lower()


# ── full interactive wizard ──────────────────────────────────────────────────────


def test_interactive_wizard_all_blank_answers_writes_the_zero_friction_default(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    # connector [blank] -> local_cli, posture [blank] -> open, final confirm [blank] -> yes
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("", "", ""))
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"posture": "open"}


def test_interactive_wizard_choosing_allowlist_prompts_for_hosts(tmp_path: Path) -> None:
    target = tmp_path / "egress.json"
    # connector [blank] -> local_cli, posture=2 (allowlist), hosts, final confirm [blank] -> yes
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("", "2", "api.anthropic.com", ""))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written == {"posture": "allowlist", "allowlist": ["api.anthropic.com"]}


def test_interactive_wizard_final_confirmation_no_aborts_without_writing(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("", "", "n"))
    assert rc == 1
    assert not target.exists()
    assert "aborted" in capsys.readouterr().out.lower()


def test_interactive_wizard_shows_a_summary_before_the_final_confirmation(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    cmd_graph_init(_ns(config=str(target)), input_fn=_stub("", "", "y"))
    out = capsys.readouterr().out
    assert "open" in out
    assert "local_cli" in out


def test_interactive_wizard_byok_connector_prints_pointer(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"
    # connector=2 (byok), posture [blank] -> open, final confirm [blank] -> yes
    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_stub("2", "", ""))
    assert rc == 0
    out = capsys.readouterr().out
    assert "BYOK" in out
    assert "--admitted" in out


# ── EOFError / KeyboardInterrupt during a prompt ────────────────────────────────


def test_eof_during_an_interactive_prompt_exits_cleanly_without_a_traceback(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"

    def _eof(_prompt: str) -> str:
        raise EOFError

    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_eof)
    assert rc == 2
    assert not target.exists()
    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "--yes" in err or "non-interactive" in err.lower()


def test_keyboard_interrupt_during_an_interactive_prompt_aborts_cleanly(tmp_path: Path, capsys) -> None:
    target = tmp_path / "egress.json"

    def _interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    rc = cmd_graph_init(_ns(config=str(target)), input_fn=_interrupt)
    assert rc == 1
    assert not target.exists()
    assert "aborted" in capsys.readouterr().out.lower()


# ── environ isolation at the full CLI level (CRIT-derived) ──────────────────────


def test_written_config_and_its_verification_are_never_shadowed_by_ambient_env_vars(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOUNDED_LOOPS_EGRESS_POSTURE", "open")
    monkeypatch.setenv("BOUNDED_LOOPS_EGRESS_ALLOWLIST", "should-never-be-admitted.example.com")
    target = tmp_path / "egress.json"
    rc = cmd_graph_init(_ns(
        posture="allowlist", allowlist=["api.anthropic.com"], yes=True, config=str(target),
    ))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["posture"] == "allowlist"
    assert written["allowlist"] == ["api.anthropic.com"]
    out = capsys.readouterr().out
    assert "posture=allowlist" in out
    # the printed verification note about ambient overrides must fire (honesty contract)
    assert "BOUNDED_LOOPS_EGRESS_POSTURE" in out


# ── --config vs BOUNDED_LOOPS_EGRESS_CONFIG vs the true default path ───────────


def test_config_flag_writes_to_the_exact_custom_path(tmp_path: Path) -> None:
    target = tmp_path / "somewhere" / "custom-egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0
    assert target.exists()


def test_env_var_is_honored_when_no_config_flag_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "from-env" / "egress.json"
    monkeypatch.setenv("BOUNDED_LOOPS_EGRESS_CONFIG", str(target))
    rc = cmd_graph_init(_ns(yes=True))
    assert rc == 0
    assert target.exists()


def test_true_default_path_is_used_when_no_flag_and_no_env_var_are_given(tmp_path: Path) -> None:
    # HOME is redirected to a per-test tmp dir by the autouse fixture in tests/conftest.py.
    rc = cmd_graph_init(_ns(yes=True))
    assert rc == 0
    assert default_config_path().exists()
    assert default_config_path().is_relative_to(tmp_path)


def test_non_default_config_path_prints_an_honesty_note(tmp_path: Path, capsys) -> None:
    target = tmp_path / "custom" / "egress.json"
    rc = cmd_graph_init(_ns(yes=True, config=str(target)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "NON-DEFAULT" in out
    assert "BOUNDED_LOOPS_EGRESS_CONFIG" in out
