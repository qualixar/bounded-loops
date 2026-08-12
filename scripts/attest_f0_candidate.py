"""Create a content-addressed integrated-F0 default-suite attestation.

This is deliberately a release-evidence helper, not runtime code.  It binds
two hermetic default-suite runs to the exact dirty-tree candidate without
requiring an unauthorized integration commit.  Evidence output is excluded
from the subject digest, preventing the attestation from changing itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = Path(".backup/graph-engine/evidence")
DEFAULT_COMMAND = ("uv", "run", "pytest", "-q")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _run(argv: tuple[str, ...], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
    )


def _untracked_subject_files(repo_root: Path) -> tuple[Path, ...]:
    result = _run(("git", "ls-files", "--others", "--exclude-standard"), cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    paths = []
    for line in result.stdout.decode("utf-8").splitlines():
        path = Path(line)
        if not path.is_relative_to(EVIDENCE_ROOT):
            paths.append(path)
    return tuple(sorted(paths))


def candidate_digest(repo_root: Path) -> tuple[str, dict[str, Any]]:
    diff = _run(("git", "diff", "--binary", "HEAD"), cwd=repo_root)
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.decode("utf-8", errors="replace"))
    manifest: dict[str, Any] = {
        "tracked_diff_sha256": _sha256_bytes(diff.stdout),
        "untracked_files": {},
    }
    for relative_path in _untracked_subject_files(repo_root):
        manifest["untracked_files"][relative_path.as_posix()] = _sha256_bytes(
            (repo_root / relative_path).read_bytes()
        )
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(canonical), manifest


def _environment(home: Path) -> tuple[dict[str, str], dict[str, str]]:
    environment = os.environ.copy()
    environment.pop("BOUNDED_LOOPS_TRUST_STORE", None)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    recorded = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "BOUNDED_LOOPS_TRUST_STORE": "<unset>",
    }
    return environment, recorded


def _version(argv: tuple[str, ...], repo_root: Path) -> str:
    result = _run(argv, cwd=repo_root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="replace").strip()


def _attest_run(repo_root: Path, sequence: int) -> dict[str, Any]:
    digest_before, _ = candidate_digest(repo_root)
    with tempfile.TemporaryDirectory(prefix="bounded-loops-f0-home-") as temp_home:
        home = Path(temp_home)
        environment, recorded_environment = _environment(home)
        started_at = datetime.now(UTC).isoformat()
        result = _run(DEFAULT_COMMAND, cwd=repo_root, env=environment)
        finished_at = datetime.now(UTC).isoformat()
        unexpected_trust_store = home / ".bounded-loops" / "trust.json"
        if unexpected_trust_store.exists():
            raise RuntimeError(f"default suite created unexpected trust store: {unexpected_trust_store}")
    digest_after, _ = candidate_digest(repo_root)
    return {
        "sequence": sequence,
        "started_at": started_at,
        "finished_at": finished_at,
        "argv": list(DEFAULT_COMMAND),
        "environment": recorded_environment,
        "candidate_digest_before": digest_before,
        "candidate_digest_after": digest_after,
        "exit_code": result.returncode,
        "stdout_sha256": _sha256_bytes(result.stdout),
        "stderr_sha256": _sha256_bytes(result.stderr),
        "stdout_tail": result.stdout.decode("utf-8", errors="replace")[-1000:],
        "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-1000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    digest_before, manifest = candidate_digest(REPO_ROOT)
    attestation = {
        "schema_version": 1,
        "phase": "F0-integrated",
        "created_at": datetime.now(UTC).isoformat(),
        "subject_digest": digest_before,
        "subject_manifest": manifest,
        "platform": platform.platform(),
        "python": sys.version,
        "uv": _version(("uv", "--version"), REPO_ROOT),
        "git": _version(("git", "--version"), REPO_ROOT),
        "runs": [_attest_run(REPO_ROOT, 1), _attest_run(REPO_ROOT, 2)],
    }
    if any(run["exit_code"] != 0 for run in attestation["runs"]):
        raise SystemExit("default-suite attestation failed")
    if any(run["candidate_digest_before"] != digest_before or run["candidate_digest_after"] != digest_before for run in attestation["runs"]):
        raise SystemExit("candidate changed during attestation")

    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(attestation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
