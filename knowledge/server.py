#!/usr/bin/env python3
"""The experience graph's MCP server. EXPERIMENTAL. This is NOT the decide
server: `decide` is stateless judgment and never writes, this one remembers
and does write. Two servers, two one-sentence contracts.

Three tools:

    graph_log(bucket, text, entities?, rejected_alternatives?, scope?)
        Claude writes one node at the moment of deciding. It goes through the
        same ingest path as a hook's candidate, so the Jev gate judges it
        first, except an explicit `constraint`, which is stored verbatim.
    graph_search(query?, bucket?)
        A local read. No Jev call, no network.
    graph_constraint_drop(id)
        Remove one constraint (or any node) by id.

The trigger rule for WHEN to call `graph_log` ships in `INSTRUCTIONS`, sent
on `initialize`, so nobody edits a `CLAUDE.md`. Text is redacted by
airlock/redact.py before it is stored. Nothing here raises into the host: a
failure is a tool error. Standard library only, like decide/server.py.
"""
import json
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
for _p in (HERE, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SERVER_NAME = "jev-knowledge"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "This is the experience graph (experimental). It is NOT the decide server: decide judges "
    "and never writes, this server remembers and does write.\n"
    "Call mcp__jev-knowledge__graph_log when you abandon an approach, or choose between named "
    "alternatives. Log the choice, why, and the path you rejected (bucket `decision`, with "
    "`rejected_alternatives` filled in: a decision without them is refused). Log bucket `doc` "
    "when an external reference answered something you needed. Dead ends are the point: "
    "abandon an approach, log why.\n"
    "NEVER log secrets, tokens, keys or credentials.\n"
    "Log bucket `constraint` ONLY when the human explicitly stated a standing rule in their own "
    "words (\"remember:\", \"from now on\"). Never infer one from a correction.\n"
    "Keep each log to one or two plain sentences. Do not log routine successes or anything you "
    "can re-derive from the code.\n"
    "If this tool's schema is not loaded, load it with ToolSearch select:"
    "mcp__jev-knowledge__graph_log, then call it.\n"
    "graph_search reads the graph locally. graph_constraint_drop removes one node by id."
)
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
BUCKETS = ("failure", "fix", "pattern", "danger", "decision", "constraint", "doc", "env")
TEXT_CHARS = 1000
ITEM_CHARS = 300
MAX_LIST = 20
SEARCH_K = 5
SHOWN_TEXT = 200

TOOLS = [
    {
        "name": "graph_log",
        # Claude Code defers MCP tool schemas by default; a deferred graph_log
        # is never called in practice, so the write path stays empty. This
        # _meta hint asks the client to load it upfront (v2.1.287+; older
        # clients ignore unknown _meta keys).
        "_meta": {"anthropic/alwaysLoad": True},
        "description": (
            "Write one node to the experience graph (experimental, stateful: this stores "
            "something). Use it when you abandon an approach or choose between named "
            "alternatives (bucket `decision`: say what you chose, why, and list the "
            "`rejected_alternatives`), or when an external reference answered something "
            "(bucket `doc`). A `constraint` only when the human said it explicitly. Never "
            "log secrets. Jev gates the write, so a duplicate or low-value entry may be "
            "dropped; the result says what happened."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bucket": {"type": "string", "enum": list(BUCKETS)},
                "text": {"type": "string",
                         "description": "One or two plain sentences. Redacted before storage."},
                "entities": {"type": "array", "items": {"type": "string"},
                             "description": "File paths, components, commands or skill names "
                                            "this is about."},
                "rejected_alternatives": {"type": "array", "items": {"type": "string"},
                                          "description": "Required for `decision`: the paths "
                                                         "you did not take."},
                "scope": {"type": "string", "enum": ["repo", "machine", "global"],
                          "description": "Default repo (this working directory's repo)."},
            },
            "required": ["bucket", "text"],
        },
    },
    {
        "name": "graph_search",
        "description": (
            "Search the experience graph locally (no Jev call). With `query`, the nearest "
            "nodes; without, the top nodes for this repo. `bucket` filters. Read only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "bucket": {"type": "string", "enum": list(BUCKETS)},
            },
        },
    },
    {
        "name": "graph_constraint_drop",
        "description": ("Remove one node from the experience graph by id, normally a "
                        "constraint the human no longer wants. Only on the human's say-so."),
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
]


class GraphError(Exception):
    pass


