# bounded-loops

[![PyPI version](https://img.shields.io/pypi/v/bounded-loops)](https://pypi.org/project/bounded-loops/)
[![npm version](https://img.shields.io/npm/v/bounded-loops)](https://www.npmjs.com/package/bounded-loops)
[![Python versions](https://img.shields.io/pypi/pyversions/bounded-loops)](https://pypi.org/project/bounded-loops/)
[![CI](https://github.com/qualixar/bounded-loops/actions/workflows/ci.yml/badge.svg)](https://github.com/qualixar/bounded-loops/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-2563eb)](https://github.com/qualixar/bounded-loops/blob/main/LICENSE)

**An AI coding agent can tell you it finished when it did not. This runs the agent in a
loop that only stops when a separate check — one the agent cannot edit or grade — says the
work actually passes, and gives up after a limit you set in advance.**

Every run leaves a hash-chained ledger you can re-verify, so "it passed" is a claim you can
check rather than take. 69 task templates ship inside the package; 65 need no API key.

Full documentation: [**github.com/qualixar/bounded-loops**](https://github.com/qualixar/bounded-loops)

---

This npm package is a **thin launcher** for the Python engine.

The engine itself is a **Python 3.11+** package. This wrapper lets you run it with
one command:

```bash
npx bounded-loops new --list
npx bounded-loops new pytest-basic my-loop
npx bounded-loops run my-loop --yes
```

On first run it detects Python 3.11+, installs the `bounded-loops` Python package
if it isn't already present, and hands off to the real CLI. It does **not**
reimplement the tool in Node — **Python 3.11+ must be on your PATH**.

Prefer the native install if you already have Python:

```bash
pip install bounded-loops
bl new --list
bl new pytest-basic my-loop
bl run my-loop --yes
```

Full documentation, the 69 loop folders (65 keyless), the nine bounds, and the architecture
docs live in the [main repository](https://github.com/qualixar/bounded-loops).
Clone the repository when you want `bl list` to browse the full shipped loop
catalog.

Apache-2.0.
