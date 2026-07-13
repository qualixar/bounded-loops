# convergence-demo

**Role:** engineering · reliability · **Rung:** L1 · **Gate:** pytest ·
**Runner:** deterministic stub

This loop makes the bounds visible. Its cassette applies a wrong fix on lap 1,
a partial fix on lap 2, and the correct fix on lap 3. The independent pytest
gate runs after every lap, so the ledger records two failures before DONE.

## Watch convergence

```bash
bl run loops/convergence-demo --yes
```

Expected terminal line:

```text
✓ [DONE] gate-passed (laps: 3)
```

## Watch the bound stop it

```bash
bl run loops/convergence-demo --max-iterations 2 --yes
```

Expected terminal line:

```text
✗ [HALT] max_iterations 2 reached at lap 3 (laps: 3)
```

The third ledger entry is the bound decision made before a third agent turn;
the correct fix is never applied.

## Make it real

Swap the deterministic runner for a supported agent runner. Preserve the tests
as the protected gate anchor and keep the same bounds so a real agent receives
the same independent verdict and stop conditions.
