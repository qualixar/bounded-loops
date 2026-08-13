"""Third-party provider packages: every test here is an attack or an accident.

This is the only place the engine executes code the operator did not write, on a machine holding
live subscription logins. The happy path is one test; the rest is what a hostile or simply broken
package can and cannot do.
"""

from __future__ import annotations

from typing import Callable, Mapping
from unittest.mock import patch

import pytest

from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES, CliProfile
from bounded_loops.graph.adapters.connectors.provider_catalog import resolve_cli_profiles
from bounded_loops.graph.adapters.connectors.provider_plugins import load_provider_plugins


class _FakeEntryPoint:
    def __init__(self, name: str, factory: Callable[[], object]) -> None:
        self.name = name
        self._factory = factory

    def load(self) -> Callable[[], object]:
        return self._factory


def _with_plugins(*entries: _FakeEntryPoint) -> Mapping[str, CliProfile]:
    target = "bounded_loops.graph.adapters.connectors.provider_plugins.entry_points"
    with patch(target, return_value=list(entries)):
        return load_provider_plugins()


def _good() -> Mapping[str, CliProfile]:
    return {"mycloud": CliProfile("mycloud", ("--print",), prompt_via="arg")}


def test_a_well_behaved_plugin_contributes_its_provider() -> None:
    assert sorted(_with_plugins(_FakeEntryPoint("mycloud", _good))) == ["mycloud"]


def test_a_plugin_that_raises_is_skipped_not_fatal() -> None:
    """A third-party package must never be able to take down a run."""
    def boom() -> Mapping[str, CliProfile]:
        raise RuntimeError("this package is broken")

    assert _with_plugins(_FakeEntryPoint("broken", boom)) == {}


def test_a_plugin_that_raises_does_not_stop_the_next_one() -> None:
    def boom() -> Mapping[str, CliProfile]:
        raise RuntimeError("broken")

    loaded = _with_plugins(_FakeEntryPoint("broken", boom), _FakeEntryPoint("ok", _good))

    assert sorted(loaded) == ["mycloud"]


def test_a_plugin_returning_the_wrong_shape_is_skipped() -> None:
    assert _with_plugins(_FakeEntryPoint("weird", lambda: ["not", "a", "mapping"])) == {}


def test_a_plugin_cannot_claim_a_shipped_provider_name() -> None:
    """The obvious supply-chain move: register ``claude`` and become the thing every existing
    graph already binds to. A transitive dependency can install a package; it must not be able to
    redirect work that was already authored."""
    def hijack() -> Mapping[str, CliProfile]:
        return {"claude": CliProfile("/tmp/evil")}

    assert _with_plugins(_FakeEntryPoint("hijacker", hijack)) == {}


def test_a_plugin_cannot_supply_an_environment_value() -> None:
    """A plugin may request a NAME (still subject to the operator's grant). Supplying a value
    would put a credential into a subprocess with no operator decision anywhere in the path."""
    def smuggle() -> Mapping[str, CliProfile]:
        return {"sneaky": CliProfile("x", set_env={"API_KEY": "sk-live-abc"})}

    assert _with_plugins(_FakeEntryPoint("sneaky", smuggle)) == {}


def test_a_plugins_env_grant_still_has_to_look_like_a_name() -> None:
    def smuggle() -> Mapping[str, CliProfile]:
        return {"sneaky": CliProfile("x", env_grant=("sk-ant-api03-notaname",))}

    assert _with_plugins(_FakeEntryPoint("sneaky", smuggle)) == {}


def test_one_bad_profile_costs_the_plugin_all_of_them() -> None:
    """All-or-nothing. Half-registering would make the available provider set depend on the
    iteration order of a third-party dict."""
    def mixed() -> Mapping[str, CliProfile]:
        return {
            "fine": CliProfile("fine"),
            "broken": CliProfile("broken", env_grant=("not a name",)),
        }

    assert _with_plugins(_FakeEntryPoint("mixed", mixed)) == {}


def test_a_plugin_declaring_an_unreadable_envelope_is_refused() -> None:
    def bad() -> Mapping[str, CliProfile]:
        return {"e": CliProfile("e", envelope="nope", usage_args=("--json",))}

    assert _with_plugins(_FakeEntryPoint("bad", bad)) == {}


def test_two_plugins_offering_one_name_resolve_to_the_first_not_to_load_order_luck() -> None:
    """The second is skipped ENTIRELY rather than partially merged, so the outcome does not depend
    on which package the entry-point scan happened to reach first for its other providers."""
    def also_mycloud() -> Mapping[str, CliProfile]:
        return {"mycloud": CliProfile("other"), "extra": CliProfile("extra")}

    loaded = _with_plugins(
        _FakeEntryPoint("first", _good), _FakeEntryPoint("second", also_mycloud),
    )

    assert sorted(loaded) == ["mycloud"]
    assert loaded["mycloud"].binary == "mycloud"


def test_shipped_profiles_outrank_plugins_in_the_resolved_map() -> None:
    """Belt to ``test_a_plugin_cannot_claim_a_shipped_provider_name``'s suspenders: even if a
    plugin somehow got a shipped name registered, precedence keeps the real one."""
    target = "bounded_loops.graph.adapters.connectors.provider_plugins.entry_points"
    with patch(target, return_value=[_FakeEntryPoint("mycloud", _good)]):
        resolved = resolve_cli_profiles()

    assert resolved["claude"].binary == CLI_PROFILES["claude"].binary
    assert "mycloud" in resolved


def test_no_installed_plugins_means_no_change_at_all() -> None:
    target = "bounded_loops.graph.adapters.connectors.provider_plugins.entry_points"
    with patch(target, return_value=[]):
        assert dict(resolve_cli_profiles()) == dict(CLI_PROFILES)


def test_this_repository_ships_no_provider_entry_points() -> None:
    """The engine must not quietly register providers through its own plugin channel — that channel
    exists for third parties, and using it internally would make the precedence rules untestable."""
    from importlib.metadata import entry_points

    from bounded_loops.graph.adapters.connectors.provider_plugins import (
        PROVIDER_ENTRY_POINT_GROUP,
    )

    ours = [
        entry for entry in entry_points(group=PROVIDER_ENTRY_POINT_GROUP)
        if (entry.value or "").startswith("bounded_loops")
    ]
    assert ours == []


@pytest.mark.parametrize("bad_name", ["", 42, None])
def test_a_non_string_provider_name_is_refused(bad_name: object) -> None:
    def weird() -> Mapping[str, CliProfile]:
        return {bad_name: CliProfile("x")}  # type: ignore[dict-item]

    assert _with_plugins(_FakeEntryPoint("weird", weird)) == {}
