"""Real connector adapters — the seams a node uses to actually reach its connector.

``http_forwarder`` is the BYOK path (a real HTTP call to a frontier/API provider over
the no-secret egress broker); the CLI/local path lands alongside it. Nothing here holds
a credential in the graph: the forwarder resolves it out-of-band and never logs it.
"""
