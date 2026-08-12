"""`bl graph init` — interactive installer for egress posture + connector mode (Slice 4).

Package layout:
* ``errors.py``         — ``GraphInitError``, the one expected-failure type this
                           package raises (never a raw ``OSError``/traceback to the CLI).
* ``config_writer.py``  — path resolution, allowlist-entry validation, the secure
                           (0600/0700, ``O_NOFOLLOW``) config write, and the
                           post-write round-trip read-back through
                           ``egress_posture.resolve_egress_posture`` — the SAME
                           fail-closed reader `bl graph run` consumes.
* ``connector.py``      — ``ConnectorMode`` (subscription CLI vs BYOK) + its prompt
                           and BYOK guidance text. Informational only in this slice:
                           no connector config file is written (see module docstring
                           there for why).
* ``prompts.py``        — interactive egress-posture / allowlist / confirmation
                           prompts, each accepting an injectable ``input_fn`` so
                           tests never touch real stdin.
* ``cli_init.py``       — ``cmd_graph_init``, the ``bl graph init`` handler wired
                           from ``cli_graph.py``.

Non-negotiables this package upholds everywhere:
* The DEFAULT is OPEN egress + subscription-CLI connector — the zero-friction path
  — whenever a prompt is accepted blank or a flag is omitted.
* No secret or credential value is ever read, printed, or written by this package.
* Every value this package writes to ``egress.json`` is proven, in-process, to
  round-trip cleanly through ``egress_posture.resolve_egress_posture`` — the exact
  fail-closed reader `bl graph run` / `LocalGraphRuntimeFacade` already use. This
  package never modifies that reader; it is a pure, obedient producer of its
  documented config contract.
* An existing config file (or a symlink at the config path) is never silently
  clobbered or followed.
"""

from __future__ import annotations
