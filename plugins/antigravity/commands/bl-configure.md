---
description: Interview the user about a graph's consequential settings, then write the answers into it
argument-hint: "<saved graph name>"
---

Configure the saved graph named in $ARGUMENTS by ASKING the user, not by guessing.

A graph has around forty authorable fields. The ones that matter are exactly the
ones you must not quietly default: whether a person approves an irreversible
effect, what a node may spend, whether a retry would send the same thing twice.
Your job here is to ask about those in plain language and write down the answers.

Steps:

1. **Get the questions** — call `graph_interview(name="$ARGUMENTS")`. It returns
   questions ordered by consequence, each with the stake (`why`), where the
   answer goes (`pointer`), the closed set of valid answers (`options`), and what
   the engine does if nobody answers (`default`).

   Do not write your own question list. These are derived from the graph in front
   of you plus the schema the compiler actually enforces, so they cannot drift
   from it. A hand-written script goes stale the moment a field is added, and
   nothing fails to tell you.

2. **Ask the `must_ask` questions** — every key in `must_ask` (weight `high`).
   Ask them in your own words, one at a time, and include the `why`. A person
   deciding needs the stake, not the field name.

   If the user says "just pick sensible ones", you may apply defaults — but then
   say, explicitly and per field, which defaults you applied and what each one
   means. "Configured with defaults" is not an acceptable summary of having
   granted or withheld authority on someone's behalf.

3. **Ask the `medium` questions if the user has patience.** Offer, don't insist:
   *"There are N more about spend ceilings and failure handling — want to go
   through them, or take the defaults?"* Skip `low` unless asked.

4. **Preview the change** — collect answers into
   `changes=[{"pointer": <from the question>, "value": <the answer>}]` and call
   `graph_configure(name="$ARGUMENTS", changes=[...], confirm=false)`.

   Use the `pointer` from the question verbatim. Do not construct pointers
   yourself: a pointer you invented can silently write to the wrong node.

5. **Show the diff and the lint result**, then call again with `confirm=true`.
   If lint fails, the graph is NOT written — report which answer caused the
   refusal, look the code up in `plugins/shared/docs/refusal-reference.md`, and
   ask again for that field only.

6. **Report what is still unanswered.** If any `must_ask` question went
   unanswered, say so by name and say what the engine will therefore do. Do not
   describe the graph as configured or ready.

Two things this command must never do:

- **Never answer a HIGH question silently.** The whole point is that a human
  decided. A receipt showing an approval nobody was asked for is worse than no
  receipt.
- **Never run the graph.** Configuring is not approving and not executing. Use
  `/bl-graph` to plan and let the user say "run it".

Empty question list is a good outcome, not a bug: it means the graph already
declares everything consequential. Say so plainly and stop — a graph that
declares `effects: []` has answered that question, and re-asking it is the
struggling this command exists to prevent.
