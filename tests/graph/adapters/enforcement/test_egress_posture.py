"""Egress posture — a first-class, configurable deployment policy (Slice 2 close-out).

Today `bl graph run --execute` hardcodes every `local_cli` connector node to
`NetworkMode.OPEN` (README.md / RELEASE-READINESS.md: "Making ALLOWLIST their
default tier is the later-phase item"). This module gives that later-phase wiring
something concrete to call: `EgressPosture` (OPEN / ALLOWLIST / BROKER), resolved
from explicit-arg > env var > config file > default-OPEN precedence, and
`decide_egress_posture` which turns a resolved posture + this host's real
`PlatformCapabilities` into an honest, fail-closed decision.

Non-negotiables under test:
  * OPEN is the default when nothing is configured, and never fails.
  * ALLOWLIST fails CLOSED (raises) when the OS cage (Seatbelt + egress proxy)
    is unavailable — it must NEVER silently fall back to OPEN.
  * BROKER never consults host capabilities (it is not a sandbox-network
    posture at all) and its decision is unambiguous about NOT applying a
    sandbox network envelope.
  * A present-but-invalid value at ANY precedence tier (bad env value, corrupt
    config file, malformed allowlist entry) raises — only a genuinely ABSENT
    tier falls through to the next one.
  * Allowlist enforcement is an exact (hostname, port) membership test: it
    admits exactly what is configured and denies everything else.
"""

from __future__ import annotations

import json

import pytest

from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities
from bounded_loops.graph.adapters.enforcement.egress_posture import (
    EgressPosture,
    EgressPostureConfig,
    EgressPostureDecision,
    decide_egress_posture,
    resolve_egress_posture,
)
from bounded_loops.graph.application.execution_policy import NetworkDestination, NetworkMode
from bounded_loops.graph.domain.errors import GraphValidationError

# ── capability fixtures (mirrors test_enforcer.py's `_caps` idiom) ──────────────

_NO_CAGE = PlatformCapabilities(
    platform="linux", docker_available=False, process_groups=True, rlimits=True,
)
_SEATBELT_NO_PROXY = PlatformCapabilities(
    platform="darwin", docker_available=False, process_groups=True, rlimits=True,
    seatbelt=True, egress_proxy=False,
)
_SEATBELT_WITH_PROXY = PlatformCapabilities(
    platform="darwin", docker_available=False, process_groups=True, rlimits=True,
    seatbelt=True, egress_proxy=True,
)


def _env(**over: str) -> dict[str, str]:
    """A hermetic environ mapping — never touches real os.environ or HOME."""
    return dict(over)


# ── A. posture precedence: explicit > env > config file > default OPEN ─────────


def test_default_posture_is_open_when_nothing_is_configured(tmp_path):
    config = resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")))
    assert config.posture is EgressPosture.OPEN
    assert config.allowlist == ()


def test_env_var_selects_posture_over_default(tmp_path):
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="broker",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.posture is EgressPosture.BROKER


def test_config_file_selects_posture_when_env_is_absent(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "allowlist", "allowlist": ["api.anthropic.com"]}))
    config = resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))
    assert config.posture is EgressPosture.ALLOWLIST
    assert config.allowlist == (NetworkDestination(hostname="api.anthropic.com", port=443),)


def test_explicit_argument_overrides_env_and_config_file(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "allowlist", "allowlist": ["api.anthropic.com"]}))
    config = resolve_egress_posture(
        EgressPosture.OPEN,
        environ=_env(BOUNDED_LOOPS_EGRESS_POSTURE="broker", BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)),
    )
    assert config.posture is EgressPosture.OPEN
    assert config.allowlist == ()  # OPEN carries no allowlist, even though the file had one


def test_env_overrides_config_file(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "open"}))
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="broker", BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path),
    ))
    assert config.posture is EgressPosture.BROKER


def test_empty_env_var_falls_through_to_config_file(tmp_path):
    # A shell that exports the var empty (e.g. `export BOUNDED_LOOPS_EGRESS_POSTURE=`) must
    # not be treated as "the operator explicitly asked for an unrecognized posture ''" — it
    # is indistinguishable from "not set" and must fall through, not raise.
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "broker"}))
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="", BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path),
    ))
    assert config.posture is EgressPosture.BROKER


def test_unrecognized_posture_value_from_env_raises_not_silently_open(tmp_path):
    with pytest.raises(GraphValidationError, match="unrecognized egress posture"):
        resolve_egress_posture(environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="yolo-open",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ))


