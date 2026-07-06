#!/usr/bin/env bash
# cassettes/stub-agent.sh — the stub agent for standalone run.sh
# Called as: cat PROMPT.md | bash cassettes/stub-agent.sh
# Reads the spec from stdin (ignored; the fix is deterministic).
# Writes the fixed slugify.py to the current directory (cwd = seed/).
# Outputs <promise>GREEN</promise> to stdout.

cat > slugify.py << 'PYEOF'
# seed/slugify.py — fixed by stub agent
import re


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")
PYEOF

echo "<promise>GREEN</promise>"
