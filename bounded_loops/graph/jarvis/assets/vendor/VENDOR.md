# Vendored front-end libraries

Jarvis is a React app with **no bundler and no build step**. These three files are the
entire front-end dependency set, committed here and served from the wheel, so a
`pip install` user runs the UI with no node, no npm, and no network.

Each is the unmodified published artifact, pinned by version and content digest —
the same discipline the engine applies to a loop package, applied to its own UI.

| File | Package | Version | Path in tarball | Digest |
|---|---|---|---|---|
| `react.production.min.js` | react | 18.3.1 | `umd/react.production.min.js` | `sha256:d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd` |
| `react-dom.production.min.js` | react-dom | 18.3.1 | `umd/react-dom.production.min.js` | `sha256:35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d` |
| `htm.module.js` | htm | 3.1.1 | `dist/htm.module.js` | `sha256:ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4` |

## Reproducing this directory

```bash
npm pack react@18.3.1 react-dom@18.3.1 htm@3.1.1
# then extract the three paths in the table above
```

`tests/graph/jarvis/test_vendor_pins.py` recomputes every digest above and fails if a file
changes, so a silent swap of the React we ship is not possible.