def test_unrecognized_posture_value_from_config_file_raises(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "totally-open"}))
    with pytest.raises(GraphValidationError, match="unrecognized egress posture"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


# ── B. allowlist precedence + parsing ───────────────────────────────────────────


def test_env_allowlist_parsed_as_comma_separated_hosts_default_port_443(tmp_path):
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com, api.openai.com",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.allowlist == (
        NetworkDestination(hostname="api.anthropic.com", port=443),
        NetworkDestination(hostname="api.openai.com", port=443),
    )


def test_env_allowlist_supports_explicit_host_port(tmp_path):
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="internal.example.com:8443",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.allowlist == (NetworkDestination(hostname="internal.example.com", port=8443),)


def test_explicit_allowlist_overrides_env_and_config_file(tmp_path):
    config = resolve_egress_posture(
        EgressPosture.ALLOWLIST,
        explicit_allowlist=["explicit.example.com"],
        environ=_env(
            BOUNDED_LOOPS_EGRESS_ALLOWLIST="env.example.com",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ),
    )
    assert config.allowlist == (NetworkDestination(hostname="explicit.example.com", port=443),)


def test_malformed_allowlist_entry_raises(tmp_path):
    with pytest.raises(GraphValidationError, match="malformed allowlist entry"):
        resolve_egress_posture(environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
            BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.example.com:not-a-port",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ))


def test_malformed_allowlist_env_var_is_never_parsed_when_posture_is_not_allowlist(tmp_path):
    # CRIT regression: a stray/leftover BOUNDED_LOOPS_EGRESS_ALLOWLIST must not be parsed at all
    # (never even looked at) when the resolved posture is not ALLOWLIST — it must not be able to
    # fail an unrelated (open/broker, or no-local_cli) run at preflight.
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="open",
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="not::a:::valid,,,entry:::at:all",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.posture is EgressPosture.OPEN
    assert config.allowlist == ()


def test_malformed_allowlist_env_var_is_never_parsed_under_the_default_posture(tmp_path):
    # Same regression, default (unset) posture rather than an explicit "open".
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="not::a:::valid,,,entry:::at:all",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.posture is EgressPosture.OPEN
    assert config.allowlist == ()


def test_malformed_allowlist_env_var_still_raises_under_broker_posture(tmp_path):
    # BROKER also carries no allowlist concept — a stray value must not be parsed for it either.
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="broker",
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="not::a:::valid,,,entry:::at:all",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.posture is EgressPosture.BROKER
    assert config.allowlist == ()


def test_malformed_allowlist_env_var_still_raises_under_allowlist_posture(tmp_path):
    # The fix must only skip parsing when posture ISN'T allowlist — a genuinely malformed
    # value under allowlist posture (where it IS relevant) must still raise.
    with pytest.raises(GraphValidationError, match="malformed allowlist entry"):
        resolve_egress_posture(environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
            BOUNDED_LOOPS_EGRESS_ALLOWLIST="not::a:::valid,,,entry:::at:all",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ))


def test_ip_literal_allowlist_entry_raises(tmp_path):
    # NetworkDestination admits exact PUBLIC HOSTNAMES only; an IP literal must fail
    # closed here too, not be silently admitted as if it were a hostname.
    with pytest.raises(GraphValidationError):
        resolve_egress_posture(environ=_env(
            BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
            BOUNDED_LOOPS_EGRESS_ALLOWLIST="203.0.113.5",
            BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
        ))


def test_explicit_allowlist_rejects_a_bare_string_instead_of_a_sequence(tmp_path):
    # A bare `str` IS a `Sequence[str]` in Python — iterating it yields characters, not
    # hosts. A caller who passes "host.example.com" instead of ["host.example.com"] must
    # get a clear error, never a silent per-character iteration.
    with pytest.raises(GraphValidationError, match="not a bare string"):
        resolve_egress_posture(
            EgressPosture.ALLOWLIST,
            explicit_allowlist="host.example.com",  # type: ignore[arg-type]
            environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")),
        )


def test_non_allowlist_posture_with_allowlist_entries_raises(tmp_path):
    with pytest.raises(GraphValidationError, match="only allowlist posture"):
        resolve_egress_posture(
            EgressPosture.OPEN,
            explicit_allowlist=["api.anthropic.com"],
            environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")),
        )


