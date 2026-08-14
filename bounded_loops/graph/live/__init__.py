"""Live run visibility — SSE event stream over controller-events.jsonl.

Sub-modules:
  posture    — shared security primitives (loopback bind, token, CSRF check, headers)
  sse_server — WatchServer: tails the event log, pushes ArenaProjection snapshots via SSE
  cli_watch  — ``bl graph watch`` CLI entry point
"""
