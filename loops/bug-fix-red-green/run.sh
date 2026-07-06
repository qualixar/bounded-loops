#!/usr/bin/env bash
# run.sh — standalone bounded loop for bug-fix-red-green
# Reproduces the course §4 bash runner. NO engine required.
#
# Usage:
#   ./run.sh                          # uses stub agent (prints fixed slugify.py)
#   AGENT_CLI="claude -p" ./run.sh    # plug in any agent CLI
#   AGENT_CLI="codex exec" ./run.sh   # same, with Codex
#
# The stub agent (default) writes the correct slugify.py to seed/slugify.py,
# making pytest pass on the first lap. Change it to a real agent to see the
# full loop iterate.
#
# Requirements: bash 3.2+, Python 3.11+, pytest (pip install pytest)

set -euo pipefail

LOOP_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_DIR="$LOOP_DIR/seed"
MAX_ITER=15

# Precondition check: a missing pytest previously produced
# 15 silently-failing loop iterations that surfaced as a misleading
# "HALT — max iterations reached", making the user debug the wrong thing.
# Fail fast with a clear, actionable message instead.
if ! command -v pytest >/dev/null 2>&1; then
    echo "error: 'pytest' not found on PATH." >&2
    echo "       Install it first: pip install pytest" >&2
    exit 2
fi

# Default: stub agent that emits the fixed slugify.py.
# Override by setting AGENT_CLI in environment.
AGENT_CLI="${AGENT_CLI:-bash $LOOP_DIR/cassettes/stub-agent.sh}"

echo "=== bug-fix-red-green — standalone run ==="
echo "Workspace : $SEED_DIR"
echo "Agent     : $AGENT_CLI"
echo "Max laps  : $MAX_ITER"
echo ""

i=0
cd "$SEED_DIR"

while [ "$i" -lt "$MAX_ITER" ]; do
    i=$((i + 1))
    echo "--- Lap $i ---"

    # Feed the spec to the agent.
    # Real agents: the agent reads PROMPT.md, edits seed/slugify.py, and exits.
    # Stub agent: writes the fixed file directly (keyless, deterministic).
    #
    # Direct stdin redirection, not a `cat | $AGENT_CLI` pipe: under
    # pipefail, an AGENT_CLI that doesn't fully drain stdin (e.g. `echo`)
    # would make cat's write raise SIGPIPE, aborting the script (exit 141)
    # before it reaches any of its own logic. This form has no upstream
    # writer process, so it's safe for any AGENT_CLI regardless of
    # whether it reads stdin at all.
    $AGENT_CLI < "$LOOP_DIR/PROMPT.md"

    # GATE — independent of the agent's opinion.
    # The agent cannot talk its way past a failing pytest.
    if pytest -q; then
        echo ""
        echo "GREEN — gate passed on lap $i"
        exit 0
    fi

    echo "Gate failed. Looping..."
done

echo "HALT — max iterations ($MAX_ITER) reached without green gate"
exit 1
