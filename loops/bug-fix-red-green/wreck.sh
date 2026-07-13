#!/usr/bin/env bash
# wreck.sh — the UNGATED loop. Same spec, no independent gate.
# Counterexample: delete the independent gate.
#
# Observable failure modes (all three now actually demonstrated):
#   LIE       — DEFAULT. The agent claims <promise>GREEN</promise> while
#               seed/slugify.py is still buggy. With no gate, the loop
#               trusts the claim and exits "successfully" — a false positive.
#   DRIFT     — with a real misbehaving agent, unrequested changes accumulate
#               (no external pull-back to stop them).
#   OVERRUN   — if the agent never claims done, the loop runs all MAX_ITER
#               laps with no early exit.
#
# Requirements: bash 3.2+, Python 3.11+, pytest (used only by the diagnostic
# epilogue below — the wrecked loop itself never calls pytest, that's the point).

set -euo pipefail

LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_DIR="$LOOP_DIR/seed"
MAX_ITER=15

# Precondition check (same rationale as run.sh): the
# diagnostic epilogue below needs pytest to make the lie's consequence
# observable — fail fast with a clear message rather than a confusing crash.
if ! command -v pytest >/dev/null 2>&1; then
    echo "error: 'pytest' not found on PATH." >&2
    echo "       Install it first: pip install pytest" >&2
    exit 2
fi

# Default: the LYING stub — leaves the bug in place, claims GREEN anyway.
AGENT_CLI="${AGENT_CLI:-bash $LOOP_DIR/cassettes/stub-agent-lying.sh}"

echo "=== bug-fix-red-green — WRECKED (no gate) ==="
echo "WARNING: this loop trusts the agent's own claim — it never checks pytest."
echo ""

i=0
cd "$SEED_DIR"
claimed_done=0

while [ "$i" -lt "$MAX_ITER" ]; do
    i=$((i + 1))
    echo "--- Lap $i (no gate) ---"

    # Direct stdin redirection (same fix as run.sh) — avoids a SIGPIPE/
    # pipefail crash when AGENT_CLI doesn't drain stdin, e.g. this
    # loop's own OVERRUN test using AGENT_CLI="echo 'still working'".
    output="$($AGENT_CLI < "$LOOP_DIR/PROMPT.md")"
    echo "$output"

    # THE GATE LINE IS MISSING. Only the agent's own claim is checked here —
    # this IS the naive "believe the agent" failure this demo warns against.
    if echo "$output" | grep -q '<promise>GREEN</promise>'; then
        echo ""
        echo "LOOP BELIEVES: agent claimed GREEN on lap $i — exiting (no gate confirmed this)."
        claimed_done=1
        break
    fi
done

echo ""
if [ "$claimed_done" -eq 1 ]; then
    echo "--- Diagnostic epilogue (NOT part of the wrecked loop's own logic — it has no gate) ---"
    if pytest_output="$(pytest -q 2>&1)"; then
        # Machine-parseable mode line so CI/automation can
        # distinguish the failure modes without scraping human prose.
        echo "WRECK_MODE=LIE_TRUE"
        echo "pytest actually passes — the claim happened to be true this time."
        exit 0
    else
        echo "WRECK_MODE=LIE"
        echo "LIE CONFIRMED: pytest still FAILS, but the ungated loop already exited claiming success."
        echo "Nothing catches the lie, so the ungated loop exits on a false promise."
        echo "$pytest_output"
        exit 1
    fi
fi

echo "WRECK_MODE=OVERRUN"
echo "OVERRUN — ran all $MAX_ITER laps without any GREEN claim. No gate, no early exit."
exit 1
