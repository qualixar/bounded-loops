"""Runner admission preflight — an adapter because it starts real processes.

``runner_preflight`` lived under ``graph/application/`` until P3 while importing
``ProcessTurn`` and launching subprocesses. A module that spawns a process is an adapter;
leaving it in the application layer is what made the layering rule unenforceable.
"""
