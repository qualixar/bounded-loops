# Accessibility Lint: fix the static a11y violations

Goal: make `python3 seed/a11y_lint.py seed/index.html` report zero
accessibility violations (exit 0).

Steps each turn:
  1. Run: `python3 seed/a11y_lint.py seed/index.html`
  2. If it fails, read each violation and edit `seed/index.html` to fix it:
     - A1: add an `alt` attribute to every `<img>`
     - A2: add a `lang` attribute to the `<html>` element
     - A3: give every `<input>` a matching `<label for=...>` or an `aria-label`
  3. Run the linter again to confirm.

Done when: `a11y_lint.py` exits 0 (no static accessibility violations).

Do not delete the `<img>`, `<input>`, or `<html>` elements.
Do not edit `seed/a11y_lint.py` (that is the gate, not the target).
Do not add new dependencies — the linter is pure standard library on purpose.
