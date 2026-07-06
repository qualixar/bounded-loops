"""Tests for domain/errors.py (frozen contracts)."""
import pytest
from bounded_loops.domain.errors import (
    BoundedLoopsError,
    ManifestError,
    RunnerError,
    GateError,
    KillSwitchTripped,
)


class TestErrorHierarchy:
    def test_manifest_error_is_bounded_loops_error(self):
        assert issubclass(ManifestError, BoundedLoopsError)

    def test_runner_error_is_bounded_loops_error(self):
        assert issubclass(RunnerError, BoundedLoopsError)

    def test_gate_error_is_bounded_loops_error(self):
        assert issubclass(GateError, BoundedLoopsError)

    def test_kill_switch_tripped_is_bounded_loops_error(self):
        assert issubclass(KillSwitchTripped, BoundedLoopsError)

    def test_base_error_is_exception(self):
        assert issubclass(BoundedLoopsError, Exception)

    def test_manifest_error_catches_as_base(self):
        with pytest.raises(BoundedLoopsError):
            raise ManifestError("bad yaml")

    def test_gate_error_carries_message(self):
        err = GateError("pytest -q could not run (code 127): not found")
        assert "127" in str(err)

    def test_kill_switch_tripped_raises_and_is_caught_as_base(self):
        with pytest.raises(BoundedLoopsError):
            raise KillSwitchTripped("signal SIGTERM")

    def test_sibling_errors_not_related(self):
        # ManifestError should not be catchable as GateError
        with pytest.raises(ManifestError):
            try:
                raise ManifestError("x")
            except GateError:
                pass  # must NOT catch
