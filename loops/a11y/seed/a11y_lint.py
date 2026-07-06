#!/usr/bin/env python3
"""
a11y_lint.py — a tiny, dependency-free static accessibility linter.

Checks the STATICALLY-detectable subset of WCAG in an HTML file using only the
Python standard library (html.parser) — no browser, no WebDriver, no npm, no
external tool, no API key. It runs anywhere Python does, which is the whole
point: a genuinely keyless, clone-and-run accessibility gate.

HONEST LIMITATION (documented, not hidden — same disclosure discipline as the
rest of this repo): a static linter cannot catch rendered-DOM issues — computed
colour contrast, focus order, live ARIA state, reading order. For those you
still need a DOM-based tool (axe-core, Lighthouse) that requires a running
browser, deliberately out of scope for this keyless loop. This checks the
high-value static rules a browser-based tool would ALSO flag but that you can
catch at author time with zero setup:

  A1  every <img> must have an `alt` attribute (an empty alt="" is allowed for
      decorative images; a MISSING alt is the violation)
  A2  the root <html> element must declare a `lang`
  A3  every labelable <input> must be associated with a <label for=...> or
      carry an `aria-label` (an input nobody can label is invisible to a
      screen reader)

Exit code: 0 = clean (gate passes), 1 = violations found (gate fails),
2 = could not run (bad args / unreadable file).
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser

_LABELABLE_EXEMPT_TYPES = {"hidden", "submit", "button", "reset", "image"}


class _A11yParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.violations: list[str] = []
        self.label_for_targets: set[str] = set()
        self.inputs: list[tuple[int, dict[str, str]]] = []
        self.html_seen = False
        self.html_has_lang = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "img":
            if "alt" not in a:
                self.violations.append(
                    f"line {line}: <img> is missing an `alt` attribute (A1)"
                )
        elif tag == "html":
            self.html_seen = True
            if a.get("lang", "").strip():
                self.html_has_lang = True
        elif tag == "label":
            target = a.get("for", "").strip()
            if target:
                self.label_for_targets.add(target)
        elif tag == "input":
            self.inputs.append((line, a))

    def finalize(self) -> None:
        if not self.html_seen:
            self.violations.append("no <html> element found (A2)")
        elif not self.html_has_lang:
            self.violations.append("<html> is missing a `lang` attribute (A2)")
        for line, a in self.inputs:
            itype = a.get("type", "text").lower()
            if itype in _LABELABLE_EXEMPT_TYPES:
                continue
            has_label = a.get("id", "").strip() in self.label_for_targets
            has_aria = bool(a.get("aria-label", "").strip())
            if not has_label and not has_aria:
                self.violations.append(
                    f"line {line}: <input type={itype!r}> has no associated "
                    "<label for=...> and no aria-label (A3)"
                )


def lint(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
    except OSError as exc:
        print(f"a11y_lint: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    parser = _A11yParser()
    parser.feed(html)
    parser.finalize()
    if parser.violations:
        print(f"a11y_lint: {len(parser.violations)} accessibility violation(s) in {path}:")
        for v in parser.violations:
            print(f"  - {v}")
        return 1
    print(f"a11y_lint: no static accessibility violations in {path}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: a11y_lint.py <file.html>", file=sys.stderr)
        return 2
    return lint(argv[1])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
