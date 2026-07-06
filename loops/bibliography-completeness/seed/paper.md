# Toward Reliable Local-First Agent Memory

Local-first software preserves user ownership of data even when a network
connection is unavailable [@smith2020].

Recent work on retrieval quality in long-running agent sessions has shown
that stale or duplicated memory entries degrade downstream task accuracy
over time [@jones2021].

We build on these findings to argue that a governed, versioned memory store
is necessary for production agent deployments [@lee2019].

## References

- smith2020: Smith, J. "Local-First Software: You Own Your Data." 2020.
- lee2019: Lee, K. "Versioned State Stores for Long-Running Agents." 2019.
