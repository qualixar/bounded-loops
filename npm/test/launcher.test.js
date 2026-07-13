'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const launcher = path.resolve(__dirname, '..', 'bin', 'bounded-loops.js');

test('npx launcher pins the Python engine to the npm package version', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'bounded-loops-npm-test-'));
  const log = path.join(temp, 'python.log');
  const fakePython = path.join(temp, 'python3');
  fs.writeFileSync(
    fakePython,
    `#!/bin/sh\n` +
      `printf '%s\\n' "$*" >> "$BOUNDED_LOOPS_TEST_LOG"\n` +
      `case "$*" in\n` +
      `  *"import sys"*) exit 0 ;;\n` +
      `  *"importlib.metadata"*) printf '0.2.1\\n'; exit 0 ;;\n` +
      `  *"import bounded_loops"*) exit 0 ;;\n` +
      `  *"pip install"*) exit 0 ;;\n` +
      `  *"bounded_loops.cli"*) exit 0 ;;\n` +
      `esac\n` +
      `exit 1\n`,
    { mode: 0o755 }
  );

  const result = spawnSync(process.execPath, [launcher, 'doctor'], {
    env: {
      ...process.env,
      PATH: temp,
      BOUNDED_LOOPS_TEST_LOG: log,
    },
    encoding: 'utf8',
  });

  assert.equal(result.status, 0, result.stderr);
  const calls = fs.readFileSync(log, 'utf8');
  assert.match(calls, /pip install --quiet bounded-loops==0\.3\.0/);
  assert.match(calls, /-m bounded_loops\.cli doctor/);
});
