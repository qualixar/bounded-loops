#!/usr/bin/env python3
"""Materialise a parameterised workload loop for E9 (bound utilisation).

WHY THIS EXISTS
---------------
The shipped catalogue cannot measure bound utilisation. Every loop plants a defect of median
size 2 lines and every cassette repairs it in one action, so 59 of 61 loops reach DONE on lap 1
against a declared ``max_iterations: 10``. Utilisation is 1/10 by construction, ``max_iterations``
is never approached, and ``no_progress_window`` never fires. Those loops are correct and they do
their job -- proving a gate catches a planted defect -- but they cannot exercise a budget.

So this module builds a loop whose workload is a PARAMETER. The seed holds ``n_records`` records,
each missing a required field; the gate counts how many are still missing. A worker that repairs
``k`` records per attempt therefore converges in ``ceil(n_records / k)`` attempts -- a convergence
length DERIVED from the declared workload, not authored into a fixture.

The independent variable is the workload and the worker's repair rate. The dependent variable is
attempts consumed against the declared ceiling. Nothing here measures agent capability, and no
claim about agent capability may be drawn from it.

The catalogue under ``loops/`` is NOT touched: it is the alpha corpus for E6/E7 and its planted
defects are frozen. These loops live outside it and are excluded from ``loops/*/loop.yaml``.

Usage:
    python3 generate.py --dest /tmp/w --records 8 --policy one_per_lap
    python3 generate.py --dest /tmp/w --records 8 --policy stall_after --stall-after 1
"""

from __future__ import annotations

import argparse
import json
import pathlib
import textwrap

#: Repair policies. `complete` reproduces the shipped catalogue's behaviour (converge on attempt
#: one); the others exist to drive the loop into regions of its budget the catalogue never reaches.
POLICIES = ("complete", "fraction", "one_per_lap", "stalled", "stall_after")

_GATE = '''\
#!/usr/bin/env python3
"""Independent acceptance gate: every record must carry a non-empty checksum.

Reports the outstanding VIOLATION COUNT on stdout, not merely pass/fail. A count is what lets an
experiment check predicted convergence against observed convergence, and what lets an operator see
progress across attempts instead of a bare red/green.

Refuses to pass on an empty or malformed record list. A gate that returns success because it found
nothing to check is vacuous -- it would be satisfied by the absence of the thing it checks -- and
this project measures that defect class elsewhere; it must not ship one in its own instrument.
"""

import json
import pathlib
import sys

#: Minimum repair round at which this gate is willing to pass, regardless of content.
#: Zero means "content alone decides", which is the ordinary case.
REQUIRE_ROUND = __REQUIRE_ROUND__


def _repair_round() -> int:
    """The graph controller's current repair round, or 0 when running standalone.

    Published by `loop_node_entry` into the loop's own workspace. Absent under plain `bl run`,
    which is not an error -- a loop outside a graph is always in round 0.
    """
    context = pathlib.Path(".bounded-loops-node.json")
    if not context.exists():
        return 0
    try:
        return int(json.loads(context.read_text(encoding="utf-8")).get("repair_round", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # Refuse to guess. An unreadable context is round 0, which is the strictest reading.
        return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("check_records: usage: check_records.py <records.json>")
        return 2
    if REQUIRE_ROUND:
        current = _repair_round()
        if current < REQUIRE_ROUND:
            print(
                f"check_records: refusing before repair round {REQUIRE_ROUND} "
                f"(currently {current})"
            )
            return 1
    path = pathlib.Path(sys.argv[1])
    if not path.exists():
        print(f"check_records: {path} does not exist -- refusing to pass")
        return 2
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"check_records: {path} is not valid JSON: {exc}")
        return 2
    if not isinstance(records, list) or not records:
        print("check_records: no records found -- refusing to pass on an empty list")
        return 2

    missing = [r for r in records if not (isinstance(r, dict) and r.get("checksum"))]
    print(
        f"check_records: {len(missing)} of {len(records)} records missing a checksum"
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_WORKER = '''\
#!/usr/bin/env python3
"""Synthetic worker of declared, bounded competence.

Repairs a fixed quota of outstanding records per invocation. The quota -- not the workload -- is
what varies between experiment conditions, so convergence length falls out of the arithmetic
rather than being written down anywhere.

Stateless by construction: the quota is derived from what is already repaired in the workspace,
never from a lap counter the worker would have to be told. That keeps the worker honest under
resume and replay.
"""

import json
import math
import pathlib

POLICY = {policy!r}
COMPETENCE = {competence!r}
STALL_AFTER = {stall_after!r}
RECORDS = pathlib.Path("seed/records.json")


def quota(outstanding: int, repaired: int) -> int:
    if POLICY == "stalled":
        return 0
    if POLICY == "stall_after":
        return 0 if repaired >= STALL_AFTER else 1
    if POLICY == "one_per_lap":
        return 1
    if POLICY == "fraction":
        return max(1, math.ceil(COMPETENCE * outstanding))
    return outstanding  # "complete"


