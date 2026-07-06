# bounded-loops (npm launcher)

This npm package is a **thin launcher** for [**bounded-loops**](https://github.com/qualixar/bounded-loops)
— runnable, bounded AI-agent loops where every safety bound is enforced in engine
code, not described in a checklist.

The engine itself is a **Python 3.11+** package. This wrapper lets you run it with
one command:

```bash
npx bounded-loops list
npx bounded-loops run loops/bug-fix-red-green --yes
```

On first run it detects Python 3.11+, installs the `bounded-loops` Python package
if it isn't already present, and hands off to the real CLI. It does **not**
reimplement the tool in Node — **Python 3.11+ must be on your PATH**.

Prefer the native install if you already have Python:

```bash
pip install bounded-loops
bl list
```

Full documentation, the 63 runnable loops, the nine bounds, and the architecture
docs live in the [main repository](https://github.com/qualixar/bounded-loops).

Apache-2.0.
