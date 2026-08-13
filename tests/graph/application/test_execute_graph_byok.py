"""Hermetic BYOK/HTTP e2e tests for ``execute_graph_run`` in https-transport mode.

Every network test runs against a real HTTPS server bound to 127.0.0.1 — no external
egress, no real credential, no real API key.  The TLS handshake uses a throwaway self-signed
certificate (same pattern as test_http_forwarder.py).

Pinned-IP invariant: a custom ``_PinnedEgressBroker`` always returns 127.0.0.1 as the
single pinned address, bypassing the EgressBroker's SSRF check (which correctly refuses
loopback addresses in production — we're proving the FULL seam from graph compile through
grant issuance and artifact storage, not the broker's SSRF logic which has its own test).

Tests:
1. BYOK https node runs end-to-end; grant issued from AdmittedConnectionRecord, response
   captured as node artifact; run dir persists mode="https" in run-meta.json.
2. An https node with no admitted record fails CLOSED at preflight — no event log written,
   no dummy grant minted.
3. Credential value NEVER appears in the stored request document or the response artifact.
4. Local-CLI tests from test_execute_graph.py still pass (mode unchanged).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
import shutil
import ssl
import subprocess
import threading

import pytest

from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
    AdmittedConnectionRecord,
)
from bounded_loops.graph.adapters.connectors.credentials import (
    MappingCredentialResolver,
    ProviderCredential,
)
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.arena_projection import (
    ArenaReadRequest,
    read_arena_projection,
)
from bounded_loops.graph.application.egress_broker import EgressDecision, EgressBroker
from bounded_loops.graph.graph_composition import execute_graph_run
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir as _load_plan_from_run_dir
from bounded_loops.graph.domain.authoring import DataClass
from bounded_loops.graph.domain.connections import RoutePolicy

_ORG, _PROJECT = "local-org", "local-project"

# ── shared manifest + connections fixtures ────────────────────────────────────

_BYOK_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: byok-chat
version: "1.0.0"
nodes:
  - id: chat
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [external_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""

_CONN_ID = "conn-byok"
_BINDING_ID = "binding-byok"


def _byok_connections(host: str) -> list[dict]:
    """Connection-candidate snapshot for an https-transport node."""
    return [{
        "binding_id": _BINDING_ID,
        "slot_id": "model",
        "connector_id": "byok-http",
        "connector_version": "1.0.0",
        "connection_id": _CONN_ID,
        "admission_digest": "sha256:" + "b" * 64,
        "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "openai",
        "model_target": "gpt-4o-mini",
        "region": "us-east-1",
        "fallback": False,
        "capabilities": ["text_generation"],
        "data_class_max": "public",
        "allowed_effects": ["external_write"],
        "isolation": "process_restricted",
        "transport": "https",
        "admitted": True,
    }]


def _route_policy() -> RoutePolicy:
    return RoutePolicy(
        policy_digest="sha256:" + "c" * 64,
        allowed_providers=frozenset({"openai"}),
        allowed_models=frozenset({"gpt-4o-mini"}),
        allowed_regions=frozenset({"us-east-1"}),
        fallback_allowed=False,
        route_verifiable=False,
        data_class_max=DataClass.PUBLIC,
    )


def _admitted_record(host: str) -> AdmittedConnectionRecord:
    """A real AdmittedConnectionRecord for the mock server."""
    return AdmittedConnectionRecord(
        connection_id=_CONN_ID,
        endpoint_scheme="https",
        endpoint_host=host,
        endpoint_path="/v1/chat/completions",
        allowed_effect=__import__(
            "bounded_loops.graph.domain.authoring", fromlist=["Effect"]
        ).Effect.EXTERNAL_WRITE,
        expires_at="2999-01-01T00:00:00+00:00",
        route_policy=_route_policy(),
        request_style="openai_chat",
        credential_env_var_name="TEST_BYOK_KEY",
    )


# ── test-only EgressBroker that pins 127.0.0.1 ────────────────────────────────

class _PinnedEgressBroker(EgressBroker):
    """Test-only EgressBroker: always authorizes the request and returns 127.0.0.1.

    The real EgressBroker correctly refuses 127.0.0.1 (loopback is not a globally-routable
    public address — SSRF-safety).  For hermetic e2e tests we bypass that check here; the
    broker's SSRF logic is independently tested in test_egress_broker.py.
    """

    def authorize(
        self, *, lease: object, request: object, now: object = None,
    ) -> EgressDecision:
        return EgressDecision(allowed=True, reason="", pinned_ips=("127.0.0.1",))


# ── minimal local-TLS mock provider (reusing test_http_forwarder.py pattern) ──

class _MockProvider:
    """A real HTTPS server on 127.0.0.1 that records the request and returns a canned reply."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b'{"choices":[{"message":{"content":"mock reply"}}]}',
        tls: tuple[Path, Path],
    ) -> None:
        self.received: list[dict] = []
        provider = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                payload = self.rfile.read(length) if length else b""
                provider.received.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": payload,
                })
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._handle()

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(tls[0]), keyfile=str(tls[1]))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        self.port: int = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockProvider":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ── TLS cert fixture (identical to test_http_forwarder.py) ────────────────────

