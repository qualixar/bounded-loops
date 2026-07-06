# seed/slugify.py  — BUGGY (the target the agent must fix)
# Python 3.11+
import re


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug.

    Known bug: consecutive spaces produce consecutive hyphens.
    The agent's job is to fix this so the test passes.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = text.replace(" ", "-")  # BUG: should collapse runs of spaces first
    return text.strip("-")
