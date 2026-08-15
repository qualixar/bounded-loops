"""The gate defects found and fixed in 0.6.2, pinned so they stay fixed.

Every case here was CONFIRMED BY RUNNING the shipped checker before it was fixed —
none was inferred from reading. They were found while designing the held-out mutant
corpus (#36), by asking of each gate: what does its stated purpose promise, and what
does its implementation actually do? The gap between those two is where a false
accept lives, and it is invisible to whoever wrote the implementation.

Both directions are tested, because a gate fix is only half done if it trades a
false accept for a false reject. Tightening `check_clauses.py` during this very
sweep broke `nda-required-clauses` — a word-boundary match rejected the legitimate
plural heading "Permitted Disclosures" — and the suite stayed green because nothing
ran the loops. See `test_catalog_convergence.py`.

The eight classes, as a checklist for the next gate anyone writes:

  1. presence checked anywhere in the text  -> a NEGATION or a COMMENT satisfies it
  2. heading checked, content not           -> a table of contents passes as a document
  3. one syntax matched                     -> the alternate form is invisible
  4. pattern derived from the data it checks-> anything outside that data is never examined
  5. regex stricter than the spec it names  -> valid input is rejected
  6. presence checked, ORDER ignored        -> a later instruction overrides the one that passed
  7. sync-only AST walk                     -> `async def` is invisible
  8. empty collection                       -> passes vacuously
  9. the FIX itself is evadable              -> requiring a heading moved the substring defect
                                               into headings ("## No Audit Rights") instead of
                                               removing it

Round 2 exists because of class 9. After fixing the first fourteen I probed my own fixes the
same way I had probed the originals, and three survived: a negated heading, a case reporter
with no periods, and an unquoted YAML value. A fix is not a fix until it has been attacked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOOPS = REPO_ROOT / "loops"


@dataclass(frozen=True)
class GateCase:
    """One input, run against one shipped gate, with the exit code it must produce."""

    loop: str
    script: str
    files: dict[str, str]
    args: tuple[str, ...]
    expected: int
    why: str
    extra_args: tuple[str, ...] = field(default=())

    @property
    def id(self) -> str:
        return f"{self.loop}:{self.why[:44]}"


def _run(case: GateCase, tmp_path: Path) -> subprocess.CompletedProcess:
    for name, content in case.files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    script = LOOPS / case.loop / case.script
    assert script.is_file(), f"gate script missing: {script}"
    argv = [sys.executable, str(script), *[str(tmp_path / a) for a in case.args]]
    argv.extend(str(LOOPS / case.loop / extra) for extra in case.extra_args)
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


# ---------------------------------------------------------------------------
# FALSE ACCEPTS — every one of these exited 0 before 0.6.2.
# ---------------------------------------------------------------------------

FALSE_ACCEPTS: list[GateCase] = [
    GateCase(
        loop="gdpr-dpa-terms", script="seed/check_dpa.py", args=("dpa.md",), expected=1,
        why="a DPA DENYING audit rights satisfied the audit requirement",
        files={"dpa.md": (
            "# DPA\n## Subject Matter\nx\n## Duration\nx\n## Nature and Purpose\nx\n"
            "## Type of Personal Data\nx\n## Obligations of the Controller\nx\n"
            "## Sub-Processor\nx\n## Confidentiality\nx\n## Security Measures\nx\n\n"
            "This agreement grants NO audit rights whatsoever to the controller.\n"
        )},
    ),
    GateCase(
        loop="runbook-completeness", script="seed/check_runbook.py", args=("rb.md",), expected=1,
        why="seven headings and no content at all passed as a complete runbook",
        files={"rb.md": "# RB\n## Summary\n## Severity\n## Detection\n## Diagnosis\n"
                        "## Mitigation\n## Rollback\n## Escalation\n"},
    ),
    GateCase(
        loop="rfc-decision-recorded", script="seed/check_rfc.py", args=("r.md",), expected=1,
        why="four bare headings recorded no decision but passed",
        files={"r.md": "# RFC\n## Status\n## Context\n## Decision\n## Consequences\n"},
    ),
    GateCase(
        loop="alt-text-present", script="seed/check_alt.py", args=("post.md",), expected=1,
        why="an HTML <img> with no alt attribute was invisible",
        files={"post.md": '# P\n<img src="chart.png">\n![real](ok.png)\n'},
    ),
    GateCase(
        loop="citation-existence-check", script="seed/check_citations.py",
        args=("brief.md",), extra_args=("seed/known_reporter.json",), expected=1,
        why="an INVENTED case in an unlisted reporter was never examined",
        files={"brief.md": "The court in Smith v. Jones, 500 F.3d 100 (2026), held otherwise.\n"},
    ),
    GateCase(
        loop="cds-view-annotations", script="seed/check_cds.py", args=("z.txt",), expected=1,
        why="COMMENTED-OUT annotations counted as present",
        files={"z.txt": "// @AccessControl.authorizationCheck: #CHECK\n"
                        "// @EndUserText.label: x\n// @Metadata.allowExtensions: true\n"
                        "define view Z as select from t {}\n"},
    ),
    GateCase(
        loop="dockerfile-no-root", script="seed/check_dockerfile.py",
        args=("Dockerfile",), expected=1,
        why="USER root AFTER a non-root USER still passed a security gate",
        files={"Dockerfile": 'FROM python:3.12\nUSER appuser\nUSER root\nCMD ["x"]\n'},
    ),
    GateCase(
        loop="assertion-density", script="seed/check_assertions.py", args=("t.py",), expected=1,
        why="an async test with zero assertions was invisible",
        files={"t.py": "async def test_nothing():\n    pass\n"},
    ),
    GateCase(
        loop="test-naming-contract", script="seed/check_test_names.py", args=("t.py",), expected=1,
        why="a misnamed async test method was invisible",
        files={"t.py": "class TestX:\n    async def helper(self):\n        assert 1\n"},
    ),
    GateCase(
        loop="okr-measurable", script="seed/check_okrs.py", args=("o.json",), expected=1,
        why="an objective with ZERO key results passed vacuously",
        files={"o.json": '[{"objective":"Grow","key_results":[]}]\n'},
    ),
    GateCase(
        loop="broken-internal-links", script="seed/check_links.py", args=("c",), expected=1,
        why="an HTML <a href> to a missing file was invisible",
        files={"c/a.md": '# A\n<a href="missing.md">gone</a>\n'},
    ),
    GateCase(
        loop="secret-scan-keyless", script="seed/check_secrets.py", args=("s.py",), expected=1,
        why="a password inside a dict literal was invisible",
        files={"s.py": 'CFG = {"password": "hunter2"}\n'},
    ),
    # The two below shared check_dpa.py's substring-anywhere code path verbatim and
    # were fixed with it. Unlike the twelve above they were not probed BEFORE the
    # fix, so they are same-class fixes rather than independently confirmed defects
    # — worth stating plainly rather than inflating the confirmed count.
    GateCase(
        loop="privacy-policy-completeness", script="seed/check_privacy.py",
        args=("p.md",), expected=1,
        why="a policy DENYING it retains data satisfied Data Retention",
        files={"p.md": "# Privacy Policy\n## Data We Collect\nx\n## How We Use Your Data\nx\n"
                       "## Data Sharing\nx\n## Your Rights\nx\n## Contact\nx\n\n"
                       "We publish no data retention schedule of any kind.\n"},
    ),
    # Round 2 — found by probing my OWN 0.6.2 fixes before release. Fixing the substring
    # class by requiring a heading MOVED the defect into headings rather than removing it,
    # and two narrowed patterns were still narrower than the thing they claim to check.
    GateCase(
        loop="gdpr-dpa-terms", script="seed/check_dpa.py", args=("dpa.md",), expected=1,
        why="a heading that NEGATES itself satisfied its own requirement",
        files={"dpa.md": (
            "# DPA\n## Subject Matter\nx\n## Duration\nx\n## Nature and Purpose\nx\n"
            "## Type of Personal Data\nx\n## Obligations of the Controller\nx\n"
            "## Sub-Processor\nx\n## Confidentiality\nx\n## Security Measures\nx\n"
            "## No Audit Rights Are Granted\nnone\n"
        )},
    ),
    # Round 3 — found by the Grok audit, one round after round 2 fixed the same class.
    # Prefix-matching a negation caught "## No Audit Rights" and missed the negation simply
    # moving past the first word.
    GateCase(
        loop="gdpr-dpa-terms", script="seed/check_dpa.py", args=("dpa.md",), expected=1,
        why="negation AFTER the term ('## Audit: None Granted') still satisfied it",
        files={"dpa.md": "# DPA\n## Subject Matter\nx\n## Duration\nx\n## Nature and Purpose\nx\n## Type of Personal Data\nx\n## Obligations of the Controller\nx\n## Sub-Processor\nx\n## Confidentiality\nx\n## Security Measures\nx\n## Audit: None Granted\nnone\n"},
    ),
    GateCase(
        loop="gdpr-dpa-terms", script="seed/check_dpa.py", args=("dpa.md",), expected=1,
        why="negation inside parentheses ('## Rights (No Audit Permitted)')",
        files={"dpa.md": "# DPA\n## Subject Matter\nx\n## Duration\nx\n## Nature and Purpose\nx\n## Type of Personal Data\nx\n## Obligations of the Controller\nx\n## Sub-Processor\nx\n## Confidentiality\nx\n## Security Measures\nx\n## Rights (No Audit Permitted)\nn\n"},
    ),
    GateCase(
        loop="runbook-completeness", script="seed/check_runbook.py", args=("rb.md",), expected=1,
        why="a section whose only content was an EMPTY SUB-HEADING passed",
        files={"rb.md": "# RB\n## Summary\ns\n## Severity\ns\n## Detection\nd\n"
                        "## Diagnosis\nd\n## Mitigation\nm\n## Rollback\n### TBD\n"
                        "## Escalation\ne\n"},
    ),
    GateCase(
        loop="citation-existence-check", script="seed/check_citations.py",
        args=("brief.md",), extra_args=("seed/known_reporter.json",), expected=1,
        why="a reporter with no periods (Westlaw) was still invisible",
        files={"brief.md": "See Smith v. Jones, 500 WL 12345 (2026).\n"},
    ),
    GateCase(
        loop="secret-scan-keyless", script="seed/check_secrets.py", args=("s.yaml",), expected=1,
        why="an UNQUOTED yaml value hid a secret the quoted form catches",
        files={"s.yaml": "password: hunter2\napi_key: sk-abc123\n"},
    ),
    GateCase(
        loop="nda-required-clauses", script="seed/check_clauses.py", args=("nda.md",), expected=1,
        why="prose DENYING a governing law satisfied Governing Law",
        files={"nda.md": "# NDA\n## Confidentiality\nx\n## Term\nx\n"
                         "## Return of Materials\nx\n## Permitted Disclosures\nx\n\n"
                         "The parties agree that no governing law is specified herein.\n"},
    ),
]

# ---------------------------------------------------------------------------
# FALSE REJECTS — valid input the gate wrongly BLOCKED before 0.6.2.
# ---------------------------------------------------------------------------

FALSE_REJECTS: list[GateCase] = [
    GateCase(
        loop="conventional-commits", script="seed/check_commits.py",
        args=("commits.txt",), expected=0,
        why="'feat!:' is the spec's own breaking-change marker",
        files={"commits.txt": "feat!: drop legacy API\nrevert(api)!: undo\nstyle: fmt\n"},
    ),
    GateCase(
        loop="dependency-pinning", script="seed/check_pins.py", args=("r.txt",), expected=0,
        why="extras, spacing, local versions and markers are all exact pins",
        files={"r.txt": 'requests[security]==2.31.0\nurllib3 == 2.0.7\n'
                        'torch==2.1.0+cpu\ntomli==2.0.1; python_version < "3.11"\n'},
    ),
]

# ---------------------------------------------------------------------------
# NOT OVER-CORRECTED — legitimate input that must still pass after the fixes.
# A fix that trades a false accept for a false reject has not fixed anything.
# ---------------------------------------------------------------------------

STILL_ACCEPTED: list[GateCase] = [
    GateCase(
        loop="dependency-pinning", script="seed/check_pins.py", args=("r.txt",), expected=1,
        why="a WILDCARD is a prefix match, not a pin, and must still be rejected",
        files={"r.txt": "foo==1.0.*\n"},
    ),
    GateCase(
        loop="dockerfile-no-root", script="seed/check_dockerfile.py",
        args=("Dockerfile",), expected=0,
        why="root during build then a non-root USER last is the correct pattern",
        files={"Dockerfile": "FROM python:3.12\nUSER root\nRUN apt-get install -y curl\n"
                             'USER appuser\nCMD ["x"]\n'},
    ),
    GateCase(
        loop="nda-required-clauses", script="seed/check_clauses.py", args=("nda.md",), expected=0,
        why="the PLURAL heading 'Permitted Disclosures' is legitimate",
        files={"nda.md": "# NDA\n## Confidentiality\nx\n## Term\nx\n## Governing Law\nx\n"
                         "## Return of Materials\nx\n## Permitted Disclosures\nx\n"},
    ),
    GateCase(
        loop="nda-required-clauses", script="seed/check_clauses.py", args=("nda.md",), expected=1,
        why="'Termination' must NOT satisfy the 'term' requirement",
        files={"nda.md": "# NDA\n## Confidentiality\nx\n## Termination\nx\n## Governing Law\nx\n"
                         "## Return of Materials\nx\n## Permitted Disclosures\nx\n"},
    ),
    GateCase(
        loop="citation-existence-check", script="seed/check_citations.py",
        args=("brief.md",), extra_args=("seed/known_reporter.json",), expected=0,
        why="statutes and regulations are not cases and must not be flagged",
        files={"brief.md": "See 5 U.S.C. 552 and 29 C.F.R. 1910. Also Brown, 347 U.S. 483.\n"},
    ),
    GateCase(
        loop="runbook-completeness", script="seed/check_runbook.py", args=("rb.md",), expected=0,
        why="a sub-heading and its prose count as the parent section's content",
        files={"rb.md": "# RB\n## Summary\ns\n## Severity\ns\n## Detection\nd\n## Diagnosis\nd\n"
                        "## Mitigation\n### Step 1\ndo it\n## Rollback\nr\n## Escalation\ne\n"},
    ),
    GateCase(
        loop="assertion-density", script="seed/check_assertions.py", args=("t.py",), expected=0,
        why="an async test WITH an assertion is a valid test",
        files={"t.py": "async def test_ok():\n    assert 1\n"},
    ),
    GateCase(
        loop="secret-scan-keyless", script="seed/check_secrets.py", args=("s.py",), expected=0,
        why="a `password: str` type annotation is not a hardcoded secret",
        files={"s.py": "def f(password: str) -> None:\n    pass\n"},
    ),
    GateCase(
        loop="broken-internal-links", script="seed/check_links.py", args=("c",), expected=0,
        why="external URLs are out of scope and must not be resolved on disk",
        files={"c/a.md": '# A\n<a href="https://example.com">ext</a>\n[b](b.md)\n', "c/b.md": "# B\n"},
    ),
    GateCase(
        loop="gdpr-dpa-terms", script="seed/check_dpa.py", args=("dpa.md",), expected=0,
        why="a real Audit heading is not a negation and must still satisfy the requirement",
        files={"dpa.md": (
            "# DPA\n## Subject Matter\nx\n## Duration\nx\n## Nature and Purpose\nx\n"
            "## Type of Personal Data\nx\n## Obligations of the Controller\nx\n"
            "## Sub-Processor\nx\n## Confidentiality\nx\n## Security Measures\nx\n"
            "## Audit\nThe controller may audit annually.\n"
        )},
    ),
    GateCase(
        loop="nda-required-clauses", script="seed/check_clauses.py", args=("nda.md",), expected=0,
        why="'Non-Disclosure' is a clause name, not a negation",
        files={"nda.md": "# NDA\n## Confidentiality and Non-Disclosure\nx\n## Term\nx\n"
                         "## Governing Law\nx\n## Return of Materials\nx\n"
                         "## Permitted Disclosures\nx\n"},
    ),
    GateCase(
        loop="runbook-completeness", script="seed/check_runbook.py", args=("rb.md",), expected=0,
        why="a sub-heading WITH prose under it is still written content",
        files={"rb.md": "# RB\n## Summary\ns\n## Severity\ns\n## Detection\nd\n"
                        "## Diagnosis\nd\n## Mitigation\n### Step 1\ndo it\n"
                        "## Rollback\nr\n## Escalation\ne\n"},
    ),
    GateCase(
        loop="secret-scan-keyless", script="seed/check_secrets.py", args=("c.yaml",), expected=0,
        why="ordinary yaml config keys are not credentials",
        files={"c.yaml": "timeout: 30\nname: myapp\n"},
    ),
    GateCase(
        loop="cds-view-annotations", script="seed/check_cds.py", args=("z.txt",), expected=0,
        why="real, uncommented annotations still satisfy the gate",
        files={"z.txt": "@AccessControl.authorizationCheck: #CHECK\n@EndUserText.label: x\n"
                        "@Metadata.allowExtensions: true\ndefine view Z as select from t {}\n"},
    ),
]

#: Round 3 — class 10: **a verdict filed as an inability to run**.
#:
#: These seven gates already DETECTED vacuity. Each returned 2 on an artifact with nothing to
#: check, and 2 is the documented code for "could not run" — which `CommandGate` classifies as a
#: gate ERROR, not a rejection. So the detection was performed and then thrown away: in production
#: an emptied artifact halted the run with an error instead of looping back to tell the worker what
#: was wrong, and in the mutant corpus all 84 such mutants were excluded from α as "not judged".
#:
#: This is the subtlest shape of the vacuity defect found so far, because the guard is present and
#: reads correctly. Only running it end to end shows the answer never reaches a caller. It is also
#: the reason error counts deserve reading: 84 of 233 mutants were errors, seven loops produced
#: NOTHING BUT errors, and a rate computed over the remainder looked perfect.
#:
#: The line these pin: exit 2 means no verdict was possible (unreadable file, bad argv). Exit 1
#: means the gate read the artifact and the answer is no. "Nothing to check" is an answer.
VACUITY_IS_A_REJECTION_NOT_AN_ERROR = [
    GateCase(
        loop="alt-text-present", script="seed/check_alt.py", args=("post.md",), expected=1,
        why="a post with no images was reported as 'could not run' instead of failing",
        files={"post.md": ""},
    ),
    GateCase(
        loop="bibliography-completeness", script="seed/check_biblio.py",
        args=("paper.md",), expected=1,
        why="a paper with no References section cannot list the keys it cites",
        files={"paper.md": "# Paper\nSome prose with no citations.\n"},
    ),
    GateCase(
        loop="bibliography-completeness", script="seed/check_biblio.py",
        args=("paper.md",), expected=1,
        why="a References section with no parseable keys satisfies no citation",
        files={"paper.md": "# Paper\nText [@a].\n\n## References\n\nnothing usable here\n"},
    ),
    GateCase(
        loop="contract-defined-terms", script="seed/check_defined.py",
        args=("contract.md",), expected=1,
        why="a contract with no Definitions section defines nothing it uses",
        files={"contract.md": "# Contract\nThe **Effective Date** is important.\n"},
    ),
    GateCase(
        loop="dockerfile-no-root", script="seed/check_dockerfile.py",
        args=("Dockerfile",), expected=1,
        why="a file with no FROM pins no base image",
        files={"Dockerfile": "# just a comment\n"},
    ),
    GateCase(
        loop="idoc-xml-schema", script="seed/check_idoc.py", args=("idoc.xml",), expected=1,
        why="XML that does not parse is the most structurally invalid an IDoc can be",
        files={"idoc.xml": ""},
    ),
    GateCase(
        loop="meeting-action-items", script="seed/check_actions.py",
        args=("minutes.md",), expected=1,
        why="minutes with no Action Items section record no actions",
        files={"minutes.md": "# Minutes\nWe talked about things.\n"},
    ),
    GateCase(
        loop="meeting-action-items", script="seed/check_actions.py",
        args=("minutes.md",), expected=1,
        why="an Action Items section with no bullets records no actions",
        files={"minutes.md": "# Minutes\n## Action Items\n\n## Next Meeting\nsoon\n"},
    ),
    GateCase(
        loop="prd-acceptance-criteria", script="seed/check_prd.py",
        args=("prd.md",), expected=1,
        why="a PRD with no stories cannot show that every story is verifiable",
        files={"prd.md": "# PRD\nA product with no stories written yet.\n"},
    ),
    # The other half of the line. Without these, "return 1 more often" would pass every case above
    # while destroying the distinction that makes an ERROR meaningful — a gate that cannot run must
    # not be recorded as having judged, because that credits it for catching what it never saw.
    GateCase(
        loop="alt-text-present", script="seed/check_alt.py", args=("absent.md",), expected=2,
        why="a file that cannot be read yields NO verdict and must stay an error",
        files={"post.md": "# P\n![a](x.png)\n"},
    ),
    GateCase(
        loop="prd-acceptance-criteria", script="seed/check_prd.py",
        args=("absent.md",), expected=2,
        why="a file that cannot be read yields NO verdict and must stay an error",
        files={"prd.md": "# PRD\n## Story: x\n### Acceptance Criteria\n- [ ] y\n"},
    ),
]

ALL_CASES = (
    FALSE_ACCEPTS + FALSE_REJECTS + STILL_ACCEPTED + VACUITY_IS_A_REJECTION_NOT_AN_ERROR
)


@pytest.mark.parametrize("case", ALL_CASES, ids=[c.id for c in ALL_CASES])
def test_gate_verdict(case: GateCase, tmp_path: Path) -> None:
    result = _run(case, tmp_path)
    assert result.returncode == case.expected, (
        f"{case.loop}: expected exit {case.expected}, got {result.returncode} — {case.why}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


#: Loops whose gate was tightened but where no legitimate input sits close enough
#: to the new boundary to be worth a guard case. Listed explicitly so that adding a
#: gate fix without thinking about over-correction fails this test rather than
#: passing quietly.
NO_NEAR_MISS_TO_GUARD = {
    "privacy-policy-completeness",  # guarded via gdpr-dpa-terms, identical code path
    "alt-text-present",         # "image has alt text" has no near-miss
    "rfc-decision-recorded",    # guarded via runbook-completeness, identical _sections
    "okr-measurable",           # "zero key results" has no legitimate form
    "test-naming-contract",     # guarded via assertion-density, identical _ANY_FUNC
    "conventional-commits",     # its FALSE_REJECTS case IS the boundary
}


def test_every_fixed_gate_is_pinned_in_both_directions() -> None:
    """Bookkeeping: no gate may be changed in 0.6.2 without a case in this file.

    Twelve false accepts were confirmed by running the shipped checker before the
    fix. Two more (privacy-policy-completeness, nda-required-clauses) shared
    check_dpa.py's code path verbatim and were fixed with it — same-class fixes,
    counted separately so the "confirmed by running" figure stays honest.
    """
    accepts = {c.loop for c in FALSE_ACCEPTS}
    rejects = {c.loop for c in FALSE_REJECTS}

    assert len(accepts) == 14, sorted(accepts)  # 14 loops; 17 confirmed defect cases
    assert rejects == {"conventional-commits", "dependency-pinning"}

    unguarded = (accepts | rejects) - {c.loop for c in STILL_ACCEPTED}
    assert unguarded == NO_NEAR_MISS_TO_GUARD, sorted(unguarded)
