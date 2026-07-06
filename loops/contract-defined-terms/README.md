# contract-defined-terms

**Role:** legal · **Rung:** L2 · **Gate:** `command` (keyless) · **Runner:** stub (keyless)

A bounded loop that drives an agent until **every bolded defined term used
in a contract's body is actually defined**. This is the runnable form of a
common drafting defect: a contract reuses the visual convention of a
defined term (bold text) for a phrase that was never added to the
Definitions section, leaving its meaning implicit and disputable.

## What it demonstrates

The seed `contract.md` bolds `**Effective Date**` three times in the body
(in the preamble, Scope of Services, and Term sections) but the
Definitions section only defines `Agreement`, `Services`, and
`Confidential Information` — `Effective Date` is missing.

The gate `seed/check_defined.py` extracts every `**Term**` used outside
the Definitions section and every term actually defined under
Definitions, then flags any bolded term used but not defined. The loop is
DONE only when every bolded body term has a matching definition.

## Run it (keyless, ~1s)

```bash
bl run loops/contract-defined-terms --yes   # stub runner + real command gate
```

You'll see the contract fail the checker on `Effective Date`, the
recorded fix add the missing definition, then the gate pass.

## Why the checker is `forbid`-protected

The whole point is that the agent conforms the *document* to the
requirement — add the missing definition — not silently unbold the term
to dodge detection or edit the checker to fake a pass. The engine refuses
any write to `seed/check_defined.py`.

## Make it real

Swap the stub runner for a real agent and extend `check_defined.py` to
handle additional drafting styles (e.g. `_Term_` italics, ALL CAPS
defined terms, or a schedule of defined terms in a separate exhibit).
Keep the gate as the bottleneck: a contract is never "done" until every
defined term it uses is independently confirmed to have a definition.
