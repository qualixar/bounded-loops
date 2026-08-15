# Who approved it — and how much that answer is worth

At every other point in a run, a completion claim is backed by an **independent gate**. At an
approval checkpoint it is backed by a **person**. So the receipt has to be able to say which
person, and has to be honest about how much that name proves.

## Two fields, because there are two questions

```json
{
  "node_id": "publish-release",
  "actor_id": "local-org",
  "decided_by": "priya",
  "decided_by_source": "configured"
}
```

| Field | Question | On a local run |
|---|---|---|
| `actor_id` | **who was permitted** — the authorization subject | always the tenant |
| `decided_by` | **who decided** | a person |
| `decided_by_source` | where that name came from | never `verified` |

`actor_id` is the subject the authorizer actually checked. `SameTenantArenaAuthorizer` requires
`subject_id == organization_id`, so on a local run it can only ever be the organization — which is
why it cannot answer "who approved this irreversible effect" on its own.

Keeping them separate is deliberate. Conflating "who is permitted" with "who decided" is how one of
them ends up wrong, and the authorization invariant is what makes tenancy hold.

Both approvals **and rejections** carry attribution. Refusing an irreversible effect is as
consequential as permitting one, and "who blocked the release?" has to be answerable too.

## Setting the identity

Resolved in this order, most deliberate first:

1. **`[identity] name` in `.bounded-loops/config.toml`** — someone chose to write it down.

   ```toml
   [identity]
   name = "priya"
   ```

2. **`BOUNDED_LOOPS_IDENTITY`** — for one process or one CI job.

   ```bash
   BOUNDED_LOOPS_IDENTITY=release-bot bl graph approve --run … --node … --decision approved
   ```

3. **The OS user** — the fallback, when nothing else is set.
4. **`unknown`** — when even that fails. A real value, not a blank, so a reader sees "we did not
   know" rather than a missing field.

The identity is resolved by the engine at decision time. It is **never** accepted as an argument, so
a caller — or a model driving one — cannot assert who approved something.

## What this does not give you

**None of these sources is authentication.** An environment variable is trivially set. A config file
is written by whoever can write the workspace. An OS username is self-asserted: it proves which
local account acted, not which human sat at the keyboard.

That is why `decided_by_source` travels with the name, and why `decided_by_verified` is always
`false` locally. A surface rendering an approval must say so rather than presenting a name as
proven. The honest offer is "here is a name and here is exactly what it is worth" — which is
strictly more than the previous answer of nothing, and strictly less than authentication.

A **hosted or multi-tenant** deployment must not treat run-directory writability as approval
authority. It needs signed decisions and an injected verifier.

## Publishing a run directory

`approvals.json` lives in the run directory. If you publish one — as a reproducibility artifact,
a bug report, or a paper appendix — **the recorded identity travels with it.**

With no identity configured that will be the OS username of whoever ran the approval. Set one
explicitly for anything you intend to share:

```bash
BOUNDED_LOOPS_IDENTITY=paper-author bl graph run --execute …
```

The engine cannot know which runs you plan to publish, so it cannot make this choice for you. It
records what it resolved and says where it came from; deciding what is safe to share is yours.