@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not available for a live TLS handshake test")
    directory = tmp_path_factory.mktemp("byok_tls")
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    # Use byok.test (not localhost) so the hostname passes NetworkDestination validation,
    # which requires a public-style hostname with at least one dot.  The _PinnedEgressBroker
    # maps it to 127.0.0.1 at runtime, so no real DNS resolution is needed.
    result = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
         "-out", str(cert), "-days", "3650", "-nodes", "-subj", "/CN=byok.test",
         "-addext", "subjectAltName=DNS:byok.test"],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("openssl could not generate a test certificate")
    return cert, key


# ── helpers ───────────────────────────────────────────────────────────────────

class _Auth:
    def authorize(self, request: ArenaReadRequest) -> bool:
        return True


class _Verify:
    def verify(self, identity: object, receipts: object) -> None:
        return None


def _arena(out: Path):
    plan, identity, meta = _load_plan_from_run_dir(out)
    event_log = GraphEventLog(out / "controller-events.jsonl", identity)
    return read_arena_projection(
        plan, event_log,
        ArenaReadRequest(subject_id=_ORG, organization_id=_ORG, project_id=_PROJECT, run_id="run-1"),
        _Auth(), _Verify(),
    ), meta


# ── test 1: BYOK e2e run succeeds ────────────────────────────────────────────

def test_byok_https_node_runs_end_to_end(tmp_path: Path, tls_cert: tuple[Path, Path]):
    """Full BYOK e2e: https connector node runs against mock, grant from admitted record,
    response captured as artifact, run dir has mode='https'."""
    cert, key = tls_cert
    mock_body = b'{"choices":[{"message":{"content":"BYOK REPLY"}}]}'
    fake_api_key = "test-key-byok-not-real-9999"

    with _MockProvider(body=mock_body, tls=(cert, key)) as provider:
        # Use byok.test (has a dot) so NetworkDestination accepts it; _PinnedEgressBroker
        # maps it to 127.0.0.1 at runtime — no real DNS resolution or external egress.
        host = f"byok.test:{provider.port}"
        record = _admitted_record(host)

        # Client TLS context trusts the self-signed cert.
        client_ctx = ssl.create_default_context(cafile=str(cert))

        # Inject a MappingCredentialResolver with the fake key — it flows into the HTTP
        # header over TLS, NOT into any stored artifact.
        fake_credential = ProviderCredential({"authorization": f"Bearer {fake_api_key}"})
        cred_resolver = MappingCredentialResolver({_BINDING_ID: fake_credential})

        out = tmp_path / "run"
        rc = execute_graph_run(
            manifest_text=_BYOK_MANIFEST,
            manifest_suffix=".yaml",
            connections_raw=_byok_connections(host),
            node_prompts={"chat": "Hello, BYOK world"},
            out_dir=out,
            run_id="run-1",
            admitted_connections={_CONN_ID: record},
            byok_egress_broker=_PinnedEgressBroker(),
            byok_credential_resolver=cred_resolver,
            byok_tls_context=client_ctx,
        )

    assert rc == 0, "BYOK run should succeed (rc=0)"
    assert (out / "controller-events.jsonl").is_file()

    arena, meta = _arena(out)
    assert arena.run_state == "SUCCEEDED"
    assert meta["execution"] is True
    assert meta["mode"] == "https"

    node = arena.nodes[0]
    assert node.node_id == "chat"
    assert node.state == "SUCCEEDED"
    assert node.artifact_digests, "response must be captured as an artifact"

    # Read back the response artifact and verify it contains the mock body.
    store = LocalArtifactStore(out / "artifacts")
    from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
    with store.open(
        ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT),
        ArtifactAccess(_ORG, _PROJECT),
    ) as handle:
        response_bytes = handle.read()
    assert response_bytes == mock_body

    # Verify exactly one request was made to the mock.
    assert len(provider.received) == 1
    seen = provider.received[0]
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/chat/completions"
    # Credential header reached the wire (over TLS only).
    assert seen["headers"].get("authorization") == f"Bearer {fake_api_key}"

    # Request body contains the prompt; no credential header inside the document.
    req_body = json.loads(seen["body"])
    assert req_body["messages"][0]["content"] == "Hello, BYOK world"
    assert req_body["model"] == "gpt-4o-mini"


