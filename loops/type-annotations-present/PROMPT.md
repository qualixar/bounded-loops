# Type Annotations Present: fix api.py so every public function is annotated

Goal: make `python3 seed/check_types.py seed/api.py` report that every
public top-level function is fully annotated (exit 0).

Steps each turn:
  1. Run the checker:
     `python3 seed/check_types.py seed/api.py`
  2. For each function it flags, open `seed/api.py` and add the missing
     parameter and/or return type annotations. Use the correct, honest type
     for each parameter based on how it's actually used in the function body
     (do not annotate everything `Any` as a shortcut).
  3. Run the checker again to confirm.

Done when: `check_types.py` exits 0 (every public top-level function
annotates every parameter and its return type). Private functions (name
starting with `_`) are out of scope and may stay unannotated.
Then output: <promise>ANNOTATED</promise>

Do not edit `seed/check_types.py` — that is the gate, not the target.
Do not rename the public function to start with `_` as a shortcut to dodge
the checker; add the real annotations instead.
Do not add new dependencies — the checker is pure standard library on purpose.
