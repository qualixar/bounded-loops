#!/usr/bin/env node
'use strict';

// Thin launcher for bounded-loops.
//
// bounded-loops is a Python package (the engine is Python). This npm wrapper exists so
// `npx bounded-loops <args>` works: it locates an engine at EXACTLY this npm release's version and
// hands off to it. It does NOT reimplement the tool in Node — Python 3.11+ must be on your PATH.
//
// WHY THIS FILE IS SHAPED THIS WAY. Until 0.6.10 the only install path was
// `python -m pip install bounded-loops==<v>` into whichever interpreter was found first. On any
// managed interpreter — Homebrew Python, Debian's python3, a distro build — PEP 668 refuses that
// outright with `externally-managed-environment`, so `npx bounded-loops` failed on a stock macOS
// machine with Homebrew Python and told the user to run the command that had just been refused.
// It also never looked for an engine ALREADY installed by pipx or `uv tool`, so a user with a
// perfectly good `bl` on PATH was sent down the install path anyway.
//
// So resolution now goes, in order: an interpreter that already has the matching version; a `bl` on
// PATH at the matching version; this launcher's own managed venv; and only then an install — into
// that venv, which PEP 668 does not govern, rather than into the user's Python.
//
// `--break-system-packages` is deliberately NOT used anywhere here. It exists to override exactly
// the protection PEP 668 provides, and a launcher that quietly mutates a distro-managed interpreter
// to save one step is not a tradeoff this package gets to make on a user's behalf.

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { version: npmVersion } = require('../package.json');

const args = process.argv.slice(2);
const isWindows = process.platform === 'win32';

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

// The version an interpreter's installed engine reports, or null. Merely importing bounded_loops is
// insufficient: an older global install would make `npx bounded-loops@0.6.10` silently execute
// 0.6.9, so every hand-off below is gated on an EXACT match.
function engineVersion(python) {
  const probe = spawnSync(
    python,
    ['-c', 'from importlib.metadata import version; print(version("bounded-loops"))'],
    { encoding: 'utf8' }
  );
  return probe.status === 0 ? probe.stdout.trim() : null;
}

// The version a `bl` on PATH reports. `bl --version` prints "bl 0.6.10"; take the last field. This
// is how a pipx or `uv tool` install is found — those live in their own venvs, so the interpreter
// probe above cannot see them, and before 0.6.10 nothing looked.
function pathCliVersion() {
  const probe = spawnSync('bl', ['--version'], {
    encoding: 'utf8',
    shell: isWindows, // a Windows console script is bl.cmd, which spawnSync will not resolve bare
  });
  if (probe.status !== 0 || !probe.stdout) return null;
  const fields = probe.stdout.trim().split(/\s+/);
  return fields[fields.length - 1] || null;
}

function venvRoot() {
  const base =
    process.env.BOUNDED_LOOPS_NPM_HOME ||
    process.env.XDG_CACHE_HOME ||
    path.join(os.homedir(), '.cache');
  // Keyed by version: `npx bounded-loops@0.6.9` and `@0.6.10` must not fight over one venv.
  return path.join(base, 'bounded-loops', `engine-${npmVersion}`);
}

function venvPython(root) {
  return isWindows
    ? path.join(root, 'Scripts', 'python.exe')
    : path.join(root, 'bin', 'python');
}

function runPython(python) {
  const run = spawnSync(python, ['-m', 'bounded_loops.cli', ...args], { stdio: 'inherit' });
  process.exit(run.status === null ? 1 : run.status);
}

function runPathCli() {
  const run = spawnSync('bl', args, { stdio: 'inherit', shell: isWindows });
  process.exit(run.status === null ? 1 : run.status);
}

function giveUp(reason) {
  console.error(`bounded-loops: ${reason}`);
  console.error('Install the engine yourself — any one of these works:');
  console.error(`  pipx install bounded-loops==${npmVersion}     # isolated, recommended`);
  console.error(`  uv tool install bounded-loops==${npmVersion}`);
  console.error(`  python3 -m pip install --user bounded-loops==${npmVersion}`);
  console.error('Then re-run this command.');
  process.exit(1);
}

const python = findPython();
if (!python) {
  console.error('bounded-loops requires Python 3.11+ on your PATH.');
  console.error('  → Install Python 3.11+, then re-run.');
  console.error(`  → Native install (recommended): pipx install bounded-loops==${npmVersion}`);
  process.exit(1);
}

// 1. The interpreter we found already has the matching engine.
if (engineVersion(python) === npmVersion) runPython(python);

// 2. A matching `bl` is already on PATH — a pipx or `uv tool` install. Nothing to do but use it.
if (pathCliVersion() === npmVersion) runPathCli();

// 3. This launcher's own venv from a previous run.
const root = venvRoot();
const managed = venvPython(root);
if (fs.existsSync(managed) && engineVersion(managed) === npmVersion) runPython(managed);

// 4. Create the venv and install into it. PEP 668 governs the interpreter's own site-packages, not
//    a venv, so this succeeds on exactly the managed Pythons where the old path was refused.
console.error(`bounded-loops: preparing engine ${npmVersion} in ${root}…`);
fs.mkdirSync(path.dirname(root), { recursive: true });
const created = spawnSync(python, ['-m', 'venv', root], { stdio: 'inherit' });
if (created.status !== 0) {
  // Debian and Ubuntu ship venv separately, so this is a real and common failure, not a corner case.
  giveUp(
    'could not create a private virtual environment. On Debian or Ubuntu install python3-venv first.'
  );
}
const installed = spawnSync(
  managed,
  ['-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', `bounded-loops==${npmVersion}`],
  { stdio: 'inherit' }
);
if (installed.status !== 0) {
  giveUp(`could not install bounded-loops==${npmVersion} into ${root}.`);
}
if (engineVersion(managed) !== npmVersion) {
  giveUp(`installed into ${root} but it does not report ${npmVersion}.`);
}
runPython(managed);