# ── test 2: missing admitted record fails CLOSED at preflight ────────────────

def test_https_node_without_admitted_record_fails_closed_at_preflight(tmp_path: Path):
    """An https connector node with no admitted record fails at preflight — no event log,
    no dummy grant, no receipt.  The run directory is created but empty."""
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_BYOK_MANIFEST,
        manifest_suffix=".yaml",
        connections_raw=_byok_connections("api.openai.com"),
        node_prompts={"chat": "should not run"},
        out_dir=out,
        run_id="run-1",
        admitted_connections={},      # deliberately empty — no record for conn-byok
        byok_egress_broker=_PinnedEgressBroker(),
    )

    assert rc == 2, "missing admitted record must fail closed (rc=2)"
    # Preflight fires BEFORE the event log is written.
    assert not (out / "controller-events.jsonl").is_file(), (
        "no event log must be written if preflight refuses the run"
    )


# ── two bindings on one connection: refused from the plan ────────────────────

_TWO_SLOT_MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: byok-two
version: "1.0.0"
nodes:
  - id: ask
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [external_write]
    isolation: process_restricted
    connection_slot: model_a
  - id: again
    kind: research_claim
    inputs: {claim: text}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [external_write]
    isolation: process_restricted
    connection_slot: model_b
edges:
  - {from_node: ask, from_port: claim, to_node: again, to_port: claim}
connection_slots:
  - {id: model_a, requires: [text_generation], data_class_max: public}
  - {id: model_b, requires: [text_generation], data_class_max: public}
policies: {data_class: public, fail_mode: fail_closed}
"""


def _two_slot_connections(connection_a: str, connection_b: str) -> list[dict]:
    """Two https bindings, one per slot, over the caller's choice of connection ids."""
    def one(binding_id: str, slot_id: str, connection_id: str) -> dict:
        candidate = dict(_byok_connections("api.openai.com")[0])
        candidate.update(binding_id=binding_id, slot_id=slot_id, connection_id=connection_id)
        return candidate
    return [one("b-a", "model_a", connection_a), one("b-b", "model_b", connection_b)]


def _record_for(connection_id: str) -> AdmittedConnectionRecord:
    from dataclasses import replace

    return replace(_admitted_record("api.openai.com"), connection_id=connection_id)


def test_two_bindings_on_one_connection_are_refused_at_preflight(tmp_path: Path, capsys):
    """Two bindings sharing one connection_id is refused from the PLAN, not from a worker.

    Left to run time this reached ``OpaqueCredentialBroker``, whose lease mint recovers the
    binding from the grant's ``connection_id`` and refuses when that is ambiguous. The worker
    turned that into ``cause=worker_fault`` with an EMPTY ``node.spend`` — a classification that
    cannot distinguish "refused before the request" from "the provider was already paid", and an
    empty usage block that cannot settle it either. It is in fact pre-egress, but the receipt
    could not say so, and any node upstream on a different connection had really paid by then.
    """
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_TWO_SLOT_MANIFEST,
        manifest_suffix=".yaml",
        connections_raw=_two_slot_connections("shared-conn", "shared-conn"),
        node_prompts={"ask": "should not run", "again": "should not run"},
        out_dir=out,
        run_id="run-1",
        admitted_connections={"shared-conn": _record_for("shared-conn")},
        byok_egress_broker=_PinnedEgressBroker(),
    )

    assert rc == 2, "an ambiguous connection binding must fail closed (rc=2)"
    assert not (out / "controller-events.jsonl").is_file(), (
        "refused from the plan means no node ever started, so there is no receipt stream"
    )
    captured = capsys.readouterr()
    message = captured.out + captured.err
    # The message has to name the duplicated connection AND both bindings — the whole point is
    # that the operator can find the two lines of config to change.
    assert "'shared-conn'" in message, message
    assert "'b-a'" in message and "'b-b'" in message, message


