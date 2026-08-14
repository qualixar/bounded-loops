"""The configuration interview: what an orchestrator should ASK before it assumes.

The problem this solves, in the user's words: *"If someone does not want to go under the UI and put
configuration in every node, we should look for the easiest way of working… people should not
struggle."*

A graph has around forty authorable fields. Making a non-technical person click through all of them
is a bad product, and letting an agent silently pick defaults for the dangerous ones is worse — the
defaults that matter are exactly the ones a human should have been asked about. So: the agent
interviews the person, in plain language, and writes the answers into the graph.

**The questions are derived, not written.** Same decision as the schema-driven forms, for the same
reason: a hand-written interview script drifts from the compiler the moment a field is added, and
nobody notices because nothing fails. Here the questions come from the authoring schema plus the
facts that make a field consequential — a node that declares an irreversible effect gets asked
about approval, because the engine treats that effect as authority.

Ordered by consequence, not by schema order. A person answering three questions should have
answered the three that matter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from bounded_loops.graph.domain.authoring import NETWORK_EFFECTS, Effect


@dataclass(frozen=True)
class Question:
    """One thing to ask a human, and what to do with the answer."""

    key: str
    #: The node this concerns, or None for a graph-level question.
    node_id: str | None
    #: Plain language. No jargon that the `why` does not immediately explain.
    ask: str
    #: Why it is being asked — a person deciding needs the stake, not the field name.
    why: str
    #: Where the answer is written, as a JSON pointer into the manifest.
    pointer: str
    #: Closed answers, when the field is closed. Empty for free values.
    options: tuple[str, ...] = ()
    #: What the engine does if nobody answers. Stated so the agent can say it out loud.
    default: str | None = None
    #: HIGH questions must be asked. LOW ones can be skipped silently on a first pass.
    weight: str = "medium"


_HIGH = "high"
_MEDIUM = "medium"
_LOW = "low"

_EFFECTFUL = frozenset(effect.value for effect in NETWORK_EFFECTS)
_IRREVERSIBLE = frozenset({Effect.IRREVERSIBLE.value, Effect.FINANCIAL.value})


def interview(manifest: Mapping[str, Any]) -> tuple[Question, ...]:
    """The questions worth asking about this specific graph, most consequential first.

    Takes a parsed manifest (or a draft dict) rather than a validated graph, so it can run on
    something the compiler would still refuse — which is exactly when an agent needs to know what
    to ask.
    """
    questions: list[Question] = []
    nodes = manifest.get("nodes")
    node_list: Sequence[Mapping[str, Any]] = (
        [node for node in nodes if isinstance(node, Mapping)] if isinstance(nodes, list) else []
    )
    raw_policies = manifest.get("policies")
    policies: Mapping[str, Any] = raw_policies if isinstance(raw_policies, Mapping) else {}

    questions.extend(_effect_questions(node_list))
    questions.extend(_approval_questions(node_list))
    questions.extend(_spend_questions(node_list))
    questions.extend(_failure_questions(node_list, policies))
    questions.extend(_isolation_questions(node_list))
    questions.extend(_data_class_question(policies))

    order = {_HIGH: 0, _MEDIUM: 1, _LOW: 2}
    return tuple(sorted(questions, key=lambda item: (order[item.weight], item.key)))


def interview_document(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """JSON-ready, for the MCP tool and the monitor."""
    questions = interview(manifest)
    return {
        "questions": [
            {
                "key": question.key,
                "node_id": question.node_id,
                "ask": question.ask,
                "why": question.why,
                "pointer": question.pointer,
                "options": list(question.options),
                "default": question.default,
                "weight": question.weight,
            }
            for question in questions
        ],
        "must_ask": [q.key for q in questions if q.weight == _HIGH],
        "how_to_use": (
            "Ask the HIGH questions before running anything — each one concerns authority the "
            "engine will actually grant or a bound it will actually enforce. Ask MEDIUM ones when "
            "the person has patience. Never answer a HIGH question on someone's behalf and then "
            "tell them the graph is ready: say which defaults you applied."
        ),
    }


# ── question families ────────────────────────────────────────────────────────


def _effect_questions(nodes: Sequence[Mapping[str, Any]]) -> list[Question]:
    """A node with no declared effects is the easiest thing to get wrong in both directions."""
    questions = []
    for index, node in enumerate(nodes):
        node_id = str(node.get("id", f"#{index}"))
        # ABSENT is unanswered; EMPTY is an answer. `effects: []` says "grant this node nothing",
        # which is a deliberate and common choice — every shipped reference graph makes it. Asking
        # about it turned a correct graph into thirteen questions, which is the exact struggling
        # this module exists to prevent.
        if "effects" in node:
            continue
        questions.append(
            Question(
                key=f"effects:{node_id}",
                node_id=node_id,
                ask=(
                    f"What does '{node_id}' actually change? Nothing outside its own workspace, "
                    "files in this project, something outside this machine, money, or something "
                    "that cannot be undone?"
                ),
                why=(
                    "A declared effect is a grant of authority: the engine uses it to decide "
                    "whether the node may retry, what isolation it needs, and whether a human has "
                    "to approve it. Declaring nothing means the node is granted nothing — safe, "
                    "but it will be refused if it then tries to reach out."
                ),
                pointer=f"/nodes/{index}/effects",
                options=tuple(effect.value for effect in Effect),
                default="[] — no authority granted",
                weight=_HIGH,
            )
        )
    return questions


def _approval_questions(nodes: Sequence[Mapping[str, Any]]) -> list[Question]:
    """An irreversible effect with no approval node in front of it is the headline risk."""
    has_approval = any(node.get("kind") == "approval" for node in nodes)
    questions = []
    for index, node in enumerate(nodes):
        effects = node.get("effects")
        declared = {str(value) for value in effects} if isinstance(effects, list) else set()
        if not declared & _IRREVERSIBLE:
            continue
        node_id = str(node.get("id", f"#{index}"))
        if not has_approval:
            questions.append(
                Question(
                    key=f"approval:{node_id}",
                    node_id=node_id,
                    ask=(
                        f"'{node_id}' declares something irreversible. Should a person approve it "
                        "before it happens?"
                    ),
                    why=(
                        "There is no approval node in this graph, so nothing pauses for a human. "
                        "An irreversible effect that runs unattended cannot be taken back, and the "
                        "receipt will show it was never authorised by anyone."
                    ),
                    pointer="/nodes",
                    options=("yes — add an approval node", "no — run it unattended"),
                    default="no — nothing will pause",
                    weight=_HIGH,
                )
            )
    for index, node in enumerate(nodes):
        if node.get("kind") != "approval" or node.get("required_role"):
            continue
        node_id = str(node.get("id", f"#{index}"))
        questions.append(
            Question(
                key=f"required_role:{node_id}",
                node_id=node_id,
                ask=f"Who is allowed to approve '{node_id}'?",
                why=(
                    "The role is recorded on the approval, so it is the evidence of who had the "
                    "authority. An approval anyone can grant is not much of a gate."
                ),
                pointer=f"/nodes/{index}/required_role",
                weight=_HIGH,
            )
        )
    return questions


def _spend_questions(nodes: Sequence[Mapping[str, Any]]) -> list[Question]:
    """A missing ceiling is not a small ceiling; it is no ceiling."""
    questions = []
    for index, node in enumerate(nodes):
        raw_budget = node.get("budget")
        budget: Mapping[str, Any] = raw_budget if isinstance(raw_budget, Mapping) else {}
        node_id = str(node.get("id", f"#{index}"))
        # Two independent questions live in this loop and must not share a gate. "What may this
        # cost?" applies only where usage is metered; "is repeating this effect safe?" applies
        # wherever the node reaches outside, metered or not. They were behind one `continue`,
        # so narrowing the spend predicate silently swallowed a HIGH must-ask about retrying an
        # irreversible effect — caught by its own test, which is the only reason it is not in
        # this release.
        if _can_spend(node) and (
            budget.get("max_tokens") is None and budget.get("max_cost_microunits") is None
        ):
            questions.append(
                Question(
                    key=f"spend:{node_id}",
                    node_id=node_id,
                    ask=f"How much should '{node_id}' be allowed to spend before it stops?",
                    why=(
                        "With no token or cost ceiling the node has none, and a run that goes "
                        "wrong keeps going. This node reports its usage through a connection "
                        "slot, so when a ceiling is reached the run PAUSES rather than failing "
                        "and you can raise it deliberately and resume. A node whose worker "
                        "cannot report usage is not asked this, because a ceiling it could "
                        "never measure would fail the run instead of pausing it."
                    ),
                    pointer=f"/nodes/{index}/budget",
                    default="no ceiling",
                    weight=_MEDIUM,
                )
            )
        if budget.get("max_attempts") in (None, 1):
            continue
        effects = node.get("effects")
        declared = {str(value) for value in effects} if isinstance(effects, list) else set()
        if declared & _EFFECTFUL:
            questions.append(
                Question(
                    key=f"retry:{node_id}",
                    node_id=node_id,
                    ask=(
                        f"'{node_id}' may retry and it reaches outside this machine. Is repeating "
                        "its effect safe?"
                    ),
                    why=(
                        "Retrying a node that already sent something sends it twice unless the "
                        "effect carries an idempotency key. The compiler refuses this combination "
                        "outright, so it must be resolved before the graph will run at all."
                    ),
                    pointer=f"/nodes/{index}/budget/max_attempts",
                    options=("safe — it has an idempotency key", "not safe — set max_attempts to 1"),
                    weight=_HIGH,
                )
            )
    return questions


def _can_spend(node: Mapping[str, Any]) -> bool:
    """Whether this node's spend can be METERED — which is a stricter question than whether it
    can have consequences.

    Only a connection slot routes to a provider that reports usage, so only a slot-bound node
    can have a ceiling that ever fires. This used to also return True for any network-bearing
    effect, and that conflated two different things: `external_write` says the node changes
    something outside, not that it consumes tokens. Writing a file costs nothing.

    The consequence was not cosmetic. `publish-instruction` on every shipped reference graph
    declares `external_write` and binds no slot, so the interview asked it for a ceiling — and
    setting one turns a working keyless graph into `node.failed / budget_unmeasurable`, because
    the runtime correctly refuses a budget no worker can report. The single question the
    interview asked about these graphs was one whose only answer broke them.
    """
    return bool(node.get("connection_slot"))


def _failure_questions(
    nodes: Sequence[Mapping[str, Any]], policies: Mapping[str, Any],
) -> list[Question]:
    questions = []
    if policies.get("fail_mode") is None:
        questions.append(
            Question(
                key="fail_mode",
                node_id=None,
                ask="If one step fails, should the whole run stop, or should the rest continue?",
                why=(
                    "Stopping at the first failure is the safe default and it makes the receipt "
                    "unambiguous. Continuing is useful when the steps are genuinely independent, "
                    "but then a run can finish with some work undone."
                ),
                pointer="/policies/fail_mode",
                options=("fail_closed", "continue_declared"),
                default="fail_closed",
                weight=_MEDIUM,
            )
        )
    wants_repair = any(
        isinstance(node.get("on_failure"), Mapping)
        or node.get("on_failure") == "repair"
        for node in nodes
    )
    if wants_repair and not policies.get("repair_budget"):
        questions.append(
            Question(
                key="repair_budget",
                node_id=None,
                ask="How many times may the run go back and repair earlier work before giving up?",
                why=(
                    "A node asks for repair but the graph sets no bound, and the compiler refuses "
                    "that — the bound is what makes repair terminate. It is global across the whole "
                    "run, not per node."
                ),
                pointer="/policies/repair_budget",
                default="0 — refused, because repair is requested",
                weight=_HIGH,
            )
        )
    return questions


def _isolation_questions(nodes: Sequence[Mapping[str, Any]]) -> list[Question]:
    questions = []
    for index, node in enumerate(nodes):
        if node.get("isolation"):
            continue
        node_id = str(node.get("id", f"#{index}"))
        questions.append(
            Question(
                key=f"isolation:{node_id}",
                node_id=node_id,
                ask=f"How tightly should '{node_id}' be confined while it runs?",
                why=(
                    "The tiers differ in what is actually enforced, and a tier this machine cannot "
                    "deliver is refused rather than quietly downgraded. Ask for what the work needs; "
                    "`bl capabilities` shows what this host can enforce."
                ),
                pointer=f"/nodes/{index}/isolation",
                options=("workspace_only", "process_restricted", "container_restricted"),
                default="workspace_only",
                weight=_LOW,
            )
        )
    return questions


def _data_class_question(policies: Mapping[str, Any]) -> list[Question]:
    if policies.get("data_class"):
        return []
    return [
        Question(
            key="data_class",
            node_id=None,
            ask="How sensitive is the data this graph touches?",
            why=(
                "The data class constrains which connections a node may use, so declaring it too "
                "low is how confidential data reaches somewhere it should not."
            ),
            pointer="/policies/data_class",
            options=("public", "internal", "confidential", "restricted"),
            default="internal",
            weight=_MEDIUM,
        )
    ]
