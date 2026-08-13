"""Assembling the BYOK / https connector worker.

Split out of ``graph_composition.py`` for the 800-line cap, and a clean seam on its own: this is the
only wiring that touches credentials at all — a real ``ExecutionGrant`` issued from a
deployment-supplied ``AdmittedConnectionRecord``, never from the plan or the binding, so a graph
cannot manufacture its own authority to call a paid API.

No credential VALUE passes through here either: the broker hands the forwarder an opaque handle and
the forwarder resolves it at the egress boundary.
"""

from __future__ import annotations

import ssl
from typing import Mapping

from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
    AdmittedConnectionRecord,
    AdmittedConnectionRequestBuilder,
)
from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.connectors.credentials import (
    CredentialSource,
    EnvCredentialResolver,
)
from bounded_loops.graph.adapters.connectors.http_forwarder import HttpConnectorForwarder
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.workers.connector_worker import ConnectorNodeWorker
from bounded_loops.graph.application.connector_forward import ConnectorInvoker
from bounded_loops.graph.application.credential_broker import OpaqueCredentialBroker
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.domain.connections import CredentialBinding, CredentialKind
from bounded_loops.graph.domain.plan import ExecutionPlan


def _build_https_worker(
    *,
    plan: ExecutionPlan,
    store: LocalArtifactStore,
    run_id: str,
    node_prompts: Mapping[str, str],
    admitted: Mapping[str, AdmittedConnectionRecord],
    organization_id: str,
    project_id: str,
    environ: Mapping[str, str] | None,
    egress_broker: EgressBroker | None,
    credential_resolver: object,
    tls_context: ssl.SSLContext | None,
) -> ConnectorNodeWorker:
    """Assemble the real BYOK worker stack for https-transport connector nodes."""
    # Collect bindings whose connection has an admitted record.
    https_bindings = [
        b for b in plan.connection_bindings
        if b.connection_id in admitted
    ]
    # Build OpaqueCredentialBroker — one CredentialBinding per admitted https binding.
    credential_bindings = [
        CredentialBinding(
            binding_id=b.binding_id,
            connection_id=b.connection_id,
            kind=CredentialKind.VAULT_REFERENCE,
        )
        for b in https_bindings
    ]

    # Build EnvCredentialResolver — or use the injected one (for tests).
    if credential_resolver is None:
        cred_sources = {
            b.binding_id: CredentialSource(
                env_var=admitted[b.connection_id].credential_env_var_name,
                header_name=admitted[b.connection_id].credential_header_name,
                value_prefix=admitted[b.connection_id].credential_header_prefix,
            )
            for b in https_bindings
        }
        resolved_credential_resolver = EnvCredentialResolver(cred_sources, environ=environ)
    else:
        resolved_credential_resolver = credential_resolver  # type: ignore[assignment]

    artifact_body: LocalArtifactBody = LocalArtifactBody(
        store,
        organization_id=organization_id,
        project_id=project_id,
        producer_attempt=f"{run_id}-byok-response",
    )

    forwarder = HttpConnectorForwarder(
        artifact_body=artifact_body,
        credential_resolver=resolved_credential_resolver,
        tls_context=tls_context,
    )

    resolved_egress_broker: EgressBroker = egress_broker if egress_broker is not None else EgressBroker()

    invoker = ConnectorInvoker(
        credential_broker=OpaqueCredentialBroker(credential_bindings),
        egress_broker=resolved_egress_broker,
        forwarder=forwarder,
    )

    request_port = AdmittedConnectionRequestBuilder(
        records=admitted,
        artifact_store=store,
        run_id=run_id,
        node_prompts=node_prompts,
        organization_id=organization_id,
        project_id=project_id,
    )

    return ConnectorNodeWorker(run_id=run_id, invoker=invoker, request_port=request_port)
