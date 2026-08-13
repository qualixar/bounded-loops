# Providers: adding an agent CLI without changing code

Five agent CLIs ship built in — `claude`, `codex`, `grok`, `muse`, `agy`. They differ from each
other only in data: which binary, which flags, how the prompt arrives, which JSON envelope carries
usage. So adding a sixth does not need a code change or a release.

## See what your deployment can actually run

```bash
bl graph providers
```

```
agy: binary=agy prompt_via=arg metered via 'agy' envelope; env names requested: none
claude: binary=claude prompt_via=stdin metered via 'claude' envelope; env names requested: none
codex: binary=codex prompt_via=arg NOT metered; env names requested: none
grok: binary=grok prompt_via=arg metered via 'grok' envelope; env names requested: none
muse: binary=muse prompt_via=arg NOT metered; env names requested: none
```

**Read the metering column before you author a spend cap.** A provider marked `NOT metered` cannot
report what it spent, so a node that declares `max_tokens` or `max_cost_microunits` on it **fails
closed** — the run refuses rather than treating the call as free. That is deliberate: a budget
checked against a quantity nobody can measure never trips, which looks exactly like protection and
is not.

`codex` and `muse` are unmetered because they emit JSONL event streams rather than a single
envelope. `muse`'s stream was probed and contains no token, usage or cost field anywhere; `codex`
did not return inside the probe window, so its stream is genuinely unread. Neither is guessed at.

## Add a provider

Write a TOML catalog:

```toml
[providers.house-reviewer]
binary = "agy"
args = ["-p"]
prompt_via = "arg"                          # "arg" or "stdin"
usage_args = ["--output-format", "json"]    # how to ask for a machine-readable envelope
envelope = "agy"                            # which shipped parser reads it: claude | grok | agy
unset_env = ["AGY_SESSION"]                 # names to remove before launching
env_grant = ["HOUSE_REGION"]                # names this CLI needs forwarded — never values
```

Then point a run at it:

```bash
bl graph run graph.yaml --execute --out ./run --providers ./providers.toml
```

Or set it once for the machine:

```bash
export BOUNDED_LOOPS_PROVIDERS=/etc/bounded-loops/providers.toml
```

An entry may also **override** a shipped provider — that is how you point `claude` at a wrapper
script, or correct a flag this version gets wrong on your host, without waiting for a release.

## What a catalog may not contain

**It may not contain a credential.** `env_grant` and `unset_env` hold environment variable *names*.
`set_env` — which would hold values — is refused outright, because a config file that *can* hold a
value is one that eventually does: committed to a repo, pasted into a ticket, copied into a chat.
The engine decides which names reach a subprocess; it never reads a value.

An entry that names something which is plainly a value rather than a name is refused and tells you
to rotate it.

**Unknown keys are errors, not shrugs.** A typo'd `envelop = "claude"` is rejected. Silently
ignoring it would leave you believing the provider is metered while every spend cap on it fails
closed — and the real cause would be a missing letter.

## Forwarding an environment variable takes two keys

Some CLIs need a variable the base allow-list does not include. That takes **both** of:

1. the provider **declaring** the name — `env_grant` in its catalog entry, and
2. the operator **allowing** the name — `BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW=NAME1,NAME2`

```bash
export BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW=HOUSE_REGION
```

Neither alone is enough. A careless catalog entry cannot open the channel by itself, and neither can
a forgotten `export` in a shell profile. If a provider asks for a name you have not allowed, the run
logs a warning naming it and does not forward it.

### This is a breaking change from 0.4.x — read this if you already set a grant

Before 0.5, on the local-CLI path, **the operator variable alone was enough**. Every shipped
provider declares `env_grant = []`, so if you set `BOUNDED_LOOPS_CLI_ENV_GRANT=MY_KEY` and relied on
a built-in provider, that key reached the CLI.

**It no longer does.** The provider has to declare the name too. `BOUNDED_LOOPS_CLI_ENV_GRANT` is
still read on this path, so the *variable* keeps working — but the old name does **not** restore the
old one-key behaviour, and nothing can, because that behaviour is the thing being fixed.

To restore a grant, add the name to the provider's catalog entry — **and make sure the process
actually loads that catalog.** A file on disk is not a grant.

```toml
# /etc/bounded-loops/providers.toml
[providers.codex]
binary = "codex"
args = ["exec", "--skip-git-repo-check"]
prompt_via = "arg"
env_grant = ["MY_KEY"]        # the provider's half
```

```bash
export BOUNDED_LOOPS_PROVIDERS=/etc/bounded-loops/providers.toml  # load the catalog
export BOUNDED_LOOPS_ENV_PASSTHROUGH_ALLOW=MY_KEY                # your half
```

Check it took effect before you rely on it — this prints the names each provider asks for:

```bash
bl graph providers
```

If `codex` still shows `env names requested: none`, the catalog is not being loaded and the grant
will not happen. (The P3 audit followed the first version of this recipe literally, without the
`BOUNDED_LOOPS_PROVIDERS` line, and the grant silently did nothing — the recipe was wrong, not the
reader.)

The run logs a warning naming any variable you allowed that no provider asked for, and any variable
a provider asked for that you did not allow — so a half-configured grant says so instead of failing
somewhere inside the CLI. Names only; never values.

Prefer the new name going forward: it is the one the base loop engine has always used, and since 0.5
both names mean exactly the same thing.

## Provider packages (entry points)

A provider that needs actual code — a new transport, not just different flags — can ship as a
package:

```toml
[project.entry-points."bounded_loops.graph.providers"]
mycloud = "mycloud_bounded_loops:providers"
```

where `providers` is a callable returning `Mapping[str, CliProfile]`.

This is the only place the engine runs code you did not write, so four rules apply:

- **A broken plugin is skipped, not fatal.** One that raises on import or on call is logged and
  dropped. A third-party package cannot take down your run.
- **Registration is all-or-nothing.** A plugin offering three good providers and one bad one
  contributes none of them, so the available provider set never depends on iteration order.
- **A plugin cannot claim a shipped name.** A package registering `claude` and quietly becoming the
  thing your existing graphs bind to is refused. Your own catalog *can* override a shipped name —
  that is a local decision you made, not a package making it for you.
- **A plugin cannot forward a credential on its own.** Its `env_grant` names still go through your
  allow-list, and a plugin supplying an environment *value* is refused.

Precedence, tightest authority last: **plugins < shipped < your catalog.**

## Binding a provider you do not have

A graph that binds a node to a provider with no profile is refused **before the run starts**:

```
error: node 'review' binds local-CLI provider 'openai', which this deployment has no profile for
(known: agy, claude, codex, grok, muse). Add a provider catalog entry for it, or bind the node to a
provider that is installed. Refused before the run starts: reaching this node would fail every
attempt identically, having already paid for every node upstream of it.
```

Before this check existed the run started, paid for every node upstream, then failed the bad node
once per retry — each attempt failing identically. A misconfiguration that is fully visible in the
plan is now caught from the plan.