def test_two_bindings_on_distinct_connections_are_not_refused(tmp_path: Path):
    """The guard must not refuse the configuration that already worked.

    Same graph, same two slots — only the connection ids differ. This is the shape operators are
    told to move to by the refusal message, so it has to keep passing preflight.
    """
    from bounded_loops.graph.graph_composition import _preflight, _parse_manifest
    from bounded_loops.graph.graph_composition import _DEFAULT_POLICY_DIGEST
    from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph

    plan = compile_graph(
        _parse_manifest(_TWO_SLOT_MANIFEST, ".yaml"),
        CompileSnapshot(
            policy_digest=_DEFAULT_POLICY_DIGEST,
            package_digests=frozenset(),
            connections=tuple(_two_slot_connections("conn-a", "conn-b")),  # type: ignore[arg-type]
        ),
    )
    admitted = {"conn-a": _record_for("conn-a"), "conn-b": _record_for("conn-b")}

    assert _preflight(plan, admitted) is None


def test_a_duplicate_binding_no_node_uses_does_not_wedge_the_run():
    """Scoping guard: the rule is per NODE, not per plan.

    A plan can carry two bindings on a connection that no node binds. An unused slot never
    reaches the broker's ambiguous lookup, so refusing it would wedge a run that works — the
    same over-scoping that has broken a working path here three times before.
    """
    from bounded_loops.graph.graph_composition import _preflight, _parse_manifest
    from bounded_loops.graph.graph_composition import _DEFAULT_POLICY_DIGEST
    from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph

    # The single-node manifest binds slot "model" only; the second candidate duplicates the
    # connection on a slot the graph never references.
    spare = dict(_byok_connections("api.openai.com")[0])
    spare.update(binding_id="b-spare", slot_id="unused")
    plan = compile_graph(
        _parse_manifest(_BYOK_MANIFEST, ".yaml"),
        CompileSnapshot(
            policy_digest=_DEFAULT_POLICY_DIGEST,
            package_digests=frozenset(),
            connections=tuple(_byok_connections("api.openai.com") + [spare]),
        ),
    )
    assert _preflight(plan, {_CONN_ID: _admitted_record("api.openai.com")}) is None


# ── test 3: credential absent from stored request document and artifacts ──────

def test_credential_never_stored_in_request_document_or_artifact(
    tmp_path: Path, tls_cert: tuple[Path, Path],
):
    """Credential value must not appear in:
    * the stored request document (ArtifactStore),
    * the response artifact,
    * any run-dir file (run-meta.json, connections.json, plan.json).
    """
    cert, key = tls_cert
    fake_api_key = "SUPER-SECRET-KEY-XYZ-99887766"
    mock_body = b'{"choices":[{"message":{"content":"ok"}}]}'

    with _MockProvider(body=mock_body, tls=(cert, key)) as provider:
        host = f"byok.test:{provider.port}"
        record = _admitted_record(host)

        client_ctx = ssl.create_default_context(cafile=str(cert))
        fake_credential = ProviderCredential({"authorization": f"Bearer {fake_api_key}"})
        cred_resolver = MappingCredentialResolver({_BINDING_ID: fake_credential})

        out = tmp_path / "run"
        rc = execute_graph_run(
            manifest_text=_BYOK_MANIFEST,
            manifest_suffix=".yaml",
            connections_raw=_byok_connections(host),
            node_prompts={"chat": "test prompt"},
            out_dir=out,
            run_id="run-1",
            admitted_connections={_CONN_ID: record},
            byok_egress_broker=_PinnedEgressBroker(),
            byok_credential_resolver=cred_resolver,
            byok_tls_context=client_ctx,
        )

    assert rc == 0

    # Scan every file in the run directory and artifact store for the secret.
    secret_bytes = fake_api_key.encode()
    for path in out.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert secret_bytes not in content, (
                f"secret key found in run-dir file {path.relative_to(out)}"
            )


