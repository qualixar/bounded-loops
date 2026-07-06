"""
OsvGate — wraps Google's osv-scanner (github.com/google/osv-scanner) as a
GatePort. Scans the workspace recursively for known-vulnerable dependencies
in whatever manifest/lockfile files osv-scanner recognizes there.

Prerequisite: osv-scanner must be installed and on PATH — NOT bundled.

Exit-code classification:
  0                    -> Verdict(passed=True)   no vulnerabilities
  1                    -> Verdict(passed=False)  vulnerabilities found (NORMAL fail)
  128                  -> Verdict(passed=True)   "no packages found" — BENIGN,
                          not an error (the common case for a fresh/non-
                          dependency-bearing workspace)
  127 or anything else -> GateError              scanner itself could not run

Security posture: subprocess
env is allowlisted to {PATH, HOME, LANG, LC_ALL, TMPDIR, SHELL} + ctx.env
opt-ins — never the full parent environment. This gate takes NO
configuration from gate_config beyond timeout_s, argv is a fixed list
(shell=False), so there is no shell-injection surface.

fix — untrusted-JSON robustness: osv-scanner's stdout on exit 1 is
attacker-influenced (bounded-loops explicitly invites community loop PRs;
the workspace it scans is not fully trusted). _summarize is defensive at
every nesting level (isinstance guards), and check()'s exit-1 branch
catches (JSONDecodeError, AttributeError, TypeError, KeyError) — not just
JSONDecodeError — so a valid-JSON-but-wrong-shape payload degrades to a
raw-tail summary instead of raising an uncaught exception that would
escape the Verdict-or-GateError contract every gate in this project must
honor (domain/errors.py).
"""
from __future__ import annotations

import json
import subprocess

from bounded_loops.adapters._env import ENV_ALLOWLIST, build_subprocess_env
from bounded_loops.domain.errors import GateError
from bounded_loops.domain.models import LoopContext, Verdict

# Single source in adapters/_env.py. Names kept for local references/tests.
_ENV_ALLOWLIST = ENV_ALLOWLIST


def _build_subprocess_env(ctx_env: dict[str, str]) -> dict[str, str]:
    return build_subprocess_env(ctx_env)


def _summarize(payload: object) -> str:
    """Extract a short, human-readable summary from osv-scanner's JSON
    shape — never dump raw JSON as the Verdict detail. fix:
    isinstance-guarded at every nesting level — a valid-JSON-but-wrong-shape
    payload (adversarial or a schema-version drift) must degrade to "no
    findings extracted", never raise."""
    findings: list[str] = []
    if not isinstance(payload, dict):
        return "unrecognized output shape"
    for result in payload.get("results", []) or []:
        if not isinstance(result, dict):
            continue
        for pkg in result.get("packages", []) or []:
            if not isinstance(pkg, dict):
                continue
            pkg_info = pkg.get("package")
            name = pkg_info.get("name", "?") if isinstance(pkg_info, dict) else "?"
            version = pkg_info.get("version", "?") if isinstance(pkg_info, dict) else "?"
            for vuln in pkg.get("vulnerabilities", []) or []:
                if not isinstance(vuln, dict):
                    continue
                vid = vuln.get("id", "UNKNOWN")
                findings.append(f"{name}@{version}: {vid}")
    if not findings:
        return "NO_FINDINGS_EXTRACTED"   # sentinel — see check()'s exit-1
                                          # branch, which treats this as
                                          # "summary parsing found nothing
                                          # useful", NOT "scanner found no
                                          # vulnerabilities" (those are
                                          # different claims; exit 1 already
                                          # means vulnerabilities exist).
    shown = findings[:10]
    more = f" (+{len(findings) - 10} more)" if len(findings) > 10 else ""
    return "; ".join(shown) + more