def test_default_allowlist_is_empty(tmp_path):
    config = resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")))
    assert config.allowlist == ()


# ── C. config file: fail-closed on corruption, never silently "absent" ─────────


def test_missing_config_file_is_treated_as_absent(tmp_path):
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="broker",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "does-not-exist.json"),
    ))
    assert config.posture is EgressPosture.BROKER  # fell through cleanly to the env tier


def test_corrupt_json_config_file_raises_rather_than_falling_through(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text("{not valid json")
    with pytest.raises(GraphValidationError, match="not valid JSON"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


def test_config_file_that_is_not_a_json_object_raises(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps(["allowlist"]))
    with pytest.raises(GraphValidationError, match="JSON object"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


def test_config_file_with_unknown_key_raises(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"postrue": "allowlist"}))
    with pytest.raises(GraphValidationError, match="unknown"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


def test_symlinked_config_file_raises(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"posture": "open"}))
    link = tmp_path / "egress.json"
    link.symlink_to(real)
    with pytest.raises(GraphValidationError, match="symlink"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(link)))


def test_config_file_allowlist_field_must_be_a_list_of_strings(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "allowlist", "allowlist": "api.anthropic.com"}))
    with pytest.raises(GraphValidationError, match="list of strings"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


def test_config_file_posture_field_must_be_a_string(tmp_path):
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": 1}))
    with pytest.raises(GraphValidationError, match="'posture' field must be a string"):
        resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))


def test_unreadable_config_file_raises_rather_than_falling_through(tmp_path):
    # A permission-denied EXISTING file must fail closed too — an attacker who can only
    # revoke read access (without deleting the file) must not be able to force resolution
    # to silently fall through to a lower-precedence tier or the OPEN default.
    config_path = tmp_path / "egress.json"
    config_path.write_text(json.dumps({"posture": "allowlist"}))
    config_path.chmod(0o000)
    try:
        with pytest.raises(GraphValidationError, match="could not be read"):
            resolve_egress_posture(environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(config_path)))
    finally:
        config_path.chmod(0o600)  # restore so pytest's tmp_path cleanup can remove it


