"""LIVE BYOK smoke — the real HTTP forwarder against OpenRouter (opt-in, not in the default run).

Skipped unless BOTH are set:
  * OPENROUTER_API_KEY  — the real key. This test never reads, prints, or stores the value;
    ``EnvCredentialResolver`` injects it onto the wire at call time.
  * BL_LIVE_OPENROUTER=1 — an explicit opt-in, so the default suite stays hermetic.

Run:
    set -a; . ~/.claude-secrets.env; set +a
    BL_LIVE_OPENROUTER=1 uv run pytest -s \
        tests/graph/adapters/connectors/test_http_forwarder_openrouter_live.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket

import pytest

from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.connectors.credentials import CredentialSource, EnvCredentialResolver
from bounded_loops.graph.adapters.connectors.http_forwarder import HttpConnectorForwarder
from bounded_loops.graph.adapters.connectors.request_document import ConnectorRequestDocument, encode_request_document
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.connector_forward import ConnectorInvocation
from bounded_loops.graph.application.egress_broker import _is_public_unicast
from bounded_loops.graph.domain.authoring import Effect
from bounded_loops.graph.domain.connections import CredentialLease

_MODEL = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct")

pytestmark = pytest.mark.skipif(
    not (os.environ.get("OPENROUTER_API_KEY") and os.environ.get("BL_LIVE_OPENROUTER") == "1"),
    reason="live OpenRouter smoke is opt-in (set OPENROUTER_API_KEY and BL_LIVE_OPENROUTER=1)",
)


def _public_ips(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    resolved = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
    return tuple(ip for ip in resolved if _is_public_unicast(ip))


def test_forwarder_calls_openrouter_for_real(tmp_path: Path):
    host = "openrouter.ai"
    pinned = _public_ips(host)
    assert pinned, "openrouter.ai did not resolve to a public address"

    store = LocalArtifactStore(tmp_path / "artifacts")
    body = LocalArtifactBody(store, organization_id="o", project_id="p", producer_attempt="live-1")
    payload = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
        "max_tokens": 5,
    }).encode("utf-8")
    doc = ConnectorRequestDocument(
        path="/api/v1/chat/completions",
        headers={"content-type": "application/json"},
        body=payload,
        scheme="https",
    )
    digest = body.store(encode_request_document(doc))
    invocation = ConnectorInvocation(destination=host, method="POST", effect=Effect.EXTERNAL_WRITE, payload_digest=digest)
    lease = CredentialLease(
        lease_id="lease:live", grant_id="grant:live", run_id="run-live", node_id="n1", attempt=1,
        connection_id="openrouter", binding_id="openrouter-1", effects=frozenset({Effect.EXTERNAL_WRITE}),
        destination=host, expires_at="2999-01-01T00:00:00+00:00",
    )
    resolver = EnvCredentialResolver(
        {"openrouter-1": CredentialSource("OPENROUTER_API_KEY", "Authorization", value_prefix="Bearer ")},
    )
    forwarder = HttpConnectorForwarder(artifact_body=body, credential_resolver=resolver, timeout=60.0)

    result = forwarder.forward(lease=lease, invocation=invocation, pinned_ips=pinned)

    assert result.ok is True, f"forward failed: reason={result.reason!r} status={result.provider_status}"
    assert result.provider_status == 200
    assert result.response_digest is not None
    reply = json.loads(body.fetch(result.response_digest))
    content = reply["choices"][0]["message"]["content"]
    assert isinstance(content, str) and content
    print(f"\n[LIVE OpenRouter] model={_MODEL} status={result.provider_status} reply={content!r}")
