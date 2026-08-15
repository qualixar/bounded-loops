<!-- AUTO-GENERATED from bounded_loops/graph/application/refusals.py -->
<!-- Do not hand-edit: update refusals.py, then re-run scripts/gen_refusal_reference.py -->

# Bounded-Loops Graph Validator — Refusal Reference

Every error the graph validator can raise, with the plain-language cause and the fix.
Generated from `bounded_loops.graph.application.refusals.REFUSALS` (37 codes).

Use this table when `bl graph lint`, `bl graph plan`, or `graph_lint` rejects your manifest.
The error message contains the code and the exact pointer (`/nodes/my-node/on_failure`);
look up the code here to know what to change.

| Code | What it means | How to fix it |
|---|---|---|
| `absolute_path` | The manifest contains an absolute local path. | Use a workspace-relative path. An absolute path makes the graph run on exactly one machine. |
| `api_version` | The manifest does not declare the api_version this engine compiles. | Set api_version to the value the error names. |
| `audit_profile` | The audit profile is not a string or null. | Name a profile, or set it to null. |
| `cycle` | The graph is not acyclic. | Remove the cycle. Retry is bounded per node and repair is a bounded backward edge; a cycle in the authoring graph is unbounded work. |
| `duplicate_effect` | A node declares the same effect twice. | Remove the duplicate. |
| `duplicate_key` | The same key appears twice in one mapping. | Delete one of the two. A silent last-wins merge would hide which one applied. |
| `duplicate_node_id` | Two nodes share an id. | Rename one. Node ids address receipts, so a collision makes the log ambiguous. |
| `duplicate_slot_id` | Two connection slots share an id. | Rename one of them. Nodes select a connection by slot id, so a collision makes it undecidable which connection a node actually got. |
| `duplicate_value` | A list that must hold unique declared values repeats one. | Remove the duplicate. |
| `edge_condition` | An edge guard can never be reached. | Under `fail_mode: fail_closed` the run stops at the first failure, so a `when: FAILED` edge is dead. Either drop the guard or change the fail mode. |
| `enum` | A field's value is not one this engine supports. | Use one of the supported values. `bl_capabilities` lists them, including which are declared but not yet honoured. |
| `fail_mode` | The graph's fail_mode is not a declared mode. | Use a supported fail mode (`fail_closed` or `continue_declared`). |
| `identifier` | An id is not a stable, portable identifier. | Use a plain name — letters, digits, dash, underscore — with no paths, spaces, or host-specific characters. |
| `impossible_join` | A join has no incoming edge. | Give the join the edges it joins, or delete it. |
| `incomplete_branches` | A router does not cover every outcome. | Declare routes for each branch, and either a `default` route or an explicit `default_route`. An uncovered branch is a run that stops with nowhere to go. |
| `invalid_json` | The manifest is not parseable JSON. | Fix the JSON syntax at the reported position. |
| `invalid_yaml` | The manifest is not parseable YAML. | Fix the YAML syntax at the reported position. |
| `join_mode` | The join's mode is not supported. | Use `all_selected`, `all_successful`, or `any_successful`. |
| `missing_field` | A required field is absent. | Add the field(s) the message names. |
| `missing_input_port` | An edge writes to an input port the target node does not declare. | Declare the input on the target node, or point the edge at one it has. |
| `missing_output_port` | An edge reads an output port the source node does not declare. | Declare the output on the source node, or point the edge at one it has. |
| `mutable_package_reference` | A loop node's `loop_package` is not a sha256 digest. | Pin the package by content digest. A mutable reference means the thing that ran is not the thing the receipt names. |
| `on_failure` | The node's failure policy is malformed or unreachable. | Write a bare string for the simple policies; only `repair` uses the object form `{mode: repair, target: <ancestor_node_id>}`. Under `fail_closed`, repair is unreachable — the run stops at the first failure. |
| `on_failure_unimplemented` | The policy is declared by the schema but the runtime does not route it. | Use `fail_graph` (the default) or `repair`. `continue` and `await_human` are refused rather than accepted-and-ignored, because accepting them would hand back a plan whose declared failure policy is silently discarded. |
| `port_type_mismatch` | An edge connects two ports whose declared types differ. | Make the types agree, or insert a node that converts. |
| `provider_in_slot` | A connection slot names a specific provider. | Declare the capability you need (modality, context, tool use) and let the resolver pick. Naming a provider pins the graph to one vendor. |
| `range` | A numeric field is outside its permitted range. | Choose a value inside the stated bounds (shown in the error message). |
| `repair_budget` | Repair is declared without a bound, or the bound is out of range. | Set `policies.repair_budget` above 0. It is the GLOBAL bound on repair rounds and is what makes the loop terminate. |
| `repair_target` | A repair edge names no node, or names one that is not an ancestor. | Point `target` at an ancestor of the failing node — repair re-executes work that already ran upstream. |
| `retry_of_effectful_node` | A node carrying an external or irreversible effect may retry with no idempotency key. | Give the effect a per-effect idempotency key, or set the node's `max_attempts` to 1. Retrying an irreversible effect is a double-spend. |
| `secret_field` | The manifest contains a secret-shaped field. | Delete it. Credentials never live in a graph: declare a connection slot and let the no-secret broker resolve it at run time. |
| `type` | A field has the wrong shape (object, array, or non-empty string expected). | Give the field the shape the message names. |
| `unknown_connection_slot` | A node names a connection slot the graph does not declare. | Declare the slot, or point the node at one that exists. |
| `unknown_edge_node` | An edge references a node that does not exist. | Fix the node id, or add the node. |
| `unknown_field` | A field the schema does not define is present. | Remove it. Unknown fields are refused rather than ignored, because an ignored field is a policy the author believes is in force and is not. |
| `unknown_node_kind` | The node's kind is not one this engine runs. | Use a supported kind — `bl_capabilities` lists all of them with their kind-specific required fields. |
| `version` | The graph's version is not a pinned semantic version. | Write an exact version like `1.0.0`. Ranges are refused: a graph that can mean two things is not reproducible. |

**Total: 37 distinct codes** (authority: `bounded_loops.graph.application.refusals.REFUSAL_CODES`, verified by `tests/graph/application/test_refusals.py`)
