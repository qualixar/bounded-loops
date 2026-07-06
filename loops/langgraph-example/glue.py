"""
glue.py — wires an existing LangGraph graph into bounded-loops via
PythonCallableRunner. Requires `langgraph` installed
(`pip install langgraph`) — NOT a bounded-loops dependency, a
prerequisite for running THIS example loop specifically.
"""
from pathlib import Path


def run_turn(prompt: str, workspace: str) -> dict:
    from langgraph.graph import StateGraph, END   # lazy import — only this
                                                     # example loop needs it,
                                                     # never bounded_loops itself

    # A trivial single-node graph standing in for "your existing graph."
    # Real usage: replace this with your own compiled graph; the only
    # bounded-loops-specific parts are the `workspace` file I/O below.
    def edit_node(state: dict) -> dict:
        target = Path(workspace) / "seed" / "app.py"
        fixed = target.read_text().replace("return a - b", "return a + b")
        target.write_text(fixed)
        return {**state, "done": True}

    graph = StateGraph(dict)
    graph.add_node("edit", edit_node)
    graph.set_entry_point("edit")
    graph.add_edge("edit", END)
    compiled = graph.compile()

    result_state = compiled.invoke({"prompt": prompt, "done": False})

    return {
        "changed": True,   # HONEST LIMITATION (documented, not
                            # silently shipped): hard-coded True for demo
                            # simplicity. This means the engine's no-progress
                            # bound (#6, bounds.no_progress_window) can never
                            # fire for this example loop — a framework call
                            # that silently does nothing still reports
                            # changed=True. Production glue code should instead
                            # compare target-file content before/after the
                            # framework call and report the real result. See
                            # this loop's README for the same note.
        "agent_claimed_done": bool(result_state.get("done")),
        "tokens": 0,   # LangGraph doesn't report tokens for this trivial
                       # graph; a real LLM-backed node would populate this
                       # from its own usage metadata.
        "log": f"LangGraph run_turn: done={result_state.get('done')}",
    }
