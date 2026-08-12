"""LocalArtifactBody round-trip + tenant isolation over LocalArtifactStore (RB)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.graph.adapters.connectors.artifact_body import LocalArtifactBody
from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.domain.errors import GraphIntegrityError


def _body(tmp_path: Path) -> LocalArtifactBody:
    store = LocalArtifactStore(tmp_path / "artifacts")
    return LocalArtifactBody(store, organization_id="o", project_id="p", producer_attempt="attempt-1")


def test_store_then_fetch_roundtrips(tmp_path: Path):
    body = _body(tmp_path)
    digest = body.store(b'{"hello":"world"}')
    assert digest.startswith("sha256:")
    assert body.fetch(digest) == b'{"hello":"world"}'


def test_store_is_content_addressed(tmp_path: Path):
    body = _body(tmp_path)
    assert body.store(b"same") == body.store(b"same")


def test_fetch_unknown_digest_fails_closed(tmp_path: Path):
    body = _body(tmp_path)
    with pytest.raises(GraphIntegrityError):
        body.fetch("sha256:" + "0" * 64)


def test_a_different_tenant_cannot_fetch(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    owner = LocalArtifactBody(store, organization_id="o", project_id="p", producer_attempt="a")
    digest = owner.store(b"secret-bytes")
    intruder = LocalArtifactBody(store, organization_id="o2", project_id="p2", producer_attempt="a")
    with pytest.raises(GraphIntegrityError):
        intruder.fetch(digest)
