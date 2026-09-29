#!/usr/bin/env python3
"""An MCP server with one tool, `decide`: ask Jev for a typed, calibrated
decision instead of reasoning it out in tokens.

Claude calls it when a decision is a pick between options it can name: which
of these files, which approach, is this true of this text, how severe is
this. Jev answers in about 0.3 s with a probability per option, and a batch
of decisions over the same context costs about the same as one. What Claude
saves is the reasoning it would otherwise write, and the context it would
otherwise read at length to make twenty small calls itself.

It never generates text. A decision that needs an answer nobody can list
ahead of time is not a `decide` call.

    {"context": "...facts...",
     "decisions": [{"name": "severity", "kind": "score", "question": "...",
                    "options": {"0": "cosmetic", "1": "degraded", "2": "broken"}},
                   {"name": "is_regression", "kind": "yesno", "question": "..."}]}

A single decision can be given at the top level (question, options, kind)
instead of `decisions`.

Every context and question goes through airlock/redact.py before it leaves
the machine, and each call is a row in ~/.local/state/airlock/decide.jsonl.
Standard library only, like browse/server.py.
"""
import datetime
import json
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
for _p in (HERE, ""):
    while _p in sys.path:
        sys.path.remove(_p)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SERVER_NAME = "jev-decide"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MODEL = "jev-latest"
PRICE_PER_MTOK_USD = 0.042
CONTEXT_CHARS = 60000
MAX_DECISIONS = 20
MAX_OPTIONS = 255
MAX_LEVELS = 10
TIMEOUT_S = 10.0
CONFIDENT = (0.8, 0.4)
LOG_NAME = "decide.jsonl"
KINDS = ("choice", "score", "yesno")

TOOL = {
    "name": "decide",
    "description": (
        "Ask Jev (TypeSafe's System One model) for a calibrated decision between options "
        "you can name, instead of reasoning it out yourself. Returns the pick, a "
        "probability per option, and whether it is confident. Use it for: choosing among "
        "candidate files, approaches, tools or skills; classifying items; yes/no checks "
        "over long text; severity or quality scores. Batch every decision that shares a "
        "context into one call: twenty decisions cost about what one does. Never for "
        "writing text or code, and never for a decision whose answers you cannot list."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "context": {"type": "string",
                        "description": "The facts to decide over: the text, listing, diff or "
                                       "description. Redacted before it is sent."},
            "decisions": {
                "type": "array", "maxItems": MAX_DECISIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "question": {"type": "string"},
                        "options": {"description": "Choice or score options: an object of "
                                                   "name to description, or a list of names. "
                                                   "Score options go lowest first.",
                                    "type": ["object", "array"]},
                    },
                    "required": ["question"],
                },
            },
            "question": {"type": "string", "description": "A single decision's question."},
            "options": {"type": ["object", "array"]},
            "kind": {"type": "string", "enum": list(KINDS)},
        },
    },
}


class DecideError(Exception):
    pass


def _options(raw):
    if isinstance(raw, list):
        return {str(o): None for o in raw}
    if isinstance(raw, dict):
        return {str(k): (str(v) if v not in (None, "") else None) for k, v in raw.items()}
    return {}


def build(args):
    """(request body, [(name, kind)]) from tool arguments. Raises DecideError."""
    from airlock import redact
    decisions = args.get("decisions")
    if not decisions:
        decisions = [{"name": "decision", "question": args.get("question"),
                      "options": args.get("options"), "kind": args.get("kind")}]
    if not isinstance(decisions, list) or len(decisions) > MAX_DECISIONS:
        raise DecideError("decisions must be a list of at most %d" % MAX_DECISIONS)
    questions, order = {}, []
    for i, d in enumerate(decisions):
        if not isinstance(d, dict) or not str(d.get("question") or "").strip():
            raise DecideError("decision %d has no question" % i)
        name = str(d.get("name") or "decision_%d" % i)
        kind = d.get("kind") or ("choice" if d.get("options") else "yesno")
        if kind not in KINDS:
            raise DecideError("decision %r: kind must be one of %s" % (name, ", ".join(KINDS)))
        question = {"type": "noul" if kind == "yesno" else kind,
                    "instructions": redact.redact(str(d["question"]))}
        if kind == "choice":
            options = _options(d.get("options"))
            if not 2 <= len(options) <= MAX_OPTIONS:
                raise DecideError("decision %r needs 2 to %d options" % (name, MAX_OPTIONS))
            question["criteria"] = options
        elif kind == "score":
            levels = [v or k for k, v in _options(d.get("options")).items()]
            if not 2 <= len(levels) <= MAX_LEVELS:
                raise DecideError("decision %r needs 2 to %d score levels, lowest first"
                                  % (name, MAX_LEVELS))
            question["criteria"] = levels
        if name in questions:
            raise DecideError("two decisions are named %r" % name)
        questions[name] = question
        order.append((name, kind))
    context = redact.redact(str(args.get("context") or ""))[:CONTEXT_CHARS]
    return {"state": {"context": context}, "model": MODEL, "questions": questions}, order


