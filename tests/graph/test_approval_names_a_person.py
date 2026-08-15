"""An approval receipt must name WHO decided, not just which tenant was permitted.

This is the product's own thesis applied to the one place a human *is* the evidence. Everywhere
else a completion claim is backed by an independent gate; at an approval checkpoint it is backed by
a person, and a receipt that cannot say which person is materially weaker than the feature appears
to offer. "local-org approved the irreversible publish" is not an answer an enterprise reviewer
accepts.

The original defect (#56): `SameTenantArenaAuthorizer` requires `subject_id == organization_id`,
`actor_id` was set from `subject_id`, and a local run's organization is the constant `local-org` —
so every locally approved irreversible effect produced a receipt attributing it to the tenant.

The fix separates two questions that were being answered by one field:

    actor_id    WHO WAS PERMITTED  — the authorization subject, unchanged, still the tenant
    decided_by  WHO DECIDED        — a person, resolved from config / env / OS user

Both are recorded. Conflating them is how one of them ends up wrong.

**These tests never assert a real name.** They set the identity explicitly, because the OS-user
fallback resolves differently on every machine and a test that encoded one developer's username
would pass only for them. The property under test is "not the tenant, and honestly sourced".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from bounded_loops import local_identity
from bounded_loops.graph.cli_graph import cmd_graph_approve, cmd_graph_run

_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: approval-attribution
version: "1.0.0"
nodes:
  - id: checkpoint
    kind: approval
    required_role: reviewer
    inputs: {}
    outputs: {approved: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [read_only]
    isolation: workspace_only
edges: []
connection_slots: []
policies: {data_class: public, fail_mode: fail_closed}
"""


def _ns(**kw: object) -> argparse.Namespace:
    kw.setdefault("json", False)
    return argparse.Namespace(**kw)


def _run_and_decide(tmp_path: Path, decision: str, name: str) -> dict:
    """Pause at an approval, decide it, and return the recorded decision record.

    Grants land in ``commits`` and refusals in ``rejections`` — two lists, because a rejection does
    not advance the approval version chain. Both must carry attribution, so this returns whichever
    one the decision produced and the tests assert the same properties of each.
    """
    manifest = tmp_path / "graph.yaml"
    manifest.write_text(_MANIFEST, encoding="utf-8")
    out_dir = tmp_path / "run"

    cmd_graph_run(_ns(manifest=str(manifest), execute=True, out=str(out_dir), json=True))

    found = list(tmp_path.rglob("approvals.json")) or list(tmp_path.rglob("controller-events.jsonl"))
    assert found, "the run produced no run directory to decide against"
    run_dir = found[0].parent

    cmd_graph_approve(
        _ns(run=str(run_dir), node="checkpoint", decision=decision, inputs=None, json=True)
    )

    approvals = json.loads((run_dir / "approvals.json").read_text(encoding="utf-8"))
    key = "commits" if decision == "approved" else "rejections"
    records = approvals.get(key) or []
    assert records, f"no {key} entry recorded for a {decision} decision"
    return records[0]


# ── the defect itself ────────────────────────────────────────────────────────


def test_an_approval_receipt_does_not_attribute_the_decision_to_the_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact check #56 asked for, against a real receipt from a real run."""
    monkeypatch.setenv(local_identity.IDENTITY_ENV_VAR, "reviewer-under-test")

    commit = _run_and_decide(tmp_path, "approved", "reviewer-under-test")

    assert commit["decided_by"] == "reviewer-under-test"
    assert commit["decided_by"] != commit["actor_id"], (
        "the decision is attributed to the authorization subject again — this is #56 returning"
    )
    assert commit["decided_by"] != "local-org"


def test_the_authorization_subject_is_still_recorded_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`decided_by` must ADD a field, never repurpose `actor_id`.

    The authorizer's invariant is what makes tenancy hold. If a later change made `actor_id` carry
    the person, a run would record no evidence of which tenant was permitted, and the fix for an
    honesty gap would have opened an authorization one.
    """
    monkeypatch.setenv(local_identity.IDENTITY_ENV_VAR, "reviewer-under-test")

    commit = _run_and_decide(tmp_path, "approved", "reviewer-under-test")

    assert commit["actor_id"] == "local-org", (
        "the authorization subject must remain the tenant the authorizer actually checked"
    )


def test_a_REJECTION_is_attributed_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing an irreversible effect is as consequential as permitting one.

    Attribution that covers only the approve path would leave "who blocked the release?"
    unanswerable, and asymmetries like that are where the next gap lives.
    """
    monkeypatch.setenv(local_identity.IDENTITY_ENV_VAR, "reviewer-who-said-no")

    commit = _run_and_decide(tmp_path, "rejected", "reviewer-who-said-no")

    assert commit["decided_by"] == "reviewer-who-said-no"
    assert commit["decided_by"] != commit["actor_id"]


# ── the provenance that keeps the name honest ────────────────────────────────


def test_the_receipt_says_where_the_name_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name without its source overclaims. None of these sources is authentication.

    An env var is trivially set, a config file is written by anyone who can write the workspace,
    an OS user is self-asserted. Recording which one applied is what lets a reader weigh it —
    and is strictly more than the previous answer, which was nothing.
    """
    monkeypatch.setenv(local_identity.IDENTITY_ENV_VAR, "reviewer-under-test")

    commit = _run_and_decide(tmp_path, "approved", "reviewer-under-test")

    assert commit["decided_by_source"] == local_identity.SOURCE_ENVIRONMENT
    assert commit["decided_by_source"] in local_identity.SOURCE_MEANING


def test_a_local_identity_is_never_reported_as_verified() -> None:
    """There is no local authentication to verify against, so this is False by construction.

    Asserted as a property of the type rather than of one instance: a surface that renders a name
    must be able to say it is unverified, and a `verified` that could ever be True locally would
    let one render it as proven.
    """
    for source in local_identity.SOURCE_MEANING:
        assert local_identity.LocalIdentity(name="someone", source=source).verified is False


# ── resolution order, and refusing to record a name we would mangle ──────────


def test_configured_identity_outranks_the_environment() -> None:
    """Written down in the project beats set for one process — the more deliberate act wins."""
    identity = local_identity.resolve({"identity": {"name": "from-config"}})

    assert identity.name == "from-config"
    assert identity.source == local_identity.SOURCE_CONFIGURED


@pytest.mark.parametrize(
    "hostile",
    ["", "   ", "x" * 129, "name\nwith-newline", "name\x00with-nul", 42, None],
)
def test_an_unusable_identity_is_refused_rather_than_mangled(hostile: object) -> None:
    """Truncating an over-long name would record a DIFFERENT person than the one supplied.

    On an approval receipt that is worse than declining to record one. Control characters would
    corrupt the JSON line the receipt is written into, which is worse again — a malformed receipt
    is one the hash chain cannot replay.
    """
    identity = local_identity.resolve({"identity": {"name": hostile}})

    assert identity.name != hostile
    assert identity.source != local_identity.SOURCE_CONFIGURED


def test_resolution_never_raises_so_an_approval_cannot_fail_over_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision about an irreversible effect must not be lost because `getuser()` failed.

    The fallback is the string `unknown`, which records that nobody was identified — honestly
    different from nobody having approved.
    """
    monkeypatch.delenv(local_identity.IDENTITY_ENV_VAR, raising=False)
    monkeypatch.setattr(
        local_identity.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("no passwd entry"))
    )

    identity = local_identity.resolve(None)

    assert identity.name == local_identity.UNKNOWN
    assert identity.source == local_identity.SOURCE_UNKNOWN
    assert identity.meaning  # a reader still gets a sentence explaining what that means