def main() -> int:
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    missing = [r for r in records if not r.get("checksum")]
    repaired = len(records) - len(missing)
    allowed = min(quota(len(missing), repaired), len(missing))

    # Write only when something actually changes. Rewriting identical bytes would still dirty the
    # workspace, and the engine reads workspace dirtiness as evidence of progress.
    if allowed:
        for record in missing[:allowed]:
            record["checksum"] = f"sha-{{record['id']:06d}}"
        RECORDS.write_text(json.dumps(records, indent=2) + "\\n", encoding="utf-8")

    print(f"worker: repaired {{allowed}} record(s); {{len(missing) - allowed}} still outstanding")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def materialise(
    dest: pathlib.Path,
    *,
    n_records: int = 8,
    policy: str = "one_per_lap",
    competence: float = 1.0,
    stall_after: int = 1,
    max_iterations: int = 10,
    no_progress_window: int = 3,
    require_round: int = 0,
) -> pathlib.Path:
    """Write a complete, runnable loop directory and return its path.

    ``require_round`` makes the gate refuse until the graph controller has reached that repair
    round. It is how a repair round is given something to change: without it, a deterministic node
    re-runs identical work every round and repair is a provable no-op.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")
    if n_records < 1:
        raise ValueError("n_records must be >= 1")
    if not 0.0 < competence <= 1.0:
        raise ValueError("competence must be in (0, 1]")

    dest.mkdir(parents=True, exist_ok=True)
    (dest / "seed").mkdir(exist_ok=True)

    records = [{"id": i, "payload": f"row-{i}"} for i in range(1, n_records + 1)]
    (dest / "seed" / "records.json").write_text(
        json.dumps(records, indent=2) + "\n", encoding="utf-8"
    )
    # Token substitution, not str.format: the gate template contains f-strings whose braces
    # format() would try to interpret.
    (dest / "seed" / "check_records.py").write_text(
        _GATE.replace("__REQUIRE_ROUND__", str(require_round)), encoding="utf-8"
    )
    # The worker lives INSIDE seed/ because the scratch workspace is a copy of seed/ alone
    # (composition._make_scratch_workspace). A worker at the loop root is simply absent from the
    # sandbox, and ShellRunner does not raise on a non-zero agent exit, so the loop would burn its
    # whole budget against a command that never ran.
    (dest / "seed" / "worker.py").write_text(
        _WORKER.format(policy=policy, competence=competence, stall_after=stall_after),
        encoding="utf-8",
    )

    (dest / "loop.yaml").write_text(
        textwrap.dedent(f"""\
        name: e9-workload-{n_records}-{policy}
        description: >
          Parameterised workload loop for the E9 bound-utilisation experiment. The seed holds
          {n_records} records missing a required checksum field; the gate counts the outstanding
          ones. The worker repairs a declared quota per attempt, so attempts consumed is a
          function of the workload and the quota, not of a fixture.
        pattern: evaluator-optimizer
        role:
        - engineering
        rung: L2
        spec: PROMPT.md
        forbid:
        - seed/check_records.py
        runner:
          default: shell
          agent_cmd: python3 seed/worker.py
        gate:
          kind: command
          run: python3 seed/check_records.py seed/records.json
        bounds: bounds.yaml
        memory: STATE.md
        """),
        encoding="utf-8",
    )

    (dest / "bounds.yaml").write_text(
        textwrap.dedent(f"""\
        max_iterations: {max_iterations}
        no_progress_window: {no_progress_window}
        max_tokens: null
        max_wallclock_s: 120
        sandbox: true
        quarantine_inputs: true
        schema: null
        trace: true
        require_approval: false
        """),
        encoding="utf-8",
    )

    (dest / "PROMPT.md").write_text(
        textwrap.dedent(f"""\
        # Task

        `seed/records.json` holds {n_records} records. Every record must carry a non-empty
        `checksum` field. Add the missing ones.

        The acceptance gate is `seed/check_records.py`. It is not yours to edit.
        """),
        encoding="utf-8",
    )
    (dest / "STATE.md").write_text("# State\n\nNo attempts yet.\n", encoding="utf-8")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=pathlib.Path, required=True)
    parser.add_argument("--records", type=int, default=8)
    parser.add_argument("--policy", choices=POLICIES, default="one_per_lap")
    parser.add_argument("--competence", type=float, default=1.0)
    parser.add_argument("--stall-after", type=int, default=1)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--no-progress-window", type=int, default=3)
    parser.add_argument("--require-round", type=int, default=0,
                        help="gate refuses until the controller reaches this repair round")
    args = parser.parse_args()

    path = materialise(
        args.dest,
        n_records=args.records,
        policy=args.policy,
        competence=args.competence,
        stall_after=args.stall_after,
        max_iterations=args.max_iterations,
        no_progress_window=args.no_progress_window,
        require_round=args.require_round,
    )
    print(f"materialised {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
