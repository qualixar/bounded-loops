"""OpenAI-compatible chat-completions request document builder (BYOK request_style="openai_chat").

A pure function that assembles the request body for the OpenAI Chat Completions API
(also compatible with any provider that implements the same wire format, e.g. Azure OpenAI,
together AI, Groq, etc.) and wraps it in a ``ConnectorRequestDocument``.

The document carries ONLY:
* path   — the endpoint path (e.g. "/v1/chat/completions")
* headers — {"content-type": "application/json"} — NO credential header (the forwarder
            injects the credential from ``EnvCredentialResolver`` at call time, keeping
            secrets out of stored artifacts)
* body   — canonical JSON: {"messages":[{"content":<prompt>,"role":"user"}],"model":<model>}
* scheme — "https"

More request styles (e.g. "anthropic_messages", "openai_responses") can be added in
parallel modules and registered in ``admitted_connection_request._KNOWN_STYLES``.
"""

from __future__ import annotations

import json

from bounded_loops.graph.adapters.connectors.request_document import ConnectorRequestDocument


def build_openai_chat_request(
    *,
    model: str,
    prompt: str,
    path: str = "/v1/chat/completions",
    scheme: str = "https",
) -> ConnectorRequestDocument:
    """Build a Chat Completions request document from ``model`` and ``prompt``.

    The body is a canonical, deterministic JSON encoding so the sha256 digest is
    stable for equal inputs (sorted keys, no whitespace).

    Args:
        model:  The model identifier (e.g. "gpt-4o-mini").  Comes from the graph
                binding's ``model_target`` — NOT from the AdmittedConnectionRecord —
                so routing is graph-controlled.
        prompt: The run-time user prompt.  This is NEVER baked into the portable graph;
                it is injected at execution time via ``node_prompts``.
        path:   The endpoint path; defaults to "/v1/chat/completions".

    Returns:
        A ``ConnectorRequestDocument`` with scheme="https", no credential headers,
        and a canonical JSON body.
    """
    body = json.dumps(
        {
            "messages": [{"content": prompt, "role": "user"}],
            "model": model,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    return ConnectorRequestDocument(
        path=path,
        headers={"content-type": "application/json"},
        body=body,
        scheme=scheme,
    )
