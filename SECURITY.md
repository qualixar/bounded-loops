# Security Policy

## Reporting a vulnerability

If you find a security issue in bounded-loops, please report it **privately**:

- Email **varun.pratap.bhardwaj@gmail.com** with the details and, where possible, a reproduction.
- Please do **not** open a public issue for a security vulnerability.

You can expect an acknowledgment within a few days. Once the issue is confirmed and fixed, we'll credit you in the release notes if you wish.

## Scope — what's especially valuable

bounded-loops deliberately runs untrusted agent output under nine enforced safety
bounds (a sandboxed scratch workspace, input quarantine, a workspace-integrity
guard, and an env-var kill switch). Reports that demonstrate a **bypass of any
bound** are the highest-value findings, for example:

- an agent, runner, or cassette **escaping the scratch workspace** or writing outside it;
- **tampering with a gate's own verification anchor** (e.g. rewriting a test or checker) to force a false `DONE`;
- **evading the kill switch** or a lap/token/wall-clock bound;
- smuggling quarantined secrets into or out of the sandbox.

The project's core claim is that the gate — not the agent's word — decides DONE.
A credible break of that claim is exactly what we want to hear about.

## Supported versions

The latest released version receives security fixes.