def _margin(probabilities):
    values = sorted((float(v) for v in (probabilities or {}).values()), reverse=True)
    if not values:
        return None
    return values[0] - (values[1] if len(values) > 1 else 0.0)


def shape(response, order, latency_ms):
    answers = response.get("answers") or {}
    out = {}
    for name, kind in order:
        a = answers.get(name) or {}
        if kind == "yesno":
            p = float(a.get("noul") or 0.0)
            out[name] = {"answer": p >= 0.5, "probability_true": round(p, 3),
                         "confident": abs(p - 0.5) >= CONFIDENT[0] - 0.5}
            continue
        probs = {k: round(float(v), 3) for k, v in (a.get("probabilities") or {}).items()}
        conf = a.get("confidence")
        margin = _margin(probs)
        out[name] = {"answer": a.get("score") if kind == "score" else a.get("choice"),
                     "confidence": conf, "margin": margin, "probabilities": probs,
                     "confident": bool(conf is not None and margin is not None
                                       and conf >= CONFIDENT[0] and margin >= CONFIDENT[1])}
        if kind == "score" and a.get("legend"):
            out[name]["legend"] = a.get("legend")
    tokens = int(((response.get("usage") or {}).get("input_tokens")) or 0)
    return {"decisions": out, "latency_ms": latency_ms,
            "jev_cost_usd": round(tokens * PRICE_PER_MTOK_USD / 1e6, 7)}


def call(args, ask=None):
    from airlock import client, keyfile
    if ask is None:
        if not keyfile.get_api_key():
            raise DecideError("no TYPESAFE_API_KEY: see docs/install.md")
        ask = client.ask
    body, order = build(args)
    started = time.monotonic()
    try:
        response, latency_ms = ask(body, timeout_s=TIMEOUT_S)
    except Exception as exc:
        raise DecideError("Jev request failed: %s" % str(exc)[:200]) from None
    result = shape(response, order, latency_ms or int((time.monotonic() - started) * 1000))
    _log(order, result)
    return result


def _log(order, result):
    try:
        from airlock import log, paths
        log.append({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "guard": "decide", "decisions": [n for n, _ in order],
                    "answers": {n: d.get("answer") for n, d in result["decisions"].items()},
                    "latency_ms": result["latency_ms"], "jev_cost_usd": result["jev_cost_usd"]},
                   log_file=paths.state_file(LOG_NAME))
    except Exception:
        pass


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def tool_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def handle(msg, ask=None):
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
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [TOOL]})
    if method == "tools/call":
        if params.get("name") != TOOL["name"]:
            return _error(msg_id, -32602, "Unknown tool: %r" % (params.get("name"),))
        try:
            return _result(msg_id, tool_result(json.dumps(call(params.get("arguments") or {}, ask))))
        except DecideError as exc:
            return _result(msg_id, tool_result("decide failed: %s" % exc, is_error=True))
        except Exception as exc:
            return _result(msg_id, tool_result("decide failed: %s" % type(exc).__name__,
                                               is_error=True))
    return _error(msg_id, -32601, "Method not found: %s" % method)


def serve(stdin, stdout, ask=None):
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
                responses = [r for r in (handle(m, ask) for m in batch) if r is not None]
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