# ── test 4: admitted_connections from_mapping rejects secret-shaped key names ─

def test_admitted_record_from_mapping_rejects_secret_shaped_key_names():
    from bounded_loops.graph.domain.errors import GraphValidationError
    with pytest.raises(GraphValidationError) as exc_info:
        AdmittedConnectionRecord.from_mapping({
            "connection_id": "c1",
            "api_secret": "OPENAI_KEY",   # key name "api_secret" contains "secret"
            "endpoint_scheme": "https",
            "endpoint_host": "api.openai.com",
            "endpoint_path": "/v1/chat/completions",
            "allowed_effect": "external_write",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "route_policy": {
                "policy_digest": "sha256:" + "a" * 64,
                "allowed_providers": ["openai"],
                "allowed_models": ["gpt-4o"],
                "allowed_regions": ["us-east-1"],
                "fallback_allowed": False,
                "route_verifiable": False,
                "data_class_max": "public",
            },
            "request_style": "openai_chat",
            "credential_env_var_name": "OPENAI_API_KEY",
        })
    assert exc_info.value.code == "secret_field"


# ── test 5: admitted_record from_mapping rejects non-env-var credential names ─

def test_admitted_record_from_mapping_rejects_secret_value_in_env_var_name():
    from bounded_loops.graph.domain.errors import GraphValidationError
    with pytest.raises(GraphValidationError) as exc_info:
        AdmittedConnectionRecord.from_mapping({
            "connection_id": "c1",
            "endpoint_scheme": "https",
            "endpoint_host": "api.openai.com",
            "endpoint_path": "/v1/chat/completions",
            "allowed_effect": "external_write",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "route_policy": {
                "policy_digest": "sha256:" + "a" * 64,
                "allowed_providers": ["openai"],
                "allowed_models": ["gpt-4o"],
                "allowed_regions": ["us-east-1"],
                "fallback_allowed": False,
                "route_verifiable": False,
                "data_class_max": "public",
            },
            "request_style": "openai_chat",
            # Someone accidentally put the real key value instead of the env var name.
            "credential_env_var_name": "sk-proj-ABCDEF1234567890abcdef",
        })
    assert exc_info.value.code == "secret_value"


# ── test 6: local-CLI path unchanged after extension ─────────────────────────

def _standin(tmp_path: Path, body: str) -> str:
    cli = tmp_path / "standin_cli"
    cli.write_text(body)
    cli.chmod(0o755)
    return str(cli)


def test_local_cli_path_still_works_after_byok_extension(tmp_path: Path):
    """The local_cli connector path must still work unchanged — no BYOK code is invoked."""
    import os
    from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile

    _MANIFEST = """\
api_version: "bounded-loops.dev/graph/v1"
graph_id: agent-run
version: "1.0.0"
nodes:
  - id: agent
    kind: research_claim
    inputs: {}
    outputs: {claim: text}
    budget: {max_attempts: 1, max_wallclock_s: 30}
    effects: [workspace_write]
    isolation: process_restricted
    connection_slot: model
edges: []
connection_slots: [{id: model, requires: [text_generation], data_class_max: public}]
policies: {data_class: public, fail_mode: fail_closed}
"""
    connections = [{
        "binding_id": "binding-1", "slot_id": "model", "connector_id": "local-cli",
        "connector_version": "1.0.0", "connection_id": "conn-1",
        "admission_digest": "sha256:" + "b" * 64, "route_policy_digest": "sha256:" + "c" * 64,
        "provider_id": "claude", "model_target": "subscription", "region": "local",
        "fallback": False, "capabilities": ["text_generation"], "data_class_max": "public",
        "allowed_effects": ["workspace_write"], "isolation": "process_restricted",
        "transport": "local_cli", "admitted": True,
    }]
    standin = _standin(tmp_path, "#!/bin/sh\nprintf 'LOCAL CLI REPLY: '; cat\n")
    out = tmp_path / "run"
    rc = execute_graph_run(
        manifest_text=_MANIFEST, manifest_suffix=".yaml",
        connections_raw=connections,
        node_prompts={"agent": "hello local cli"},
        out_dir=out, run_id="run-1",
        cli_profiles={"claude": CliProfile(standin)},
        environ={"PATH": os.environ.get("PATH", "")},
        admitted_connections=None,   # no BYOK records — https path not activated
    )
    assert rc == 0
    arena, meta = _arena(out)
    assert arena.run_state == "SUCCEEDED"
    assert meta["mode"] == "local_cli"
    node = arena.nodes[0]
    assert node.state == "SUCCEEDED"
    from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactRef
    store = LocalArtifactStore(out / "artifacts")
    with store.open(ArtifactRef(node.artifact_digests[0], _ORG, _PROJECT), ArtifactAccess(_ORG, _PROJECT)) as handle:
        assert handle.read() == b"LOCAL CLI REPLY: hello local cli"


