# Introduction to Bounded Loops

A bounded loop drives an agent turn after turn until a real gate — a command,
a schema check, a linter — reports success. Every lap is capped: iteration
count, wallclock time, and token budget all have hard limits so a loop can
never run away.

See [Getting Started](a.md) for the entry point into this series.
