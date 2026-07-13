# Agent plugins

The plugin packages expose the bounded-loops skill and the
`bounded-loops-mcp` server to supported coding agents. Install the Python MCP
extra first so the command referenced by every plugin is available:

```bash
pip install "bounded-loops[mcp]"
bounded-loops-mcp --help
```

## Codex

From a clone of this repository, add its local marketplace and install the
plugin:

```bash
codex plugin marketplace add .
codex plugin add bounded-loops@bounded-loops
```

For the published Git repository, use:

```bash
codex plugin marketplace add qualixar/bounded-loops
codex plugin add bounded-loops@bounded-loops
```

Start a new Codex task after installation so Codex discovers the skill and MCP
server. The package uses the current `.codex-plugin/plugin.json` contract; the
old `plugin.toml` format is not used.

## Claude Code

From a clone, register and install the local package:

```bash
claude plugin marketplace add ./plugins/claude-code
claude plugin install bounded-loops@bounded-loops
```

The Claude package contains `.claude-plugin/plugin.json`, its marketplace
entry, the shared MCP configuration, the bounded-loops skill, and the
`/bl-run` command.

## Antigravity

`plugins/antigravity/` contains the equivalent skill, command, hook, and MCP
configuration. Copy that directory through Antigravity's local plugin import
flow after installing `bounded-loops[mcp]`. No Antigravity CLI is assumed here;
the committed manifest can be validated independently as JSON.

## Verify a source checkout

The release tests validate all manifests. Codex is additionally exercised in
an isolated home so testing cannot alter a developer's real Codex config:

```bash
python -m pytest -q tests/release/test_plugin_installation.py
```