# ── test 7: https is unaffected by the deployment's egress posture (Slice 2) ──
#
# https has its own independent, per-node ALLOWLIST construction (credential-broker-mediated,
# not OS-cage-mediated) in _build_policy — the deployment posture governs local_cli egress
# only. These runs succeed identically under ALLOWLIST (even with NO cage on this host — https
# never touches Seatbelt/egress-proxy) and under BROKER (https already IS the broker path).

def _egress_environ(tmp_path: Path, **posture: str) -> dict[str, str]:
    import os
    env = {"PATH": os.environ.get("PATH", ""), "BOUNDED_LOOPS_EGRESS_CONFIG": str(tmp_path / "nonexistent.json")}
    env.update(posture)
    return env


def test_byok_https_node_unaffected_by_allowlist_egress_posture_with_no_cage(
    tmp_path: Path, tls_cert: tuple[Path, Path],
):
    from bounded_loops.graph.adapters.enforcement.capabilities import PlatformCapabilities

    cert, key = tls_cert
    mock_body = b'{"choices":[{"message":{"content":"BYOK REPLY"}}]}'
    with _MockProvider(body=mock_body, tls=(cert, key)) as provider:
        host = f"byok.test:{provider.port}"
        record = _admitted_record(host)
        client_ctx = ssl.create_default_context(cafile=str(cert))
        cred_resolver = MappingCredentialResolver({_BINDING_ID: ProviderCredential({"authorization": "Bearer x"})})
        out = tmp_path / "run"
        rc = execute_graph_run(
            manifest_text=_BYOK_MANIFEST, manifest_suffix=".yaml",
            connections_raw=_byok_connections(host), node_prompts={"chat": "hi"},
            out_dir=out, run_id="run-1", admitted_connections={_CONN_ID: record},
            byok_egress_broker=_PinnedEgressBroker(), byok_credential_resolver=cred_resolver,
            byok_tls_context=client_ctx,
            environ=_egress_environ(
                tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="allowlist", BOUNDED_LOOPS_EGRESS_ALLOWLIST="unrelated.example.com",
            ),
            # No Seatbelt, no egress proxy — https must not care; only local_cli would.
            capabilities=PlatformCapabilities(platform="linux", docker_available=False, process_groups=True, rlimits=True),
        )
    assert rc == 0
    arena, meta = _arena(out)
    assert arena.run_state == "SUCCEEDED" and meta["mode"] == "https"


def test_byok_https_node_unaffected_by_broker_egress_posture(tmp_path: Path, tls_cert: tuple[Path, Path]):
    cert, key = tls_cert
    mock_body = b'{"choices":[{"message":{"content":"BYOK REPLY"}}]}'
    with _MockProvider(body=mock_body, tls=(cert, key)) as provider:
        host = f"byok.test:{provider.port}"
        record = _admitted_record(host)
        client_ctx = ssl.create_default_context(cafile=str(cert))
        cred_resolver = MappingCredentialResolver({_BINDING_ID: ProviderCredential({"authorization": "Bearer x"})})
        out = tmp_path / "run"
        rc = execute_graph_run(
            manifest_text=_BYOK_MANIFEST, manifest_suffix=".yaml",
            connections_raw=_byok_connections(host), node_prompts={"chat": "hi"},
            out_dir=out, run_id="run-1", admitted_connections={_CONN_ID: record},
            byok_egress_broker=_PinnedEgressBroker(), byok_credential_resolver=cred_resolver,
            byok_tls_context=client_ctx,
            environ=_egress_environ(tmp_path, BOUNDED_LOOPS_EGRESS_POSTURE="broker"),
        )
    assert rc == 0
    arena, meta = _arena(out)
    assert arena.run_state == "SUCCEEDED" and meta["mode"] == "https"
