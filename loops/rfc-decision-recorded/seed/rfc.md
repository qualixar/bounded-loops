# RFC-014: Move newsletter build to a static site generator

## Status

Accepted

## Context

The newsletter is currently hand-assembled via a shell script that
concatenates HTML fragments. Each issue takes 20+ minutes to build and the
marker-based templating (`build-html.sh`) is fragile — a renamed section
heading silently breaks the build with no error, which has caused at least
two late-night re-sends.

We evaluated three options: keep the shell script and add tests, move to a
static site generator (Eleventy/Astro), or move the whole newsletter into
the existing Next.js portal codebase.
