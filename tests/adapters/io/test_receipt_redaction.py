"""
Redaction tests.

The property that matters is not "paths disappear" — it is that redaction happens
*before* hashing, so a redacted ledger still verifies. A test that only checked the
string substitution would pass on an implementation that redacted after the write
and silently broke every chain.

Each assertion that claims something was removed first asserts the input actually
contained it. Three tamper tests in this repo once passed while editing nothing;
the lesson generalises to any test that concludes from an absence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bounded_loops.adapters.io.file_ledger import FileLedger
from bounded_loops.adapters.io.ledger_chain import ChainStatus, chain_ledger_lines
from bounded_loops.adapters.io.receipt_redaction import (
    PLACEHOLDER_PATH,
    PLACEHOLDER_WORKSPACE,
    RedactingLedger,
    RedactionMode,
    RedactionPolicy,
    redact_entry,
    wrap_if_active,
)
from bounded_loops.domain.models import LedgerEntry, Verdict


def _entry(detail: str, evidence: dict) -> LedgerEntry:
    return LedgerEntry(
        lap=1,
        ts="2026-08-17T00:00:00Z",
        verdict=Verdict(passed=False, detail=detail, evidence=evidence),
        decision="continue",
        budget_spent={"laps": 1},
        attempted=True,
        handoff="",
    )


def test_off_is_a_no_op_and_returns_the_same_object() -> None:
    """The default must cost nothing and change nothing.

    Identity, not equality: OFF should not even rebuild the entry, because the
    audit-grade default is the one that must stay byte-identical to 0.6.6.
    """
    entry = _entry("/private/tmp/x/report.json failed", {"tail": "boom"})
    assert redact_entry(entry, RedactionPolicy()) is entry


def test_paths_mode_removes_an_absolute_path_that_was_present() -> None:
    original = "cannot read /Users/someone/secret/report.json"
    assert "/Users/someone" in original  # the premise, asserted before concluding
    out = redact_entry(_entry(original, {}), RedactionPolicy(mode=RedactionMode.PATHS))
    assert "/Users/someone" not in out.verdict.detail
    assert PLACEHOLDER_PATH in out.verdict.detail


def test_paths_under_the_workspace_root_stay_identifiable() -> None:
    """A reader debugging a run needs to know which file, just not whose machine."""
    root = Path("/private/tmp/ws-abc")
    entry = _entry(f"{root}/out/report.json is malformed", {})
    out = redact_entry(
        entry, RedactionPolicy(mode=RedactionMode.PATHS, workspace_root=root)
    )
    assert PLACEHOLDER_WORKSPACE in out.verdict.detail
    assert "out/report.json" in out.verdict.detail, "the relative part must survive"
    assert str(root) not in out.verdict.detail


def test_strict_mode_replaces_output_tail_with_a_digest() -> None:
    secret = "row 42: patient Jane Doe, balance 12.00"
    out = redact_entry(
        _entry("gate failed", {"tail": secret}),
        RedactionPolicy(mode=RedactionMode.STRICT),
    )
    tail = out.verdict.evidence["tail"]
    assert secret not in tail
    assert tail.startswith("sha256:"), tail
    # Re-derivable: the same output yields the same digest, so a verifier can still
    # confirm "this exact output produced this verdict".
    again = redact_entry(
        _entry("gate failed", {"tail": secret}),
        RedactionPolicy(mode=RedactionMode.STRICT),
    )
    assert again.verdict.evidence["tail"] == tail


def test_paths_mode_keeps_the_tail_that_strict_mode_drops() -> None:
    """The two modes must differ, or one of them is decoration."""
    ev = {"tail": "assertion failed at line 9"}
    paths = redact_entry(_entry("d", ev), RedactionPolicy(mode=RedactionMode.PATHS))
    strict = redact_entry(_entry("d", ev), RedactionPolicy(mode=RedactionMode.STRICT))
    assert paths.verdict.evidence["tail"] == "assertion failed at line 9"
    assert strict.verdict.evidence["tail"] != paths.verdict.evidence["tail"]


@pytest.mark.parametrize("key", ["tail", "stdout_tail", "stderr_tail", "output_tail"])
def test_every_spelling_of_the_output_key_is_covered(key: str) -> None:
    """The gate adapters spell this several ways; a missed spelling is a leak."""
    out = redact_entry(
        _entry("d", {key: "sensitive"}), RedactionPolicy(mode=RedactionMode.STRICT)
    )
    assert out.verdict.evidence[key].startswith("sha256:")


def test_nested_evidence_is_walked() -> None:
    out = redact_entry(
        _entry("d", {"outer": {"inner": ["/Users/x/y", "ok"]}}),
        RedactionPolicy(mode=RedactionMode.PATHS),
    )
    assert out.verdict.evidence["outer"]["inner"][0] == PLACEHOLDER_PATH
    assert out.verdict.evidence["outer"]["inner"][1] == "ok"


def test_the_input_entry_is_never_mutated() -> None:
    ev = {"tail": "/Users/x/secret"}
    entry = _entry("/Users/x/secret", ev)
    redact_entry(entry, RedactionPolicy(mode=RedactionMode.STRICT))
    assert entry.verdict.detail == "/Users/x/secret"
    assert ev["tail"] == "/Users/x/secret", "evidence dict was mutated in place"


def test_unknown_mode_is_refused_not_silently_off() -> None:
    """A deployment that asks for redaction and gets none must see an error."""
    with pytest.raises(ValueError, match="unknown redaction mode"):
        RedactionPolicy.from_mode("gdpr")


def test_wrap_if_active_does_not_wrap_when_off() -> None:
    inner = object()
    assert wrap_if_active(inner, RedactionPolicy()) is inner
    wrapped = wrap_if_active(inner, RedactionPolicy(mode=RedactionMode.PATHS))
    assert isinstance(wrapped, RedactingLedger)


# ── the property that actually matters ────────────────────────────────────────

def test_a_redacted_ledger_still_verifies(tmp_path: Path) -> None:
    """Redaction must precede hashing, or it breaks the chain it sits inside.

    This is the test that distinguishes a correct implementation from one that
    redacts on the way out and destroys verification.
    """
    path = tmp_path / "ledger.jsonl"
    ledger = RedactingLedger(
        FileLedger(path), RedactionPolicy(mode=RedactionMode.STRICT)
    )
    for lap in range(1, 4):
        ledger.record(
            LedgerEntry(
                lap=lap,
                ts=f"2026-08-17T00:00:0{lap}Z",
                verdict=Verdict(
                    passed=False,
                    detail=f"/Users/someone/run/{lap}.json failed",
                    evidence={"tail": f"secret-{lap}"},
                ),
                decision="continue",
                budget_spent={"laps": lap},
                attempted=True,
                handoff="",
            )
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    report = chain_ledger_lines(lines)
    assert report.status is ChainStatus.VERIFIED, report.detail
    assert report.verified_lines == 3


def test_redacted_content_is_absent_from_the_bytes_on_disk(tmp_path: Path) -> None:
    """Assert against the file, not the object. The file is what leaves the host."""
    path = tmp_path / "ledger.jsonl"
    secret = "patient-identifier-99887"
    ledger = RedactingLedger(
        FileLedger(path), RedactionPolicy(mode=RedactionMode.STRICT)
    )
    ledger.record(_entry("/Users/someone/x failed", {"tail": secret}))

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "/Users/someone" not in raw
    # And the row is still a well-formed receipt, not a mangled one.
    row = json.loads(raw.splitlines()[0])
    assert row["verdict"]["passed"] is False
    assert row["verdict"]["evidence"]["tail"].startswith("sha256:")


def test_an_unredacted_ledger_would_have_leaked_it(tmp_path: Path) -> None:
    """Proves the previous test is measuring redaction and not a quirk of the fixture.

    Without this, `secret not in raw` could pass because the secret never reached
    the ledger at all — which is exactly the vacuity this project publishes about.
    """
    path = tmp_path / "ledger.jsonl"
    secret = "patient-identifier-99887"
    FileLedger(path).record(_entry("/Users/someone/x failed", {"tail": secret}))
    raw = path.read_text(encoding="utf-8")
    assert secret in raw, "fixture does not exercise the leak it claims to prevent"
    assert "/Users/someone" in raw