def test_explicit_posture_argument_rejects_a_non_egress_posture_value(tmp_path):
    with pytest.raises(GraphValidationError, match="explicit posture must be an EgressPosture"):
        resolve_egress_posture(
            "open",  # type: ignore[arg-type]
            environ=_env(BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json")),
        )


def test_env_allowlist_skips_blank_entries_from_a_trailing_comma(tmp_path):
    config = resolve_egress_posture(environ=_env(
        BOUNDED_LOOPS_EGRESS_POSTURE="allowlist",
        BOUNDED_LOOPS_EGRESS_ALLOWLIST="api.anthropic.com,,  ,api.openai.com,",
        BOUNDED_LOOPS_EGRESS_CONFIG=str(tmp_path / "absent.json"),
    ))
    assert config.allowlist == (
        NetworkDestination(hostname="api.anthropic.com", port=443),
        NetworkDestination(hostname="api.openai.com", port=443),
    )


# ── D. decide_egress_posture — OPEN ─────────────────────────────────────────────


def test_open_posture_never_fails_regardless_of_capabilities():
    config = EgressPostureConfig(posture=EgressPosture.OPEN)
    decision = decide_egress_posture(config, capabilities=_NO_CAGE)
    assert isinstance(decision, EgressPostureDecision)
    assert decision.network_mode is NetworkMode.OPEN
    assert decision.network_destinations == ()
    assert decision.requires_broker is False


def test_open_decision_defaults_to_probing_the_real_platform_when_capabilities_omitted():
    # Mirrors build_enforcer()'s "probe unless injected" convention — must not require
    # every caller to probe the platform themselves.
    config = EgressPostureConfig(posture=EgressPosture.OPEN)
    decision = decide_egress_posture(config)
    assert decision.network_mode is NetworkMode.OPEN


# ── E. decide_egress_posture — ALLOWLIST fails closed without the OS cage ───────


def test_allowlist_fails_closed_without_seatbelt_or_bwrap_at_all():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    with pytest.raises(GraphValidationError, match="cannot deliver"):
        decide_egress_posture(config, capabilities=_NO_CAGE)


def test_allowlist_fails_closed_with_seatbelt_but_no_egress_proxy():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    with pytest.raises(GraphValidationError, match="cannot deliver"):
        decide_egress_posture(config, capabilities=_SEATBELT_NO_PROXY)


def test_allowlist_never_silently_falls_back_to_open_when_it_fails():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    try:
        decide_egress_posture(config, capabilities=_NO_CAGE)
        pytest.fail("expected GraphValidationError, got a decision instead")
    except GraphValidationError as exc:
        assert "OPEN" in str(exc)  # the refusal message names the danger it is refusing


def test_allowlist_succeeds_with_seatbelt_and_egress_proxy_available():
    dest = (NetworkDestination(hostname="api.anthropic.com", port=443),)
    config = EgressPostureConfig(posture=EgressPosture.ALLOWLIST, allowlist=dest)
    decision = decide_egress_posture(config, capabilities=_SEATBELT_WITH_PROXY)
    assert decision.network_mode is NetworkMode.ALLOWLIST
    assert decision.network_destinations == dest
    assert decision.requires_broker is False


# ── F. decide_egress_posture — BROKER routing ───────────────────────────────────


def test_broker_posture_never_consults_capabilities():
    config = EgressPostureConfig(posture=EgressPosture.BROKER)
    decision = decide_egress_posture(config, capabilities=_NO_CAGE)  # would fail ALLOWLIST; must not affect BROKER
    assert decision.network_mode is None
    assert decision.requires_broker is True


def test_broker_decision_flags_requires_broker_and_has_no_network_mode():
    config = EgressPostureConfig(posture=EgressPosture.BROKER)
    decision = decide_egress_posture(config, capabilities=_SEATBELT_WITH_PROXY)
    assert decision.posture is EgressPosture.BROKER
    assert decision.network_mode is None
    assert decision.network_destinations == ()
    assert decision.requires_broker is True
    assert "EgressBroker" in decision.rationale


# ── G. allowlist host enforcement (admit listed, deny unlisted) ────────────────


def test_allowlist_admits_a_listed_destination():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    assert config.allowlist_admits("api.anthropic.com", 443) is True


def test_allowlist_denies_an_unlisted_destination():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    assert config.allowlist_admits("evil.example.com", 443) is False


def test_allowlist_denies_the_right_host_on_the_wrong_port():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    assert config.allowlist_admits("api.anthropic.com", 8443) is False


def test_allowlist_admits_is_case_insensitive_on_hostname():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    assert config.allowlist_admits("API.Anthropic.COM", 443) is True


def test_open_posture_config_never_admits_via_allowlist_check():
    # OPEN has no allowlist concept — the network is unrestricted, so asking "is X on the
    # allowlist" is a different question than "is X reachable"; it must not report True.
    config = EgressPostureConfig(posture=EgressPosture.OPEN)
    assert config.allowlist_admits("anything.example.com", 443) is False


def test_broker_posture_config_never_admits_via_allowlist_check():
    config = EgressPostureConfig(posture=EgressPosture.BROKER)
    assert config.allowlist_admits("anything.example.com", 443) is False


def test_allowlist_admits_returns_false_for_an_unconstructible_destination():
    config = EgressPostureConfig(
        posture=EgressPosture.ALLOWLIST, allowlist=(NetworkDestination(hostname="api.anthropic.com", port=443),),
    )
    assert config.allowlist_admits("203.0.113.5", 443) is False  # IP literal: fails closed, not raises


def test_empty_allowlist_denies_every_destination():
    # A configured ALLOWLIST posture with no hosts yet is the SAFE (maximally-restrictive)
    # direction — it must deny everything, not be treated as "no restriction."
    config = EgressPostureConfig(posture=EgressPosture.ALLOWLIST)
    assert config.allowlist_admits("api.anthropic.com", 443) is False


# ── config object invariants ────────────────────────────────────────────────────


def test_config_rejects_duplicate_allowlist_destinations():
    dest = NetworkDestination(hostname="api.anthropic.com", port=443)
    with pytest.raises(GraphValidationError, match="unique"):
        EgressPostureConfig(posture=EgressPosture.ALLOWLIST, allowlist=(dest, dest))


def test_config_rejects_a_non_egress_posture_value():
    with pytest.raises(GraphValidationError):
        EgressPostureConfig(posture="open")  # type: ignore[arg-type]


def test_decide_rejects_a_non_egress_posture_config():
    with pytest.raises(GraphValidationError):
        decide_egress_posture("not-a-config", capabilities=_NO_CAGE)  # type: ignore[arg-type]
