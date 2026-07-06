from bounded_loops.adapters.io.kill_switch import EnvKillSwitch


def test_not_tripped_by_default(monkeypatch):
    monkeypatch.delenv("BOUNDED_LOOPS_KILL", raising=False)
    assert EnvKillSwitch().tripped() is False


def test_tripped_when_env_set(monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_KILL", "1")
    assert EnvKillSwitch().tripped() is True


def test_not_tripped_when_env_empty_string(monkeypatch):
    monkeypatch.setenv("BOUNDED_LOOPS_KILL", "")
    assert EnvKillSwitch().tripped() is False


def test_conforms_to_kill_switch_port():
    from bounded_loops.application.ports import KillSwitchPort
    assert isinstance(EnvKillSwitch(), KillSwitchPort)
