"""`bl graph console` — minimal localhost click-to-approve UI for one run (Slice 3).

Package layout:
* ``server.py``             — the loopback-only HTTP server + request handler
                               (bind discipline, token check, CSRF check, method
                               allowlist, fixed routing). No HTML lives here.
* ``rendering.py``           — HTML rendering only; the one place untrusted or
                               echoed values are interpolated into markup.
* ``console_template.html``  — static page shell (CSS + a single body marker).
* ``cli_console.py``         — ``cmd_graph_console``, the ``bl graph console``
                               handler wired from ``cli_graph.py``.

Every state-changing action goes through ``LocalGraphRuntimeFacade.approve()``
(Slice 1's durable machinery) unchanged. This package adds no new approval
authority, no new persistence, and no new decision logic of its own — it is a
browser-facing twin of ``bl graph approve``, nothing more.
"""

from __future__ import annotations
