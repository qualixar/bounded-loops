"""The configuration interview: ask the consequential questions, and only those.

Two failure modes matter, in opposite directions. Asking too little means an agent silently
defaults the decisions a human should have made — whether an irreversible effect gets approved,
what a node may spend. Asking too much is the struggling the feature exists to prevent: an
already-correct graph that generates a question per node teaches the user to click past all of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bounded_loops.graph.application.interview import interview, interview_document

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def shipped() -> dict:
    """A real reference graph — correct, complete, and not ours to second-guess."""
    return yaml.safe_load(
        (REPO_ROOT / "graphs" / "customer-data-request" / "graph.yaml").read_text(encoding="utf-8")
    )


_BARE_DRAFT = {
    "graph_id": "ship-it",
    "nodes": [
        {"id": "check", "kind": "loop"},
        {
            "id": "publish",
            "kind": "publish",
            "effects": ["irreversible"],
            "budget": {"max_attempts": 3, "max_wallclock_s": 60},
        },
    ],
    "policies": {},
}


def test_an_already_correct_graph_is_NOT_interrogated(shipped: dict) -> None:
    """Thirteen questions about a working graph is how a user learns to ignore all of them."""
    questions = interview(shipped)

    assert len(questions) <= 2, [q.key for q in questions]
    assert [q for q in questions if q.weight == "high"] == []


def test_effects_declared_as_EMPTY_is_an_answer_not_a_gap(shipped: dict) -> None:
    """`effects: []` says "grant this node nothing" — a deliberate choice every shipped graph makes.

    Treating absent and empty alike turned a correct graph into a question per node, which is the
    bug this test exists to keep fixed.
    """
    assert any(node.get("effects") == [] for node in shipped["nodes"]), "fixture assumption"

    keys = {q.key for q in interview(shipped)}

    assert not any(key.startswith("effects:") for key in keys)


def test_a_node_with_NO_effects_key_at_all_IS_asked() -> None:
    """The other direction: silence about authority must not read as a decision."""
    keys = {q.key for q in interview(_BARE_DRAFT)}

    assert "effects:check" in keys


def test_an_irreversible_effect_with_no_approval_node_is_a_MUST_ASK() -> None:
    """The headline risk: something that cannot be undone, running with nobody asked."""
    document = interview_document(_BARE_DRAFT)

    assert "approval:publish" in document["must_ask"]
    question = next(q for q in interview(_BARE_DRAFT) if q.key == "approval:publish")
    assert "nothing pauses for a human" in question.why


def test_a_retrying_node_that_reaches_outside_is_a_MUST_ASK() -> None:
    """The compiler refuses this outright, so asking late means the graph never runs."""
    assert "retry:publish" in interview_document(_BARE_DRAFT)["must_ask"]


def test_spend_is_asked_only_where_spend_can_be_METERED() -> None:
    """Asking for a ceiling the runtime cannot measure does not protect the run — it breaks it.

    This test previously asserted the opposite, that `external_write` was enough to ask. It is
    not: an effect describes consequences, not token consumption, and a node with no connection
    slot has no provider reporting usage. Setting a ceiling on one makes the runtime fail the
    node with `budget_unmeasurable` — correctly, since a budget nothing can measure would never
    fire — so the interview's only question about the shipped keyless graphs was one whose only
    answer turned a working graph into a failed run.
    """
    keys = {q.key for q in interview(_BARE_DRAFT)}

    assert "spend:publish" not in keys, (
        "a node with an effect but no connection slot has no metered worker; a ceiling here "
        "fails the run rather than pausing it"
    )
    assert "spend:check" not in keys, "a node with no effects and no slot cannot spend"


def test_a_slot_bound_node_IS_asked_for_a_ceiling() -> None:
    """The narrower predicate must not have silenced the question everywhere."""
    draft = json.loads(json.dumps(_BARE_DRAFT))
    draft["connection_slots"] = [{"name": "model", "purpose": "reasoning"}]
    for node in draft["nodes"]:
        if node["id"] == "publish":
            node["connection_slot"] = "model"

    keys = {q.key for q in interview(draft)}

    assert "spend:publish" in keys, "a slot-bound node reports usage and must be asked"
    question = next(q for q in interview(draft) if q.key == "spend:publish")
    assert "PAUSES" in question.why
    assert "cannot report usage is not asked this" in question.why, (
        "the question promises a pause without saying when that promise holds"
    )


def test_repair_without_a_budget_is_a_MUST_ASK() -> None:
    draft = {
        "graph_id": "r",
        "nodes": [{"id": "a", "kind": "loop", "effects": [], "on_failure": "repair"}],
        "policies": {},
    }

    assert "repair_budget" in interview_document(draft)["must_ask"]


def test_every_question_states_the_STAKE_not_just_the_field() -> None:
    """A person deciding needs to know what happens either way, not the schema path."""
    for question in interview(_BARE_DRAFT):
        assert len(question.why) > 60, question.key
        assert question.pointer.startswith("/"), question.key
        assert question.ask.endswith("?"), question.key
        assert question.weight in {"high", "medium", "low"}


def test_high_questions_come_first(shipped: dict) -> None:
    weights = [q.weight for q in interview(_BARE_DRAFT)]
    order = {"high": 0, "medium": 1, "low": 2}

    assert weights == sorted(weights, key=lambda w: order[w])


def test_the_guidance_forbids_answering_a_high_question_for_someone() -> None:
    """The whole point is that a human decides the consequential ones."""
    document = interview_document(_BARE_DRAFT)

    assert "Never answer a HIGH question on someone's behalf" in document["how_to_use"]


def test_the_document_is_json_safe() -> None:
    document = interview_document(_BARE_DRAFT)

    assert json.loads(json.dumps(document)) == document


def test_a_manifest_that_is_not_a_graph_yields_no_questions_rather_than_crashing() -> None:
    """It runs on drafts the compiler would refuse — that is when it is needed most."""
    assert interview({}) == () or all(q.node_id is None for q in interview({}))
    assert interview({"nodes": "not a list", "policies": None}) is not None
