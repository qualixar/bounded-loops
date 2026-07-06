from bounded_loops.adapters.io.approval import AutoApproval, CliApproval
from bounded_loops.domain.models import Verdict, LoopContext, Rung
from pathlib import Path


def _ctx():
    return LoopContext(workspace=Path("/tmp"), lap=1, rung=Rung.L2, trace_id="t")


def _verdict():
    return Verdict(passed=True, detail="ok")


def test_auto_approval_always_grants():
    assert AutoApproval().granted(_verdict(), _ctx()) is True


def test_cli_approval_grants_on_y(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "y")
    assert CliApproval().granted(_verdict(), _ctx()) is True


def test_cli_approval_denies_on_n(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert CliApproval().granted(_verdict(), _ctx()) is False


def test_cli_approval_fails_closed_on_eof(monkeypatch):
    def _raise():
        raise EOFError()
    monkeypatch.setattr("builtins.input", lambda: _raise())
    assert CliApproval().granted(_verdict(), _ctx()) is False
