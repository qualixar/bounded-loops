"""Concrete node-worker and gate adapters for the graph engine.

These four modules were filed under ``graph/application/`` until P3, which is why the
application layer appeared to need direct adapter imports: a concrete
``NodeWorkerPort`` implementation that launches OS sandboxes, forwards HTTP, or shells
out to a CLI *is* an adapter, whatever directory it sits in. Filing them here makes the
layering tripwire (``tests/graph/test_layering.py``) enforceable instead of aspirational.
"""
