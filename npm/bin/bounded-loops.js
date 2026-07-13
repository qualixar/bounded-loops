#!/usr/bin/env node
'use strict';

// Thin launcher for bounded-loops.
//
// bounded-loops is a Python package (the engine is Python). This npm wrapper
// exists so `npx bounded-loops <args>` works: it finds Python 3.11+, installs
// the engine on first run if it isn't already present, then hands off to the
// real CLI (`python -m bounded_loops.cli`). It does NOT reimplement the tool in
// Node — Python 3.11+ must be available on your PATH.

const { spawnSync } = require('child_process');
const { version: npmVersion } = require('../package.json');

const args = process.argv.slice(2);

function findPython() {
  for (const candidate of ['python3', 'python']) {
    const probe = spawnSync(
      candidate,
      ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'],
      { stdio: 'ignore' }
    );
    if (probe.status === 0) return candidate;
  }
  return null;
}

function runCli(python) {
  const run = spawnSync(python, ['-m', 'bounded_loops.cli', ...args], {
    stdio: 'inherit',
  });
  process.exit(run.status === null ? 1 : run.status);
}

const python = findPython();
if (!python) {
  console.error('bounded-loops requires Python 3.11+ on your PATH.');
  console.error('  → Install Python 3.11+, then re-run.');
  console.error('  → Native install (recommended): pip install bounded-loops');
  process.exit(1);
}

// Run only when the Python engine exactly matches this npm release. Merely
// importing bounded_loops is insufficient: an older global install would make
// `npx bounded-loops@0.3.0` silently execute 0.2.x.
const installed = spawnSync(
  python,
  [
    '-c',
    'from importlib.metadata import version; print(version("bounded-loops"))',
  ],
  { encoding: 'utf8' }
);
if (installed.status === 0 && installed.stdout.trim() === npmVersion) {
  runCli(python);
}

// First run or version mismatch: install the exact matching Python engine.
console.error(`bounded-loops: installing Python engine ${npmVersion}…`);
const install = spawnSync(
  python,
  ['-m', 'pip', 'install', '--quiet', `bounded-loops==${npmVersion}`],
  { stdio: 'inherit' }
);
if (install.status !== 0) {
  console.error(`Auto-install failed. Install it manually: pip install bounded-loops==${npmVersion}`);
  process.exit(1);
}
runCli(python);
