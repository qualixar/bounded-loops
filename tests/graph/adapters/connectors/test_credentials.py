"""Provider credential resolvers + secret redaction (RB). No real key is used."""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.connectors.credentials import (
    CredentialSource,
    EnvCredentialResolver,
    MappingCredentialResolver,
    ProviderCredential,
)
from bounded_loops.graph.domain.errors import GraphValidationError

_FAKE_KEY = "test-key-not-real-abcd1234"


def test_provider_credential_repr_redacts_the_secret():
    cred = ProviderCredential({"Authorization": f"Bearer {_FAKE_KEY}"})
    text = repr(cred)
    assert _FAKE_KEY not in text
    assert "redacted" in text


def test_provider_credential_requires_a_header():
    with pytest.raises(GraphValidationError):
        ProviderCredential({})


def test_provider_credential_rejects_a_reserved_header():
    with pytest.raises(GraphValidationError):
        ProviderCredential({"Host": "evil"})


def test_provider_credential_rejects_a_crlf_value():
    with pytest.raises(GraphValidationError):
        ProviderCredential({"Authorization": "Bearer x\r\nX-Evil: 1"})


def test_env_resolver_builds_the_anthropic_header_from_the_environment():
    resolver = EnvCredentialResolver(
        {"anthropic-1": CredentialSource("ANTHROPIC_API_KEY", "x-api-key", extra_headers={"anthropic-version": "2023-06-01"})},
        environ={"ANTHROPIC_API_KEY": _FAKE_KEY},
    )
    cred = resolver.resolve(binding_id="anthropic-1", destination="api.anthropic.com")
    assert cred is not None
    assert cred.headers["x-api-key"] == _FAKE_KEY
    assert cred.headers["anthropic-version"] == "2023-06-01"


def test_env_resolver_applies_the_openai_bearer_prefix():
    resolver = EnvCredentialResolver(
        {"openai-1": CredentialSource("OPENAI_API_KEY", "Authorization", value_prefix="Bearer ")},
        environ={"OPENAI_API_KEY": _FAKE_KEY},
    )
    cred = resolver.resolve(binding_id="openai-1", destination="api.openai.com")
    assert cred is not None
    assert cred.headers["Authorization"] == f"Bearer {_FAKE_KEY}"


def test_env_resolver_returns_none_for_an_unconfigured_binding():
    resolver = EnvCredentialResolver({}, environ={})
    assert resolver.resolve(binding_id="unknown", destination="d") is None


def test_env_resolver_fails_closed_when_a_configured_key_is_missing():
    resolver = EnvCredentialResolver(
        {"openai-1": CredentialSource("OPENAI_API_KEY", "Authorization", value_prefix="Bearer ")},
        environ={},
    )
    with pytest.raises(GraphValidationError):
        resolver.resolve(binding_id="openai-1", destination="api.openai.com")


def test_env_resolver_error_does_not_leak_a_key():
    resolver = EnvCredentialResolver(
        {"openai-1": CredentialSource("OPENAI_API_KEY", "Authorization", value_prefix="Bearer ")},
        environ={"OPENAI_API_KEY": ""},
    )
    with pytest.raises(GraphValidationError) as caught:
        resolver.resolve(binding_id="openai-1", destination="d")
    assert _FAKE_KEY not in str(caught.value)


def test_mapping_resolver_returns_the_mapped_credential_or_none():
    cred = ProviderCredential({"Authorization": f"Bearer {_FAKE_KEY}"})
    resolver = MappingCredentialResolver({"b1": cred})
    assert resolver.resolve(binding_id="b1", destination="d") is cred
    assert resolver.resolve(binding_id="b2", destination="d") is None


def test_credential_source_rejects_a_bad_extra_header():
    with pytest.raises(GraphValidationError):
        CredentialSource("V", "x-api-key", extra_headers={"Host": "evil"})
