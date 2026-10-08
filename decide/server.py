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
RESULTS_DIR = "decide-results"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ITEMS = 2000
ITEM_CHARS = 500
CHUNK_CHARS = 20000
CHUNK_ITEMS = 60
ITEM_CONTEXT_CHARS = 4000
WORKERS = 4
SHOWN = 50
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
            "context_file": {"type": "string",
                             "description": "A file to decide over, read by the server so its "
                                            "content never enters your context. Redacted "
                                            "before it is sent; secret stores are refused."},
            "items_file": {"type": "string",
                           "description": "Apply ONE question (question/options/kind) to every "
                                          "item of this file: each non-blank line, each JSONL "
                                          "row, or each element of a JSON array. Returns counts, "
                                          "a results_file with every label, and examples of the "
                                          "labels named in `show`."},
            "show": {"type": "array", "items": {"type": "string"},
                     "description": "items_file mode: labels to list examples of (up to 50)."},
            "max_items": {"type": "integer", "description": "items_file mode: at most 2000, "
                                                           "the default. A file with more says so "
                                                           "(`truncated`)."},
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


def read_file(path):
    """The text of a file the model named, or DecideError. Relative paths are
    taken from the server's working directory, which is the session's."""
    from airlock import rules
    # realpath, not abspath: open() follows symlinks, so the secret-path
    # check must run on the resolved target or a symlink walks past it.
    full = os.path.realpath(os.path.expanduser(str(path)))
    if rules.is_secret_path(full):
        raise DecideError("refusing %s: it looks like a secret store" % path)
    if not os.path.isfile(full):
        raise DecideError("no such file: %s" % path)
    if os.path.getsize(full) > MAX_FILE_BYTES:
        raise DecideError("%s is over %d MB" % (path, MAX_FILE_BYTES // (1024 * 1024)))
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        return full, f.read()


def parse_items(full, text, limit):
    """[(where, item text)]: line numbers for a text or JSONL file, indexes
    for a JSON array. Blank lines are skipped."""
    items = []
    stripped = text.lstrip()
    if full.endswith(".json") and stripped.startswith("["):
        try:
            data = json.loads(text)
        except Exception:
            raise DecideError("%s is not valid JSON" % full) from None
        for i, item in enumerate(data):
            items.append(("item %d" % i, item if isinstance(item, str) else json.dumps(item)))
    else:
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                items.append(("line %d" % n, line.strip()))
    return items[:limit], len(items)


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
    return {"state": {"context": _context(args)}, "model": MODEL, "questions": questions}, order


def _context(args, limit=CONTEXT_CHARS):
    from airlock import redact
    context = str(args.get("context") or "")
    if args.get("context_file"):
        _, text = read_file(args["context_file"])
        context = (context + "\n\n" + text).strip()
    return redact.redact(context)[:limit]


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


def _chunks(items):
    chunk, size = [], 0
    for item in items:
        if chunk and (size + len(item[2]) > CHUNK_CHARS or len(chunk) >= CHUNK_ITEMS):
            yield chunk
            chunk, size = [], 0
        chunk.append(item)
        size += len(item[2])
    if chunk:
        yield chunk


def _item_question(kind, question, options):
    from airlock import redact
    q = {"type": "noul" if kind == "yesno" else kind}
    if kind == "choice":
        q["criteria"] = _options(options)
    elif kind == "score":
        q["criteria"] = [v or k for k, v in _options(options).items()]
    return q, redact.redact(str(question))


def classify_items(args, ask):
    """items_file mode: one question over every item, in chunks of requests
    run WORKERS at a time. Every label goes to a local results file; the
    model gets counts and the examples it asked for."""
    from concurrent.futures import ThreadPoolExecutor
    from airlock import paths, redact
    kind = args.get("kind") or ("choice" if args.get("options") else "yesno")
    if kind not in KINDS:
        raise DecideError("kind must be one of %s" % ", ".join(KINDS))
    if not str(args.get("question") or "").strip():
        raise DecideError("items_file needs a question")
    if kind != "yesno":
        n = len(_options(args.get("options")))
        top = MAX_OPTIONS if kind == "choice" else MAX_LEVELS
        if not 2 <= n <= top:
            raise DecideError("a %s needs 2 to %d options" % (kind, top))
    limit = max(1, min(int(args.get("max_items") or MAX_ITEMS), MAX_ITEMS))
    full, text = read_file(args["items_file"])
    raw, total = parse_items(full, text, limit)
    if not raw:
        raise DecideError("%s has no items" % args["items_file"])
    items = [("i%d" % i, where, redact.redact(t)[:ITEM_CHARS]) for i, (where, t) in enumerate(raw)]
    template, question = _item_question(kind, args["question"], args.get("options"))
    context = _context(args, ITEM_CONTEXT_CHARS)

    def run(chunk):
        qs = {}
        for key, _, _ in chunk:
            q = dict(template)
            q["instructions"] = "%s Answer for `items.%s` only." % (question, key)
            qs[key] = q
        state = {"context": context, "items": {k: t for k, _, t in chunk}}
        response, _ = ask({"state": state, "model": MODEL, "questions": qs}, timeout_s=TIMEOUT_S)
        return response

    started = time.monotonic()
    chunks = list(_chunks(items))
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            responses = list(pool.map(run, chunks))
    except Exception as exc:
        raise DecideError("Jev request failed: %s" % str(exc)[:200]) from None
    latency_ms = int((time.monotonic() - started) * 1000)

    rows, tokens = [], 0
    for chunk, response in zip(chunks, responses):
        tokens += int(((response.get("usage") or {}).get("input_tokens")) or 0)
        shaped = shape(response, [(k, kind) for k, _, _ in chunk], 0)["decisions"]
        for key, where, t in chunk:
            d = shaped.get(key) or {}
            answer = d.get("answer")
            if kind == "score" and answer is not None:
                answer = int(round(answer))
            rows.append({"where": where, "text": t[:160], "answer": answer,
                         "confident": d.get("confident")})
    counts = {}
    for r in rows:
        counts[str(r["answer"])] = counts.get(str(r["answer"]), 0) + 1
    show = {str(x) for x in (args.get("show") or [])}
    shown = [r for r in rows if str(r["answer"]) in show][:SHOWN]
    out_dir = paths.state_file(RESULTS_DIR)
    os.makedirs(str(out_dir), exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    results_file = str(out_dir / ("%s-%s.jsonl" % (stamp, os.path.basename(full))))
    with open(results_file, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return {"items": len(rows), "total_items": total, "truncated": total > len(rows),
            "counts": counts,
            "not_confident": sum(1 for r in rows if not r["confident"]),
            "shown": shown, "results_file": results_file, "requests": len(chunks),
            "latency_ms": latency_ms,
            "jev_cost_usd": round(tokens * PRICE_PER_MTOK_USD / 1e6, 7)}


def call(args, ask=None):
    from airlock import client, keyfile
    if ask is None:
        if not keyfile.get_api_key():
            raise DecideError("no TYPESAFE_API_KEY: see docs/install.md")
        ask = client.ask
    if args.get("items_file"):
        result = classify_items(args, ask)
        _log([("items:" + os.path.basename(str(args["items_file"])), "items")],
             {"decisions": {}, "latency_ms": result["latency_ms"],
              "jev_cost_usd": result["jev_cost_usd"]})
        return result
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
