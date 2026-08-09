from __future__ import annotations

from bounded_loops.graph.application.runner_preflight import (
    ProbeOutcome,
    default_runner_profiles,
    preflight_runners,
)


def test_preflight_observes_a_fixed_candidate_without_admitting_it():
    profiles = default_runner_profiles()
    observed: list[tuple[str, ...]] = []

    report = preflight_runners(
        profiles,
        profile_id="codex",
        locate=lambda command: "/opt/tools/" + command,
        probe=lambda argv: observed.append(argv) or ProbeOutcome(0, "codex 1.2.3", "", False),
    )

    runner = report.runners[0]
    assert observed == [("/opt/tools/codex", "--version")]
    assert runner.available is True
    assert runner.version == "codex 1.2.3"
    assert runner.admission == "discovered"
    assert "auth" in runner.claims_not_proven


def test_preflight_never_makes_m4_or_an_unavailable_binary_routable():
    profiles = default_runner_profiles()

    m4 = preflight_runners(profiles, profile_id="m4-external-review")
    missing = preflight_runners(
        profiles,
        profile_id="muse",
        locate=lambda _command: None,
    )

    assert m4.runners[0].available is False
    assert m4.runners[0].admission == "denied"
    assert "GitHub-only" in (m4.runners[0].failure_reason or "")
    assert missing.runners[0].available is False
    assert missing.runners[0].admission == "discovered"
