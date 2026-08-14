"""Hardening regression tests for AdmittedConnectionRecord (dual Grok+Muse audit findings).

These pin the invariants that must hold on EVERY construction path — not only ``from_mapping`` —
so a direct ``AdmittedConnectionRecord(...)`` can never smuggle a secret value into the
credential env-var name, or slip a malformed endpoint / expiry past the guards.
"""

from __future__ import annotations

import pytest

from bounded_loops.graph.adapters.connectors.admitted_connection_request import (
    AdmittedConnectionRecord,
    split_endpoint_host,
)
from bounded_loops.graph.graph_composition import _https_isolation
from bounded_loops.graph.domain.authoring import DataClass, Effect, IsolationLevel
from bounded_loops.graph.domain.connections import RoutePolicy
from bounded_loops.graph.domain.errors import GraphValidationError

_POLICY = RoutePolicy(
    policy_digest="sha256:" + "c" * 64,
    allowed_providers=frozenset({"openai"}),
    allowed_models=frozenset({"gpt-4o-mini"}),
    allowed_regions=frozenset({"global"}),
    fallback_allowed=False,
    route_verifiable=True,
    data_class_max=DataClass.PUBLIC,
)


def _record(**overrides):
    kwargs = dict(
        connection_id="conn-1",
        endpoint_scheme="https",
        endpoint_host="api.openai.com",
        endpoint_path="/v1/chat/completions",
        allowed_effect=Effect.EXTERNAL_WRITE,
        expires_at="2999-01-01T00:00:00+00:00",
        route_policy=_POLICY,
        request_style="openai_chat",
        credential_env_var_name="OPENAI_API_KEY",
    )
    kwargs.update(overrides)
    return AdmittedConnectionRecord(**kwargs)


def test_valid_record_constructs():
    assert _record().connection_id == "conn-1"


def test_direct_ctor_rejects_secret_shaped_env_var_name():
    # A hyphenated API key pasted as the "env var name" must be rejected on the DIRECT ctor path,
    # not only via from_mapping — the primary dual-audit finding.
    with pytest.raises(GraphValidationError):
        _record(credential_env_var_name="sk-proj-ABCDEF0123456789ghijkl")


def test_rejects_unsupported_scheme():
    with pytest.raises(GraphValidationError):
        _record(endpoint_scheme="ftp")


def test_rejects_path_without_leading_slash():
    with pytest.raises(GraphValidationError):
        _record(endpoint_path="v1/chat")


def test_rejects_malformed_port():
    with pytest.raises(GraphValidationError):
        _record(endpoint_host="api.openai.com:NOTPORT")


def test_rejects_non_iso_expiry():
    with pytest.raises(GraphValidationError):
        _record(expires_at="not-a-date")


def test_rejects_naive_expiry():
    with pytest.raises(GraphValidationError):
        _record(expires_at="2999-01-01T00:00:00")  # no timezone


def test_split_endpoint_host_variants():
    assert split_endpoint_host("api.openai.com") == ("api.openai.com", 443)
    assert split_endpoint_host("localhost:8443") == ("localhost", 8443)
    assert split_endpoint_host("[::1]:9000") == ("::1", 9000)
    with pytest.raises(GraphValidationError):
        split_endpoint_host("host:70000")  # port out of range


def test_https_isolation_never_downgrades():
    # Lifts to the CONTAINER_RESTRICTED floor, but never below a higher declared tier.
    assert _https_isolation(IsolationLevel.PROCESS_RESTRICTED) == IsolationLevel.CONTAINER_RESTRICTED
    assert _https_isolation(IsolationLevel.CONTAINER_RESTRICTED) == IsolationLevel.CONTAINER_RESTRICTED
    assert _https_isolation(IsolationLevel.CUSTOMER_MANAGED_WORKER) == IsolationLevel.CUSTOMER_MANAGED_WORKER
