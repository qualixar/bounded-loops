from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO

import pytest

from bounded_loops.graph.adapters.persistence.artifact_store import LocalArtifactStore
from bounded_loops.graph.adapters.persistence.artifact_verifier import LocalArtifactVerifier
from bounded_loops.graph.domain.artifacts import ArtifactAccess, ArtifactPolicy, ArtifactState
from bounded_loops.graph.domain.errors import GraphIntegrityError
from bounded_loops.graph.domain.events import GraphRunIdentity


def _policy() -> ArtifactPolicy:
    return ArtifactPolicy(
        organization_id="org-1", project_id="project-1", producer_attempt="attempt-1",
        media_type="application/json", sensitivity="internal", retention_class="short",
    )


def test_artifact_store_uses_content_digest_and_tenant_scoped_reads(tmp_path):
    store = LocalArtifactStore(tmp_path)
    record = store.put(BytesIO(b'{"answer":42}'), _policy())

    assert record.digest.startswith("sha256:")
    assert record.state is ArtifactState.ACTIVE
    assert store.open(record.ref, ArtifactAccess("org-1", "project-1")).read() == b'{"answer":42}'
    with pytest.raises(GraphIntegrityError, match="unauthorized"):
        store.open(record.ref, ArtifactAccess("org-2", "project-1"))


def test_artifact_tombstone_removes_bytes_but_retains_immutable_metadata(tmp_path):
    store = LocalArtifactStore(tmp_path)
    record = store.put(BytesIO(b"sensitive"), _policy())

    tombstoned = store.tombstone(record.ref, "expired")

    assert tombstoned.state is ArtifactState.TOMBSTONED
    assert tombstoned.digest == record.digest
    with pytest.raises(GraphIntegrityError, match="not active"):
        store.open(record.ref, ArtifactAccess("org-1", "project-1"))


def test_artifact_verifier_requires_active_bytes_in_the_run_tenant(tmp_path):
    store = LocalArtifactStore(tmp_path)
    record = store.put(BytesIO(b"gate input"), _policy())
    identity = GraphRunIdentity(
        organization_id="org-1", project_id="project-1", run_id="run-1",
        graph_digest="sha256:" + "a" * 64, plan_digest="sha256:" + "b" * 64,
        policy_digest="sha256:" + "c" * 64,
    )

    LocalArtifactVerifier(store).verify(identity=identity, digests=(record.digest,))
    foreign = GraphRunIdentity(
        organization_id="org-2", project_id="project-1", run_id="run-1",
        graph_digest=identity.graph_digest, plan_digest=identity.plan_digest,
        policy_digest=identity.policy_digest,
    )
    with pytest.raises(GraphIntegrityError, match="unauthorized"):
        LocalArtifactVerifier(store).verify(identity=foreign, digests=(record.digest,))


def test_retention_expiry_respects_a_tenant_scoped_legal_hold(tmp_path):
    store = LocalArtifactStore(tmp_path)
    policy = ArtifactPolicy(
        organization_id="org-1", project_id="project-1", producer_attempt="attempt-1",
        media_type="application/json", sensitivity="internal", retention_class="short",
        expires_at="2026-08-09T00:00:00Z", legal_hold_allowed=True,
    )
    record = store.put(BytesIO(b"retain me"), policy)

    held = store.set_legal_hold(record.ref, True)
    assert held.legal_hold is True
    with pytest.raises(GraphIntegrityError, match="legal hold"):
        store.tombstone(record.ref, "manual_delete")
    assert store.sweep_expired(datetime(2026, 8, 10, tzinfo=timezone.utc)) == ()
    assert store.open(record.ref, ArtifactAccess("org-1", "project-1")).read() == b"retain me"

    store.set_legal_hold(record.ref, False)
    expired = store.sweep_expired(datetime(2026, 8, 10, tzinfo=timezone.utc))

    assert len(expired) == 1
    assert expired[0].state is ArtifactState.TOMBSTONED
    assert expired[0].tombstone_reason == "retention_expired"
    with pytest.raises(GraphIntegrityError, match="not active"):
        store.open(record.ref, ArtifactAccess("org-1", "project-1"))
