# Why Bounded Loops Beat One-Shot Prompts

Bounded loops work.

When a team first adopts an agent-driven workflow they often assume that a single well-crafted prompt sent once to a capable model will reliably produce a finished, contract-conforming artifact on the very first try, and it is only after watching that single-shot output fail a real gate again and again in production that they realize the missing ingredient was never a better prompt at all but rather a bounded, repeatable loop that keeps handing the same real verifier the agent's latest attempt until it genuinely passes, capped by iteration count, wallclock time, and token budget so it can never run away unattended and unaccountable forever.

The fix is simple: wrap the agent in a loop with a real gate.

Once that gate is in place, the agent gets to try again, and again, and again, each time incorporating the concrete, specific, line-numbered feedback from the previous failing run rather than starting cold from the original ambiguous prompt, which is exactly why evaluator-optimizer loops so reliably outperform any single unchecked pass on tasks that have a crisp, checkable definition of done, whether that definition of done lives in a JSON Schema contract, a command-line linter, or a small pure-standard-library script written specifically for the one property that actually matters to ship safely.

That is the whole idea.
