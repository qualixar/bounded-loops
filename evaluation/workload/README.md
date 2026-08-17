# E9 workload loops

Parameterised loops for the bound-utilisation experiment. `generate.py` materialises one; nothing
here is part of the `loops/` catalogue, which is frozen as the alpha corpus for E6/E7.

    python3 generate.py --dest /tmp/w --records 8 --policy one_per_lap
    bl run /tmp/w --yes

Policies: `complete`, `fraction`, `one_per_lap`, `stalled`, `stall_after`.

## Observed, 2026-08-17

| Condition | Predicted | Observed |
|---|---|---|
| 8 records, `one_per_lap`, `max_iterations: 10` | DONE at lap 8 | DONE at lap 8 — utilisation 0.8 |
| 8 records, `stalled`, `no_progress_window: 3` | HALT at lap 3, "no progress" | **HALT at lap 11, `max_iterations` reached** |

The second row is a defect in the engine, not in this generator. See `docs/` and the audit ledger.