def _strs(raw, name):
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        raise GraphError("%s must be a list of strings" % name)
    from airlock import redact
    return tuple(redact.redact(x)[:ITEM_CHARS] for x in raw if x.strip())[:MAX_LIST]


def _scope(raw, store, cwd):
    scopes = store.scopes_for(cwd)
    if raw in (None, "", "repo"):
        return scopes[0]
    if raw in ("machine", "global"):
        return raw
    raise GraphError("scope must be repo, machine or global")


def graph_log(args, ingest=None, store=None, cwd=None):
    from airlock import redact
    from knowledge import types
    if ingest is None:
        from knowledge import ingest as ingest
    if store is None:
        from knowledge import store
    bucket = args.get("bucket")
    if bucket not in BUCKETS:
        raise GraphError("bucket must be one of %s" % ", ".join(BUCKETS))
    text = redact.redact(str(args.get("text") or "")).strip()[:TEXT_CHARS]
    if not text:
        raise GraphError("text is empty")
    rejected = _strs(args.get("rejected_alternatives"), "rejected_alternatives")
    if bucket == "decision" and not rejected:
        raise GraphError("a decision needs rejected_alternatives: log what you did not choose")
    cand = types.Candidate(
        bucket_hint=bucket, text=text, entities=_strs(args.get("entities"), "entities"),
        scope=_scope(args.get("scope"), store, cwd or os.getcwd()),
        provenance="mcp graph_log",
        source="explicit" if bucket == "constraint" else "self_report",
        rejected_alternatives=rejected)
    return ingest.ingest(cand)


def _node(n):
    from airlock import redact
    return {"id": n.id, "bucket": n.bucket, "scope": n.scope,
            "text": redact.redact(n.text)[:SHOWN_TEXT], "evidence": n.evidence}


def graph_search(args, store=None, cwd=None):
    if store is None:
        from knowledge import store
    bucket = args.get("bucket")
    if bucket is not None and bucket not in BUCKETS:
        raise GraphError("bucket must be one of %s" % ", ".join(BUCKETS))
    scopes = store.scopes_for(cwd or os.getcwd())
    query = str(args.get("query") or "").strip()
    if bucket == "constraint":
        nodes = store.constraints()
    elif query:
        nodes = store.nearest(query, "", (), scopes, k=SEARCH_K * 2)
    elif bucket and hasattr(store, "list_nodes"):
        nodes = store.list_nodes(bucket, SEARCH_K)
    else:
        nodes = store.top_for_scopes(scopes, SEARCH_K * 2)
    if bucket:
        nodes = [n for n in nodes if n.bucket == bucket]
    return {"nodes": [_node(n) for n in nodes[:SEARCH_K]]}


def graph_constraint_drop(args, store=None):
    if store is None:
        from knowledge import store
    node_id = args.get("id")
    if isinstance(node_id, bool) or not isinstance(node_id, int):
        raise GraphError("id must be an integer")
    return {"dropped": bool(store.drop(node_id)), "id": node_id}


def call(name, args, deps=None):
    deps = deps or {}
    if name == "graph_log":
        return graph_log(args, ingest=deps.get("ingest"), store=deps.get("store"),
                         cwd=deps.get("cwd"))
    if name == "graph_search":
        return graph_search(args, store=deps.get("store"), cwd=deps.get("cwd"))
    if name == "graph_constraint_drop":
        return graph_constraint_drop(args, store=deps.get("store"))
    raise KeyError(name)


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def handle(msg, deps=None):
    """One JSON-RPC message in, one response out, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" \
            or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        wanted = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(msg_id, {
            "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return _error(msg_id, -32602, "Unknown tool: %r" % (name,))
        try:
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise GraphError("arguments must be an object")
            return _result(msg_id, tool_result(json.dumps(call(name, args, deps))))
        except GraphError as exc:
            return _result(msg_id, tool_result("%s failed: %s" % (name, exc), is_error=True))
        except Exception as exc:
            return _result(msg_id, tool_result("%s failed: %s" % (name, type(exc).__name__),
                                               is_error=True))
    return _error(msg_id, -32601, "Method not found: %s" % method)


def serve(stdin, stdout, deps=None):
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                responses = [_error(None, -32700, "Parse error")]
            else:
                batch = msg if isinstance(msg, list) else [msg]
                responses = [r for r in (handle(m, deps) for m in batch) if r is not None]
            for response in responses:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


def main():
    def stop(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
