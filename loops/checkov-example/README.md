# checkov-example

**Pattern:** evaluator-optimizer · **Role:** security, engineering · **Rung:** L2 · **Gate:** checkov

Demonstrates the `CheckovGate`: drive an agent until
[checkov](https://github.com/bridgecrewio/checkov) reports zero failed
infrastructure-as-code checks in the workspace.

## What happens

`seed/main.tf` defines `aws_security_group.wide_open` — a security group
that allows ALL inbound and ALL outbound traffic from `0.0.0.0/0` on every
port/protocol, with no rule descriptions. This is a real, deliberately
misconfigured resource: running `checkov -d .` against it (confirmed live,
checkov 3.3.0) trips 6 real findings — `CKV_AWS_24`, `CKV_AWS_23`,
`CKV_AWS_25`, `CKV_AWS_260`, `CKV_AWS_277`, `CKV_AWS_382`. The loop runs an
agent against `PROMPT.md`, checks `checkov -d . --output json` after each
lap (authoritative on `summary.failed`, not the process exit code), and
halts as soon as the gate reports zero failed checks.

## Prerequisites (one-time)

```bash
# checkov — install once, not part of the timed run below.
pip install checkov
```

## Prove the gate genuinely fails on the unfixed seed

```bash
cd loops/checkov-example
checkov -d seed/ --output json | python3 -m json.tool | head -40
```

Real captured output confirms `summary.failed: 6` and
`results.failed_checks[].check_id` including `CKV_AWS_24`/`CKV_AWS_23` —
exactly matching `CheckovGate._summarize_failures`'s pinned fixture.

## Run it with the engine

```bash
# from repo root
pip install -e .
bl lint loops/checkov-example
bl run loops/checkov-example --yes
```

Expected:
```
status: DONE  laps: 1  ledger: loops/checkov-example/.ledger.jsonl
```

Lap 1's cassette restricts the security group to a single HTTPS ingress
rule from a named CIDR, a single HTTPS egress rule, and adds rule
descriptions — checkov then reports `summary.failed: 0`, and the loop
reaches DONE.

## Lift it into your own repo

1. Copy this folder.
2. Replace `seed/main.tf` with your own IaC (any framework checkov
   recognizes — Terraform, CloudFormation, Kubernetes manifests, etc.).
3. Edit `PROMPT.md` to describe your goal.
4. Run `bl run loops/<your-copy>` to prove it works.

## Which Anthropic pattern

`evaluator-optimizer` — the gate (checkov) is the evaluator; the agent-turn
is the optimizer. The loop runs until the evaluator says clean.
Reference: [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
