"""Resolve an admitted local-CLI connector node to a concrete agent-CLI invocation (RE).

The compiler-admitted binding names WHICH agent CLI to run: for a ``local_cli`` connection the
binding's ``provider_id`` is the CLI's name (``claude`` | ``codex`` | ``grok`` | ``muse`` | ``agy``).
The prompt is RUN-TIME input, not authoring structure — a portable authoring graph "knows nothing
about runtime state", and its node schema is closed — so the prompt is supplied per node at execute
time (``node_id -> prompt``) rather than baked into the manifest. No credential is read here: the
CLI authenticates itself out-of-band via the user's own subscription (RC Mode 1).
"""

from __future__ import annotations

from typing import Mapping

from bounded_loops.graph.adapters.connectors.local_cli_worker import (
    CLI_PROFILES,
    CliInvocation,
    CliProfile,
)
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.domain.errors import (
    GraphIntegrityError,
    GraphValidationError,
    WorkerContractError,
)
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode, ResolvedBinding


class NodeCliResolver:
    """A ``LocalCliConnectorPort`` that maps each admitted local-CLI node to the agent CLI
    named by its binding (``provider_id``) and the prompt supplied for it as run-time input."""

    def __init__(
        self,
        node_prompts: Mapping[str, str],
        *,
        profiles: Mapping[str, CliProfile] = CLI_PROFILES,
    ) -> None:
        self._prompts = dict(node_prompts)
        self._profiles = dict(profiles)

    def resolve(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> CliInvocation:
        binding = self._binding_for(plan, node)
        profile = self._profiles.get(binding.provider_id)
        if profile is None:
            known = ", ".join(sorted(self._profiles)) or "(none configured)"
            # ``WorkerContractError``, not a bare ``GraphIntegrityError``: an unknown provider is
            # DETERMINISTIC. As a plain exception the controller read it as a transient
            # ``WORKER_FAULT`` and retried to ``max_attempts``, every attempt failing identically.
            # Preflight now refuses this before the run starts; this is the second line of defence
            # for a deployment that supplies profiles directly to the resolver and bypasses it.
            raise WorkerContractError(
                f"local-CLI node {node.node_id!r} binds provider {binding.provider_id!r}, "
                f"which is not a known agent CLI ({known})"
            )
        prompt = self._prompts.get(node.node_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise GraphValidationError(
                "cli_prompt",
                f"/inputs/{node.node_id}",
                f"no run-time prompt was supplied for local-CLI node {node.node_id!r}",
            )
        return CliInvocation(profile=profile, prompt=prompt)

    @staticmethod
    def _binding_for(plan: ExecutionPlan, node: PlannedNode) -> ResolvedBinding:
        if node.binding_id is None:
            raise GraphIntegrityError(
                f"local-CLI node {node.node_id!r} is not bound to an admitted connection"
            )
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id), None
        )
        if binding is None:
            raise GraphIntegrityError(
                f"local-CLI node {node.node_id!r} has no compiled binding"
            )
        return binding
