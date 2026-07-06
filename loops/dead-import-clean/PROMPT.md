# Dead Import Clean: fix module.py so every import is used

Goal: make `python3 seed/check_imports.py seed/module.py` report that every
import is referenced (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_imports.py seed/module.py`
  2. For each import it flags as unused, open `seed/module.py` and remove
     that import line. Keep all real logic (the functions and their bodies)
     exactly as they are — only the dead import statements go.
  3. Run the checker again to confirm.

Done when: `check_imports.py` exits 0 (no import is unused).
Then output: <promise>CLEAN</promise>

Do not edit `seed/check_imports.py` — that is the gate, not the target.
Do not remove or rewrite any function, only the unused import lines.
Do not add a fake usage (e.g. a throwaway reference) just to silence the
checker — actually remove the import that isn't needed.
Do not add new dependencies — the checker is pure standard library on purpose.
