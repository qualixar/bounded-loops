"""Deployment-supplied admitted-connection AUTHORITY record and BYOK request builder.

Unlike the compiler's ConnectionCandidate (which carries only non-secret, portable metadata),
``AdmittedConnectionRecord`` is the deployment-owned RUNTIME authority: it names the live
endpoint, the credential ENV-VAR name (never the secret value), and the expiry boundary.
It is the "second connector mode" seam — the thing the compiler's plan snapshot cannot carry.

``AdmittedConnectionRequestBuilder`` implements ``ConnectorRequestPort`` (connector_worker.py
line 41-46) and is the only place a REAL ``ExecutionGrant`` is issued for a BYOK/HTTP node:
  1. Resolve node.binding_id → binding → connection_id → the supplied AdmittedConnectionRecord.
  2. Construct a real ``AdmittedConnection(state=ADMITTED)`` from the record.
  3. Call ``issue_execution_grant`` (connections.py line 194) with that real connection.
  4. Build the content-addressed request document via a request-style builder.
  5. Store the document in the artifact store; return ``ConnectorCall(grant, invocation)``.

Anti-dummy rule (non-negotiable): destination, effects, and expires_at in the grant MUST
come from the supplied record — never fabricated from the plan/binding alone.  A missing
or absent record raises ``GraphIntegrityError`` immediately; a dummy grant is never minted.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.connectors.openai_chat_request import build_openai_chat_request
from bounded_loops.graph.adapters.connectors.request_document import (
    ConnectorRequestDocument,
    encode_request_document,
)
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.application.connections import ExecutionGrantRequest, issue_execution_grant
from bounded_loops.graph.application.connector_forward import ConnectorInvocation
from bounded_loops.graph.application.execution_policy import ExecutionEnvelope
from bounded_loops.graph.domain.authoring import DataClass, Effect
from bounded_loops.graph.domain.connections import (
    AdmittedConnection,
    ConnectionState,
    RoutePolicy,
)
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# Import ConnectorCall locally to avoid a circular import via connector_worker -> connector_forward
from bounded_loops.graph.application.connector_worker import ConnectorCall

# Reuse compile_graph / validate_graph patterns for secret detection. "credential" is deliberately
# NOT here — the legitimate field ``credential_env_var_name`` contains it; "key" alone is too broad.
_SECRET_WORDS = frozenset({"secret", "token", "password", "apikey", "api_key"})

# An env-var name is [letter][letter/digit/underscore]{0..99}.  A value that does NOT match
# is likely an actual credential value rather than an env-var name reference.
_ENV_VAR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}$")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_ALLOWED_SCHEMES = frozenset({"https", "http"})

_KNOWN_STYLES = frozenset({"openai_chat"})

_REQUIRED_FIELDS = frozenset({
    "connection_id", "endpoint_scheme", "endpoint_host", "endpoint_path",
    "allowed_effect", "expires_at", "route_policy", "request_style", "credential_env_var_name",
})
_OPTIONAL_FIELDS = frozenset({
    "credential_header_name", "credential_header_prefix", "organization_id",
})


@dataclass(frozen=True)
class AdmittedConnectionRecord:
    """Deployment-supplied runtime AUTHORITY for one BYOK/HTTP connector connection.

    This record is the thing the compiler's ConnectionCandidate does NOT carry: it names
    the real endpoint (scheme/host/path), the credential env-var NAME (never the value),
    the execution expiry, and the RoutePolicy needed to construct an AdmittedConnection for
    ``issue_execution_grant``.  It must be supplied at RUN TIME by the deployment — it is
    never baked into the portable graph manifest.

    Secret-safety invariants:
    * ``credential_env_var_name`` holds only the NAME of an environment variable, never the
      key value itself.  ``from_mapping`` rejects values that do not look like valid env-var
      names, and rejects any field whose KEY contains a secret word.
    * No credential value is stored here or derived here; the real credential is read by
      ``EnvCredentialResolver`` from the process environment at call time.
    """

    connection_id: str
    endpoint_scheme: str             # "https" (or "http" for non-TLS local testing only)
    endpoint_host: str               # "api.openai.com" or "localhost:8443" — host[:port]
    endpoint_path: str               # "/v1/chat/completions"
    allowed_effect: Effect
    expires_at: str                  # ISO-8601 expiry for the AdmittedConnection + grant
    route_policy: RoutePolicy
    request_style: str               # "openai_chat" (more styles can be added)
    credential_env_var_name: str     # ENV VAR NAME only — e.g. "OPENAI_API_KEY"
    credential_header_name: str = "Authorization"
    credential_header_prefix: str = "Bearer "
    organization_id: str = "local-org"

    def __post_init__(self) -> None:
        # Enforce the record's security invariants on EVERY construction path — not only
        # ``from_mapping`` — so a direct ``AdmittedConnectionRecord(...)`` can never smuggle a
        # secret value into ``credential_env_var_name`` or slip an unsupported scheme/style past
        # the guards (dual-audit finding: validation must not live only in the classmethod).
        if not isinstance(self.connection_id, str) or not self.connection_id:
            raise GraphValidationError("admitted_connection", "/connection_id", "connection_id must be a non-empty string")
        if self.endpoint_scheme not in _ALLOWED_SCHEMES:
            raise GraphValidationError("admitted_connection", "/endpoint_scheme", f"endpoint_scheme must be one of {sorted(_ALLOWED_SCHEMES)}")
        if not isinstance(self.endpoint_host, str) or not self.endpoint_host:
            raise GraphValidationError("admitted_connection", "/endpoint_host", "endpoint_host must be a non-empty string")
        split_endpoint_host(self.endpoint_host)  # validate host[:port] shape — raises on a bad port
        if not isinstance(self.endpoint_path, str) or not self.endpoint_path.startswith("/"):
            raise GraphValidationError("admitted_connection", "/endpoint_path", "endpoint_path must begin with '/'")
        if not isinstance(self.allowed_effect, Effect):
            raise GraphValidationError("admitted_connection", "/allowed_effect", "allowed_effect must be an Effect")
        if not isinstance(self.route_policy, RoutePolicy):
            raise GraphValidationError("admitted_connection", "/route_policy", "route_policy must be a RoutePolicy")
        if self.request_style not in _KNOWN_STYLES:
            raise GraphValidationError("admitted_connection", "/request_style", f"request_style must be one of {sorted(_KNOWN_STYLES)}")
        # credential_env_var_name is used ONLY as a key into the process environment: it is never
        # transmitted as a credential and never persisted to the run directory. This shape guard
        # rejects the common secret forms (hyphens / dots / whitespace) as an ACCIDENT guard — the
        # real protection is that the value only ever indexes os.environ, never the credential wire.
        if not isinstance(self.credential_env_var_name, str) or not _ENV_VAR_RE.fullmatch(self.credential_env_var_name):
            raise GraphValidationError("secret_value", "/credential_env_var_name", "credential_env_var_name must be an environment variable NAME (e.g. 'OPENAI_API_KEY'), not a secret value")
        _require_iso8601(self.expires_at)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "AdmittedConnectionRecord":
        """Parse and validate a record from a plain mapping (e.g. JSON deserialization).

        Rejects:
        * any key whose lowercased name contains a secret word ("secret", "token", "password")
          — reuses the pattern from compile_graph.ConnectionCandidate.from_mapping (line 68)
        * a ``credential_env_var_name`` value that does not match the env-var name pattern,
          guarding against accidentally storing a real credential value
        * unknown or missing required fields
        """
        # --- key-name secret check (compile_graph pattern) ---
        forbidden_keys = {
            key for key in raw
            if any(word in str(key).lower() for word in _SECRET_WORDS)
        }
        if forbidden_keys:
            raise GraphValidationError(
                "secret_field", "/admitted_connection",
                "admitted connection record contains secret-shaped field name(s): "
                + ", ".join(sorted(forbidden_keys)),
            )

        # --- shape check ---
        given = set(raw)
        missing = _REQUIRED_FIELDS - given
        if missing:
            raise GraphValidationError(
                "admitted_connection", "/admitted_connection",
                "admitted connection record is missing required fields: " + ", ".join(sorted(missing)),
            )
        unknown = given - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
        if unknown:
            raise GraphValidationError(
                "admitted_connection", "/admitted_connection",
                "admitted connection record has unknown fields: " + ", ".join(sorted(unknown)),
            )

        # --- credential_env_var_name value check ---
        env_var = raw.get("credential_env_var_name", "")
        if not isinstance(env_var, str) or not _ENV_VAR_RE.fullmatch(env_var):
            raise GraphValidationError(
                "secret_value", "/admitted_connection/credential_env_var_name",
                "credential_env_var_name must be an environment variable name "
                "(e.g. 'OPENAI_API_KEY'), not a secret value",
            )

        # --- field parsing ---
        connection_id = _str(raw["connection_id"], "connection_id")
        endpoint_scheme = _str(raw["endpoint_scheme"], "endpoint_scheme").lower()
        if endpoint_scheme not in _ALLOWED_SCHEMES:
            raise GraphValidationError(
                "admitted_connection", "/endpoint_scheme",
                f"endpoint_scheme must be one of {sorted(_ALLOWED_SCHEMES)}",
            )
        endpoint_host = _str(raw["endpoint_host"], "endpoint_host")
        endpoint_path = _str(raw["endpoint_path"], "endpoint_path")
        if not endpoint_path.startswith("/"):
            raise GraphValidationError(
                "admitted_connection", "/endpoint_path",
                "endpoint_path must begin with '/'",
            )
        allowed_effect = _effect(raw["allowed_effect"])
        expires_at = _str(raw["expires_at"], "expires_at")
        route_policy = _route_policy(raw["route_policy"])
        request_style = _str(raw["request_style"], "request_style")
        if request_style not in _KNOWN_STYLES:
            raise GraphValidationError(
                "admitted_connection", "/request_style",
                f"request_style must be one of {sorted(_KNOWN_STYLES)}",
            )

        return cls(
            connection_id=connection_id,
            endpoint_scheme=endpoint_scheme,
            endpoint_host=endpoint_host,
            endpoint_path=endpoint_path,
            allowed_effect=allowed_effect,
            expires_at=expires_at,
            route_policy=route_policy,
            request_style=request_style,
            credential_env_var_name=env_var,
            credential_header_name=_str(
                raw.get("credential_header_name", "Authorization"), "credential_header_name",
            ),
            credential_header_prefix=_str(
                raw.get("credential_header_prefix", "Bearer "), "credential_header_prefix",
            ),
            organization_id=_str(
                raw.get("organization_id", "local-org"), "organization_id",
            ),
        )


class AdmittedConnectionRequestBuilder:
    """``ConnectorRequestPort`` for BYOK/HTTP connector nodes.

    Resolves a graph node's binding → connection_id → ``AdmittedConnectionRecord``, constructs
    a real ``AdmittedConnection(state=ADMITTED)`` from the record, issues a REAL
    ``ExecutionGrant`` via ``issue_execution_grant`` (never fabricated from the plan), builds and
    stores the content-addressed request DOCUMENT, and returns a ``ConnectorCall``.

    Anti-dummy invariant: a missing record is a hard ``GraphIntegrityError``; a grant is never
    minted without a matching ``AdmittedConnectionRecord`` from the supplied authority mapping.
    """

    def __init__(
        self,
        *,
        records: Mapping[str, AdmittedConnectionRecord],
        artifact_store: LocalArtifactStore,
        run_id: str,
        node_prompts: Mapping[str, str],
        organization_id: str = "local-org",
        project_id: str = "local-project",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._records = dict(records)
        self._artifact_store = artifact_store
        self._run_id = run_id
        self._node_prompts = dict(node_prompts)
        self._organization_id = organization_id
        self._project_id = project_id
        self._now = now

    def build(
        self, *, plan: ExecutionPlan, node: PlannedNode, envelope: ExecutionEnvelope,
    ) -> ConnectorCall:
        """Assemble a BYOK connector call for one https-transport node.

        Seam contract (connector_worker.py line 41-46):
        * Returns ``ConnectorCall(grant, invocation)``
        * grant: issued via ``issue_execution_grant`` from a REAL ``AdmittedConnectionRecord``
        * invocation: content-addressed (payload_digest = sha256 of the stored request document)
        * No credential value is produced here — only an env-var-name reference flows through
        """
        if node.binding_id is None:
            raise GraphIntegrityError(
                f"connector node {node.node_id!r} has no binding_id — cannot issue a grant"
            )

        # Resolve binding from plan (connector_worker.py line 90)
        binding = next(
            (b for b in plan.connection_bindings if b.binding_id == node.binding_id),
            None,
        )
        if binding is None:
            raise GraphIntegrityError(
                f"connector node {node.node_id!r}: binding_id {node.binding_id!r} not found in plan"
            )

        # --- ANTI-DUMMY RULE: get the REAL authority record or fail closed ---
        record = self._records.get(binding.connection_id)
        if record is None:
            raise GraphIntegrityError(
                f"connector node {node.node_id!r}: no admitted-connection record for "
                f"connection_id {binding.connection_id!r} — a grant cannot be issued "
                "(supply one via admitted_connections or the --admitted CLI flag)"
            )

        # Build a real AdmittedConnection(state=ADMITTED) from the record.
        # consent_digest and evidence_digest are content-addressed from the record fields so
        # they are deterministic, non-secret, and unique to this connection identity.
        connection = AdmittedConnection(
            connection_id=record.connection_id,
            organization_id=self._organization_id,
            connector_id="byok-http",
            connector_version="1.0.0",
            consent_digest=_sha256(f"consent:{record.connection_id}:{record.expires_at}"),
            evidence_digest=_sha256(f"evidence:{record.connection_id}:{record.endpoint_host}"),
            expires_at=record.expires_at,
            capabilities=frozenset({"text_generation"}),
            effects=frozenset({record.allowed_effect}),
            transport=record.endpoint_scheme,
            data_path=record.endpoint_path,
            route_policy=record.route_policy,
            state=ConnectionState.ADMITTED,
        )

        # Issue the REAL grant from the REAL admitted connection.
        # expires_at == connection.expires_at so grant_expiry <= connection_expiry always holds.
        now_dt = self._now() if self._now is not None else datetime.now(timezone.utc)
        grant = issue_execution_grant(
            ExecutionGrantRequest(
                run_id=self._run_id,
                node_id=node.node_id,
                attempt=1,
                connection=connection,
                effects=frozenset({record.allowed_effect}),
                destinations=frozenset({record.endpoint_host}),
                expires_at=record.expires_at,
            ),
            now=now_dt,
        )

        # Get the runtime prompt — never baked into the portable graph.
        prompt = self._node_prompts.get(node.node_id)
        if prompt is None:
            raise GraphIntegrityError(
                f"connector node {node.node_id!r}: no runtime prompt supplied "
                "(pass node_id -> prompt via node_prompts)"
            )

        # Build the request DOCUMENT.  model_target comes from the graph binding (not the record),
        # so routing is graph-controlled.  Credential header is NOT included here; the forwarder
        # injects it from the resolver.
        document = _build_document(record, binding.model_target, prompt)

        # Store the document content-addressed in the artifact store.
        body = encode_request_document(document)
        artifact_body = LocalArtifactBody(
            self._artifact_store,
            organization_id=self._organization_id,
            project_id=self._project_id,
            producer_attempt=f"{self._run_id}-{node.node_id}-byok",
        )
        payload_digest = artifact_body.store(body)

        invocation = ConnectorInvocation(
            destination=record.endpoint_host,
            method="POST",
            effect=record.allowed_effect,
            payload_digest=payload_digest,
            declared_bytes=len(body),
        )
        return ConnectorCall(grant=grant, invocation=invocation)


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_document(
    record: AdmittedConnectionRecord, model_target: str, prompt: str,
) -> ConnectorRequestDocument:
    if record.request_style == "openai_chat":
        return build_openai_chat_request(
            model=model_target, prompt=prompt, path=record.endpoint_path,
            scheme=record.endpoint_scheme,
        )
    raise GraphIntegrityError(
        f"unknown request_style {record.request_style!r} — only {sorted(_KNOWN_STYLES)} supported"
    )


def split_endpoint_host(endpoint_host: str) -> tuple[str, int]:
    """Parse ``host`` or ``host:port`` (default 443); fail CLOSED on a malformed port.

    Handles a bracketed IPv6 literal (``[::1]:8443``). Raises ``GraphValidationError`` rather than
    a bare ``ValueError`` so a malformed record is a clean, fail-closed error — not a crash.
    """
    host = endpoint_host
    if host.startswith("["):
        close = host.find("]")
        if close == -1:
            raise GraphValidationError("admitted_connection", "/endpoint_host", "malformed IPv6 endpoint_host")
        ipv6 = host[1:close]
        rest = host[close + 1:]
        if not rest:
            return ipv6, 443
        if rest.startswith(":"):
            return ipv6, _port_num(rest[1:])
        raise GraphValidationError("admitted_connection", "/endpoint_host", "malformed endpoint_host")
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if not head:
            raise GraphValidationError("admitted_connection", "/endpoint_host", "endpoint_host must have a host part")
        return head, _port_num(tail)
    return host, 443


def _port_num(text: str) -> int:
    if not text.isdigit():
        raise GraphValidationError("admitted_connection", "/endpoint_host", "endpoint_host port must be numeric")
    value = int(text)
    if not 1 <= value <= 65535:
        raise GraphValidationError("admitted_connection", "/endpoint_host", "endpoint_host port must be 1..65535")
    return value


def _require_iso8601(value: object) -> None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise GraphValidationError("admitted_connection", "/expires_at", "expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise GraphValidationError("admitted_connection", "/expires_at", "expires_at must include a timezone")


def _sha256(data: str) -> str:
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GraphValidationError(
            "admitted_connection", f"/{name}", f"{name} must be a non-empty string"
        )
    return value


def _effect(value: object) -> Effect:
    if isinstance(value, Effect):
        return value
    if isinstance(value, str):
        try:
            return Effect(value)
        except ValueError:
            pass
    raise GraphValidationError(
        "admitted_connection", "/allowed_effect",
        f"allowed_effect must be one of {[e.value for e in Effect]}",
    )


def _route_policy(value: object) -> RoutePolicy:
    if isinstance(value, RoutePolicy):
        return value
    if not isinstance(value, Mapping):
        raise GraphValidationError(
            "admitted_connection", "/route_policy", "route_policy must be a mapping"
        )
    raw = dict(value)
    policy_digest = _str(raw.get("policy_digest", ""), "route_policy/policy_digest")
    if not _DIGEST_RE.fullmatch(policy_digest):
        raise GraphValidationError(
            "admitted_connection", "/route_policy/policy_digest",
            "route_policy/policy_digest must be a sha256 digest",
        )
    data_class_max_raw = raw.get("data_class_max", "")
    try:
        data_class_max = DataClass(data_class_max_raw)
    except ValueError:
        raise GraphValidationError(
            "admitted_connection", "/route_policy/data_class_max",
            f"data_class_max must be one of {[c.value for c in DataClass]}",
        )
    return RoutePolicy(
        policy_digest=policy_digest,
        allowed_providers=_frozenset_str(raw.get("allowed_providers", []), "allowed_providers"),
        allowed_models=_frozenset_str(raw.get("allowed_models", []), "allowed_models"),
        allowed_regions=_frozenset_str(raw.get("allowed_regions", []), "allowed_regions"),
        fallback_allowed=bool(raw.get("fallback_allowed", False)),
        route_verifiable=bool(raw.get("route_verifiable", False)),
        data_class_max=data_class_max,
    )


def _frozenset_str(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, (list, tuple, frozenset, set)):
        raise GraphValidationError(
            "admitted_connection", f"/route_policy/{name}",
            f"{name} must be a list of strings",
        )
    items = list(value)
    if not items or not all(isinstance(item, str) and item for item in items):
        raise GraphValidationError(
            "admitted_connection", f"/route_policy/{name}",
            f"{name} must contain at least one non-empty string",
        )
    return frozenset(items)
