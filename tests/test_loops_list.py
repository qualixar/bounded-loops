"""Tests for ``bl loops list`` — the loop catalog browser.

Verifies:
1. Running from the bounded-loops source tree finds all 68 shipped loops.
2. --role filter correctly narrows results.
3. --gate filter correctly narrows results.
4. --keyless filter excludes the 4 framework-example loops.
5. --json emits valid JSON with the right schema.
6. Combined filters narrow correctly.
7. bl loops with no action exits 1 with a clean message.
8. Role/gate counts match expected values derived from the catalog.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_BL = [sys.executable, "-m", "bounded_loops.cli"]


def _bl(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_BL, *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        env={**os.environ, "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/uvcache"},
    )


# ── Catalog discovery ────────────────────────────────────────────────────────

class TestLoopsListDiscovery:
    def test_finds_all_68_loops(self):
        result = _bl("loops", "list", "--json")
        assert result.returncode == 0, result.stderr
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 68, (
            f"Expected 68 loops, found {len(ok)}. "
            "If you added or removed a loop, update this test."
        )

    def test_table_output_contains_loop_names(self):
        result = _bl("loops", "list")
        assert result.returncode == 0, result.stderr
        # spot-check a few known loop names
        assert "invoice-3way-match" in result.stdout
        assert "json-config-schema" in result.stdout
        assert "citation-existence-check" in result.stdout

    def test_json_entries_have_required_fields(self):
        result = _bl("loops", "list", "--json")
        entries = json.loads(result.stdout)
        required = {"name", "roles", "gate_kind", "keyless", "description", "path", "error"}
        for entry in entries:
            missing = required - set(entry.keys())
            assert not missing, f"Entry {entry.get('name','?')} missing fields: {missing}"

    def test_returns_0_always(self):
        result = _bl("loops", "list")
        assert result.returncode == 0


# ── Role filtering ───────────────────────────────────────────────────────────

class TestLoopsListRoleFilter:
    def test_role_engineering_returns_correct_count(self):
        result = _bl("loops", "list", "--role", "engineering", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 11, f"Expected 11 engineering loops, got {len(ok)}"
        assert all("engineering" in e["roles"] for e in ok)

    def test_role_security_returns_correct_count(self):
        result = _bl("loops", "list", "--role", "security", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 7, f"Expected 7 security loops, got {len(ok)}"

    def test_role_filter_case_insensitive(self):
        lower = _bl("loops", "list", "--role", "engineering", "--json")
        upper = _bl("loops", "list", "--role", "Engineering", "--json")
        assert len(json.loads(lower.stdout)) == len(json.loads(upper.stdout))

    def test_unknown_role_returns_empty_list(self):
        result = _bl("loops", "list", "--role", "zzz-nonexistent-role", "--json")
        entries = json.loads(result.stdout)
        # Error entries pass through, but there should be no successful matches.
        ok = [e for e in entries if not e["error"]]
        assert ok == []

    def test_empty_result_message_is_helpful(self):
        result = _bl("loops", "list", "--role", "zzz-nonexistent-role")
        combined = result.stdout + result.stderr
        assert "No loops match" in combined
        assert "zzz-nonexistent-role" in combined


# ── Gate filtering ───────────────────────────────────────────────────────────

class TestLoopsListGateFilter:
    def test_gate_command_returns_correct_count(self):
        result = _bl("loops", "list", "--gate", "command", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 44, f"Expected 44 command loops, got {len(ok)}"
        assert all(e["gate_kind"] == "command" for e in ok)

    def test_gate_pytest_returns_correct_count(self):
        result = _bl("loops", "list", "--gate", "pytest", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 9, f"Expected 9 pytest loops, got {len(ok)}"

    def test_gate_jsonschema_returns_correct_count(self):
        result = _bl("loops", "list", "--gate", "jsonschema", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 10, f"Expected 10 jsonschema loops, got {len(ok)}"

    def test_gate_filter_case_insensitive(self):
        lower = _bl("loops", "list", "--gate", "pytest", "--json")
        upper = _bl("loops", "list", "--gate", "Pytest", "--json")
        assert len(json.loads(lower.stdout)) == len(json.loads(upper.stdout))


# ── Keyless filtering ────────────────────────────────────────────────────────

class TestLoopsListKeylessFilter:
    def test_keyless_returns_64_loops(self):
        result = _bl("loops", "list", "--keyless", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert len(ok) == 64, f"Expected 64 keyless loops, got {len(ok)}"
        assert all(e["keyless"] for e in ok)

    def test_keyless_excludes_framework_examples(self):
        result = _bl("loops", "list", "--keyless", "--json")
        entries = json.loads(result.stdout)
        names = {e["name"] for e in entries}
        for framework in ["adk-example", "autogen-example", "crewai-example", "langgraph-example"]:
            assert framework not in names, f"{framework} should be excluded by --keyless"

    def test_non_keyless_count_is_4(self):
        """Cross-check: catalog minus keyless = 4 framework examples."""
        all_result = _bl("loops", "list", "--json")
        kl_result = _bl("loops", "list", "--keyless", "--json")
        all_ok = [e for e in json.loads(all_result.stdout) if not e["error"]]
        kl_ok = [e for e in json.loads(kl_result.stdout) if not e["error"]]
        assert len(all_ok) - len(kl_ok) == 4


# ── Combined filters ─────────────────────────────────────────────────────────

class TestLoopsListCombinedFilters:
    def test_role_and_gate_combined(self):
        result = _bl("loops", "list", "--role", "security", "--gate", "command", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert all("security" in e["roles"] for e in ok)
        assert all(e["gate_kind"] == "command" for e in ok)

    def test_role_and_keyless_combined(self):
        result = _bl("loops", "list", "--role", "engineering", "--keyless", "--json")
        entries = json.loads(result.stdout)
        ok = [e for e in entries if not e["error"]]
        assert all("engineering" in e["roles"] for e in ok)
        assert all(e["keyless"] for e in ok)


# ── JSON output schema ───────────────────────────────────────────────────────

class TestLoopsListJsonOutput:
    def test_json_is_valid_array(self):
        result = _bl("loops", "list", "--json")
        parsed = json.loads(result.stdout)
        assert isinstance(parsed, list)

    def test_json_description_is_one_line(self):
        result = _bl("loops", "list", "--json")
        entries = json.loads(result.stdout)
        for e in entries:
            if e["error"] is None:
                assert "\n" not in e["description"], (
                    f"{e['name']}: description should be one line, "
                    f"got: {e['description']!r}"
                )

    def test_json_keyless_field_is_bool(self):
        result = _bl("loops", "list", "--json")
        entries = json.loads(result.stdout)
        for e in entries:
            assert isinstance(e["keyless"], bool), (
                f"{e['name']}: keyless should be bool, got {type(e['keyless'])}"
            )


# ── Error cases ──────────────────────────────────────────────────────────────

class TestLoopsListErrors:
    def test_loops_no_action_exits_1(self):
        result = _bl("loops")
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "Traceback" not in combined
        assert "no action" in combined.lower() or "available" in combined.lower()

    def test_table_output_does_not_crash_on_empty_dir(self, tmp_path):
        """Running from a directory with no loops should print a helpful message."""
        result = _bl("loops", "list", cwd=tmp_path)
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        # Either no loops found message, or an empty catalog table — no crash.
        assert "Traceback" not in combined
