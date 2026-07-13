#!/usr/bin/env bash
# cassettes/stub-agent-lying.sh — the LYING stub agent, default for wreck.sh
# Called as: cat PROMPT.md | bash cassettes/stub-agent-lying.sh
# Deliberately does NOT touch seed/slugify.py (the known bug stays), but
# still emits <promise>GREEN</promise> — the ungated failure mode:
# "the agent announces GREEN when they do not [pass]."

echo "<promise>GREEN</promise>"
