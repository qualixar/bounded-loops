# bounded-loops (npm launcher)

This npm package is a **thin launcher** for [**bounded-loops**](https://github.com/qualixar/bounded-loops)
— runnable, bounded AI-agent loops where every safety bound is enforced in engine
code, not described in a checklist.

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

Full documentation, the 68 loop folders (64 keyless), the nine bounds, and the architecture
docs live in the [main repository](https://github.com/qualixar/bounded-loops).
Clone the repository when you want `bl list` to browse the full shipped loop
catalog.

Apache-2.0.
