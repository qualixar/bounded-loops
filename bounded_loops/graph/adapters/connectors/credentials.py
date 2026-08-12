"""Deployment-owned provider credential resolution for the BYOK connector (RB).

The forwarder asks a resolver for the auth headers to attach to ONE call, keyed by the
lease's ``binding_id``. The credential value is read out-of-band from the deployment's OWN
store (environment / keychain / KMS) — never from the graph, the plan, receipts, or
artifacts, and never by the engine control plane. ``ProviderCredential`` redacts its repr,
so a secret can never land in a log line or a traceback frame.

Two real resolvers ship:
  * ``EnvCredentialResolver`` — BYOK: read the key from a configured environment variable at
    call time and build the provider's auth header. A binding with NO configured source is
    genuinely no-auth (``None``); a binding WITH a source whose variable is missing is a
    deployment MISCONFIGURATION and fails closed — never a silent unauthenticated call.
  * ``MappingCredentialResolver`` — a deployment that resolves credentials out-of-band itself
    supplies a ready ``ProviderCredential`` per binding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from types import MappingProxyType
from typing import Mapping, Protocol

from bounded_loops.graph.adapters.connectors._headers import validate_header_name, validate_header_value
from bounded_loops.graph.domain.errors import GraphValidationError


@dataclass(frozen=True, repr=False)
class ProviderCredential:
    """Auth headers for one provider call. The VALUES are secret; the repr is redacted."""

    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.headers, Mapping) or not self.headers:
            raise GraphValidationError("provider_credential", "/headers", "a credential must carry at least one header")
        cleaned: dict[str, str] = {}
        for name, value in self.headers.items():
            validate_header_name(name, "/headers")
            validate_header_value(value, "/headers")
            cleaned[name] = value
        object.__setattr__(self, "headers", MappingProxyType(cleaned))

    def __repr__(self) -> str:  # never render the secret values
        return f"ProviderCredential(headers=<{len(self.headers)} redacted>)"


class CredentialResolverPort(Protocol):
    """Resolve the provider credential for one lease's binding, or ``None`` for no-auth."""

    def resolve(self, *, binding_id: str, destination: str) -> ProviderCredential | None: ...


@dataclass(frozen=True)
class CredentialSource:
    """How to build one binding's credential from an environment variable."""

    env_var: str
    header_name: str
    value_prefix: str = ""
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.env_var, str) or not self.env_var:
            raise GraphValidationError("credential_source", "/env_var", "env_var must be a non-empty string")
        validate_header_name(self.header_name, "/header_name")
        if not isinstance(self.value_prefix, str):
            raise GraphValidationError("credential_source", "/value_prefix", "value_prefix must be a string")
        cleaned: dict[str, str] = {}
        for name, value in dict(self.extra_headers).items():
            validate_header_name(name, "/extra_headers")
            validate_header_value(value, "/extra_headers")
            cleaned[name] = value
        object.__setattr__(self, "extra_headers", MappingProxyType(cleaned))


class EnvCredentialResolver:
    """Resolve a BYOK credential by reading the configured environment variable at call time."""

    def __init__(self, sources: Mapping[str, CredentialSource], *, environ: Mapping[str, str] | None = None) -> None:
        self._sources = dict(sources)
        self._environ = environ

    def resolve(self, *, binding_id: str, destination: str) -> ProviderCredential | None:
        source = self._sources.get(binding_id)
        if source is None:
            return None
        environ = self._environ if self._environ is not None else os.environ
        key = environ.get(source.env_var)
        if not key:
            raise GraphValidationError(
                "provider_credential", "/credential",
                f"credential environment variable {source.env_var!r} is not set",
            )
        headers = {source.header_name: f"{source.value_prefix}{key}", **dict(source.extra_headers)}
        return ProviderCredential(headers)


class MappingCredentialResolver:
    """Resolve from a static ``binding_id -> ProviderCredential`` map supplied by the deployment."""

    def __init__(self, credentials: Mapping[str, ProviderCredential]) -> None:
        self._by_binding = dict(credentials)

    def resolve(self, *, binding_id: str, destination: str) -> ProviderCredential | None:
        return self._by_binding.get(binding_id)
