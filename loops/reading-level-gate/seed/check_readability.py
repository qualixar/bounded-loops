#!/usr/bin/env python3
"""
check_readability.py — a keyless "is this prose too dense?" gate.

Computes the average words-per-sentence across a post's prose, as a cheap
proxy for run-on-sentence complexity, and fails if the average exceeds a
threshold. This is not a full readability formula (no Flesch-Kincaid
syllable counting) — it is a deliberately simple, dependency-free
approximation: long average sentence length correlates strongly with
reader drop-off and comprehension loss.

Pure Python standard library: no network, no API key, no external tool.
It runs anywhere Python does.

A "sentence" is any run of text ending in '.', '!', or '?'. Markdown
headings (lines starting with '#') are excluded from the count, since they
are titles, not prose.

Exit code: 0 = average words-per-sentence <= 25 (gate passes), 1 = average
exceeds 25 (gate fails), 2 = could not run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_AVG_WORDS_PER_SENTENCE = 25

#: Fewest sentences over which this gate is willing to call an average a reading level. Three is
#: not a statistical threshold — no threshold makes a tiny sample informative — it is a floor that
#: stops the most misleading kind of pass, where a post replaced by one short line scores well
#: because there is nothing left to score.
_MIN_SENTENCES_TO_JUDGE = 3

_SENTENCE_END_RE = re.compile(r"[.!?]+")


def _prose_lines(text: str) -> list[str]:
    """Lines of the post that are prose (skip markdown headings and blanks)."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _sentences(prose: str) -> list[str]:
    """Split prose into sentences on '.', '!', '?'; drop empty fragments."""
    raw = _SENTENCE_END_RE.split(prose)
    return [s.strip() for s in raw if s.strip()]


def check(post_path: str) -> int:
    try:
        text = Path(post_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"check_readability: cannot run: {exc}", file=sys.stderr)
        return 2

    prose = " ".join(_prose_lines(text))
    sentences = _sentences(prose)
    if not sentences:
        print(f"check_readability: no prose sentences found in {post_path}", file=sys.stderr)
        return 2

    # An average over one or two sentences is not an average. The held-out mutant corpus replaced
    # the whole post with "lorem ipsum dolor sit amet" — a single five-word sentence — and this
    # gate passed it, because five is comfortably under the limit. The arithmetic was right and
    # the judgement was empty: there was no prose left to assess.
    #
    # This is a MINIMUM-EVIDENCE rule, the same discipline gate_metrics applies when it refuses to
    # report a rate from too small a sample. Below this floor the honest answer is "cannot judge",
    # not "passes".
    if len(sentences) < _MIN_SENTENCES_TO_JUDGE:
        print(
            f"check_readability: only {len(sentences)} sentence(s) found — too little prose to "
            f"assess reading level. An average over fewer than {_MIN_SENTENCES_TO_JUDGE} sentences "
            "is not a reading level; shorten the sentences, do not delete the post."
        )
        return 1

    word_counts = [len(s.split()) for s in sentences]
    total_words = sum(word_counts)
    average = total_words / len(sentences)

    if average > MAX_AVG_WORDS_PER_SENTENCE:
        print(
            f"check_readability: average words/sentence is {average:.1f}, "
            f"exceeds the {MAX_AVG_WORDS_PER_SENTENCE} limit "
            f"({len(sentences)} sentences, {total_words} words)"
        )
        return 1

    print(
        f"check_readability: average words/sentence is {average:.1f}, "
        f"within the {MAX_AVG_WORDS_PER_SENTENCE} limit"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_readability.py <post.md>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
