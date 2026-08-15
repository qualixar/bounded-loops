# Agent plugins

The plugin packages expose the bounded-loops skill and the
`bounded-loops-mcp` server to supported coding agents. Install the Python MCP
extra first so the command referenced by every plugin is available:

```bash
pip install "bounded-loops[mcp]"
python scripts/smoke_mcp_server.py
```

The smoke command starts the real stdio server, initializes an MCP client, and
lists the required tools. A successful run prints `MCP smoke passed` and exits.

## Installing is additive — your existing configuration is yours

You almost certainly already have hooks, agents, and MCP servers configured. Nothing here
replaces any of them.

* **Nothing in this repository writes a user-level host config.** Not
  `~/.claude/settings.json`, not `~/.codex/config.toml`, nothing. Installation happens through
  each host's own plugin mechanism, and the host merges our plugin-local files with your
  settings. `tests/release/test_install_is_additive.py` pins this against the source tree, so a
  future convenience installer cannot quietly acquire the ability to overwrite your setup.
* **Our hooks sit alongside yours.** Each `plugins/<host>/hooks/hooks.json` declares only
  `bounded_loops.hooks.*` commands. Every supported host runs all hooks registered for an event,
  so an existing Stop or PreToolUse hook of yours keeps running exactly as before.
* **`AGENTS.md.snippet` is APPEND-only.** Add its contents to the end of your existing
  `AGENTS.md`. **Do not replace** your `AGENTS.md` with it — the file is a fragment, not a
  document, and copying it over the top would delete your own instructions.
* **The Stop hook has an off switch, and it lives in your project.** That hook refuses to let a
  host agent declare a session "done" while a bounded run is not in a terminal state. That is the
  point of the tool, and it is also a behaviour change in your editor, so you can downgrade it to
  a warning in `.bounded-loops/config.toml`:

  ```toml
  [hooks]
  stop_on_active_run = false
  ```

* **Uninstalling leaves nothing behind** except the `.bounded-loops/` directory in your projects,
  which holds your run receipts. That is yours to keep or delete.

## Codex

From a clone of this repository, add its local marketplace and install the
plugin:

```bash
codex plugin marketplace add .
codex plugin add bounded-loops@bounded-loops
```

Verified on 2026-07-13 with `codex-cli 0.144.3` in a fresh `CODEX_HOME`:
the marketplace was added and `bounded-loops@bounded-loops` installed at
version `0.6.3`, with its skill and `.mcp.json` present in the plugin cache.

Start a new Codex task after installation so Codex discovers the skill and MCP
server. The package uses the current `.codex-plugin/plugin.json` contract; the
old `plugin.toml` format is not used.

## Claude Code

From a clone, register and install the local package:

```bash
claude plugin marketplace add ./plugins/claude-code
claude plugin install bounded-loops@bounded-loops
```

Verified on 2026-07-13 with Claude Code 2.1.168 in a temporary config home:
validation passed, the marketplace was added, and the plugin installed and
reported `Version: 0.6.3` and `Status: enabled`.

The Claude package contains `.claude-plugin/plugin.json`, its marketplace
entry, the shared MCP configuration, the bounded-loops skill, and the
`/bl-run` command.

## Antigravity

`plugins/antigravity/` contains the equivalent skill, command, hook, and MCP
configuration:

```bash
agy plugin validate ./plugins/antigravity
agy plugin install ./plugins/antigravity
agy plugin list
```

Verified on 2026-07-13 with `agy 1.0.16` in a temporary home. Validation and
installation both processed one skill, one command, and one MCP server.

## Direct MCP client configuration

Claude Code and Cursor accept the same stdio server definition (place it in the
client's MCP JSON file):

```json
{
  "mcpServers": {
    "bounded-loops": {
      "command": "bounded-loops-mcp",
      "args": []
    }
  }
}
```

The command is intentionally long-running when launched directly; MCP clients
own its stdin/stdout lifecycle. Use `scripts/smoke_mcp_server.py` for a bounded
startup check.

## Verify a source checkout

The release tests validate all manifests. Codex is additionally exercised in
an isolated home so testing cannot alter a developer's real Codex config:

```bash
python -m pytest -q tests/release/test_plugin_installation.py
```