class OsvGate:
    """Runs `osv-scanner scan --format json --recursive <workspace>`."""

    timeout_s: int

    def __init__(self, timeout_s: int = 120) -> None:
        self.timeout_s = timeout_s

    def check(self, ctx: LoopContext) -> Verdict:
        try:
            proc = subprocess.run(
                ["osv-scanner", "scan", "--format", "json", "--recursive", str(ctx.workspace)],
                cwd=str(ctx.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=_build_subprocess_env(ctx.env),
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"OsvGate: osv-scanner timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GateError(
                f"OsvGate: could not launch osv-scanner (is it installed and on PATH?): {exc}"
            ) from exc

        code = proc.returncode

        if code == 0:
            # A genuine clean scan STILL emits osv-scanner's real JSON report
            # (verified live 2026-07-06: exit 0 with --format json produces
            # {"results": [...], ...}). Empty or unparseable stdout on exit 0
            # means the scanner did not actually run — shadowed on PATH,
            # wrapped in `--soft-fail`, crashed-after-fork-but-still-exited-0,
            # or replaced by a no-op — and must NEVER be reported as PASS. This
            # mirrors CheckovGate's empty-stdout-first fail-closed guard; a
            # vulnerability gate that green-lights a silently-broken scanner
            # defeats the entire "a gate cannot be tricked into passing" premise.
            try:
                payload = json.loads(proc.stdout or "")
            except (json.JSONDecodeError, TypeError):
                payload = None
            if not isinstance(payload, dict) or "results" not in payload:
                raise GateError(
                    "OsvGate: osv-scanner exited 0 but produced no recognizable "
                    "JSON report (expected a top-level 'results' key). The scanner "
                    "almost certainly did not actually run — refusing to report a "
                    "PASS on unverifiable output. "
                    f"stdout_head={(proc.stdout or '')[:200]!r} "
                    f"stderr={(proc.stderr or '')[-300:]!r}"
                )
            return Verdict(
                passed=True,
                detail="osv-scanner: no known vulnerabilities",
                evidence={"cmd": "osv-scanner scan", "code": 0},
            )

        if code == 128:
            # Hardening: "no packages found" (exit 128) must
            # NOT be a PASS. For an osv-GATED loop, the whole point is to verify
            # a dependency manifest — "nothing to scan" means the manifest is
            # missing or was gutted (a cassette/agent overwriting package-lock.json
            # with `{}` triggers exactly this), which is the "gate green-lights
            # output it could not evaluate" false-pass. The legitimate clean
            # outcome is exit 0 (manifest present, zero known vulns), never 128.
            # Fail CLOSED: the gate could not verify anything.
            raise GateError(
                "OsvGate: osv-scanner found NO packages to scan (exit 128). For a "
                "dependency-vulnerability gate this means the manifest/lockfile is "
                "missing or empty — the gate could not verify anything, which is NOT "
                "a clean pass. If a lockfile is the loop's mutable anchor, a runner "
                "may have gutted it. "
                f"stderr={(proc.stderr or '')[-300:]!r}"
            )

        if code == 1:
            try:
                payload = json.loads(proc.stdout or "{}")
                summary = _summarize(payload)
            except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
                summary = "PARSE_FAILED"
            if summary in ("NO_FINDINGS_EXTRACTED", "PARSE_FAILED", "unrecognized output shape"):
                # fix: exit 1 already means vulnerabilities exist —
                # never say "no known vulnerabilities" here (self-
                # contradicting). Fall back to the raw tail so a human can
                # still see something real, rather than a misleading claim.
                tail = (proc.stdout or "")[-500:] or "vulnerabilities found (unparsed/unrecognized output)"
                summary = tail
            return Verdict(
                passed=False,
                detail=f"osv-scanner: vulnerabilities found — {summary}",
                evidence={"cmd": "osv-scanner scan", "code": 1, "stdout_tail": (proc.stdout or "")[-2000:]},
            )

        raise GateError(
            f"OsvGate: osv-scanner exited with unexpected code {code} "
            f"(documented codes are 0=clean, 1=vulnerabilities-found, "
            f"128=no-packages-found; 127 and anything else mean the scanner "
            f"itself could not run cleanly). "
            f"stderr={(proc.stderr or '')[-500:]!r}"
        )
