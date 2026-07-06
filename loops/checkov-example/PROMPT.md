# Checkov Scan: close the open security group

Goal: make `checkov -d . --output json` report zero failed checks for
`seed/main.tf`.

Steps each turn:
  1. Run `checkov -d seed/ --output json` (or read the last verdict).
  2. `aws_security_group.wide_open` allows ALL inbound and ALL outbound
     traffic from `0.0.0.0/0` on every port/protocol — this trips multiple
     real checkov rules (CKV_AWS_24, CKV_AWS_23, CKV_AWS_25, CKV_AWS_260,
     CKV_AWS_277, CKV_AWS_382).
  3. Restrict the rule: narrow `ingress`/`egress` to a specific port
     (e.g. 443/tcp) and a specific, non-0.0.0.0/0 CIDR block for ingress.
     Add a `description` field to each rule block (checkov also flags
     missing descriptions).

Done when: `checkov -d . --output json` reports `summary.failed == 0` for
every matched framework.

Do not delete `main.tf`.
Do not add new resources beyond fixing this one.
