# Codex Cloud Security Baseline

Use this checklist when creating or updating the Cloud environment for this repository.

## Repository scope

Attach only this repository unless a task genuinely requires another repository. Adding another repository is a permission expansion and requires human approval.

## Install/setup

Use deterministic setup commands only. Do not embed tokens, passwords, private URLs, API keys, or production configuration in setup scripts.

Prefer the repository's declared Python requirements and existing test commands. Avoid remote shell installers such as `curl ... | sh`.

## Start skill

Leave the Start skill unset initially. Add one only after reviewing its complete instructions and capabilities. A start skill must not broaden network access, read credential stores, publish changes, or bypass the approval stages in `docs/AI_CONTROL_POLICY.md`.

## Internet access

Set runtime internet access to **off** by default.

If a task truly requires network access, approve the exact purpose and use the narrowest available destination allowlist. Disable that exception again when the task is complete.

## Secrets and credentials

Phase 1 should run with no production secrets.

Do not expose long-lived cloud credentials, deployment credentials, signing keys, package-publishing tokens, database credentials, personal access tokens, or organization-admin credentials to the agent runtime.

If future work needs authenticated access, prefer a dedicated, revocable, least-privilege identity scoped to one repository and one purpose. Never grant admin or bypass privileges simply to make an agent task easier.

## GitHub permissions

The worker may eventually need enough permission to push a task branch and open a draft PR, but it must not have authority to bypass protected-branch rules or merge without human review.

The default branch should require pull requests and human approval. Recommended protections:

- require a pull request before merging;
- require at least one approving human review;
- dismiss stale approvals when new commits are pushed;
- require approval of the most recent push by someone other than the pusher where your team structure permits it;
- require status checks;
- require conversation resolution;
- block force pushes and branch deletion;
- do not grant agents/apps bypass rights.

## Operating sequence

1. Dot observes and investigates read-only.
2. Dot reports evidence and a proposed bounded task.
3. Human approves implementation scope.
4. Codex Cloud edits and tests only inside the isolated workspace.
5. Dot reports the result.
6. Human separately approves publication.
7. Codex Cloud may push the task branch and open/update a **draft** PR.
8. Required CI and independent gates run.
9. Human reviews.
10. Merge/release/deploy remains human-only.

If any step needs more authority than listed, stop and request a new approval.
