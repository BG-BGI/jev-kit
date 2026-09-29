"""The harness's decisions, all made by Jev, one request per round.

For every failed gate, three typed questions against the same state:

  fix_kind::<gate>    autofix | code_edit | environment | needs_person
  model::<gate>       haiku | sonnet: the cheapest model enough to write the fix
  same::<gate>        (only from round two) is this the failure the last
                      attempt was meant to fix?

The loop (harness/loop.py) acts on the answers in code. Claude never decides
what to do next; it is only asked to write an edit it has been handed.
"""
PRICE_PER_MTOK_USD = 0.042
MODEL = "jev-latest"
STATE_CHARS = 2500

FIX_KIND = {
    "autofix": {
        "what": "The project's own formatter or linter fixer can resolve every error "
                "shown without judgement: formatting differences, import order, unused "
                "imports, lint findings the tool marks as fixable.",
        "not_for": "Type errors, missing annotations, banned comments, size limits.",
    },
    "code_edit": {
        "what": "Someone has to change code to pass: type errors, missing annotations, "
                "a banned comment to remove, a function to split, duplicated code, a "
                "failing test.",
    },
    "environment": {
        "what": "The code is not the problem: a third-party package, command, service "
                "or file the gate needs is missing or broken.",
    },
    "needs_person": {
        "what": "Passing needs a decision rather than a fix: a ratchet floor or baseline "
                "to move, a rule to change, or a requirement that is ambiguous.",
    },
}
MODEL_CHOICE = {
    "haiku": {
        "what": "A small local edit anyone could make from the error alone: delete a "
                "comment, add an obvious type annotation, rename, lift a constant.",
    },
    "sonnet": {
        "what": "Needs reasoning about behaviour or across files: split a long "
                "function, fix a type error that needs a logic change, deduplicate "
                "code, make a failing test pass.",
    },
}


def _key(name):
    return "".join(c if c.isalnum() else "_" for c in name)


def questions(failed, previous):
    qs = {}
    for name, _ in failed:
        k = _key(name)
        qs["fix_kind::" + k] = {
            "type": "choice",
            "instructions": "How can the failed gate `gates.%s` be made to pass?" % k,
            "criteria": FIX_KIND,
        }
        qs["model::" + k] = {
            "type": "choice",
            "instructions": ("If code has to change to pass `gates.%s`, which model is the "
                             "cheapest one enough to write that change?" % k),
            "criteria": MODEL_CHOICE,
        }
        if previous.get(name):
            qs["same::" + k] = {
                "type": "noul",
                "instructions": ("Is the failure in `gates.%s` the same one as in "
                                 "`previous.%s`, so the last attempt to fix it did not "
                                 "work?" % (k, k)),
            }
    return qs


def state(failed, previous):
    return {
        "gates": {_key(n): out[-STATE_CHARS:] for n, out in failed},
        "previous": {_key(n): (previous.get(n) or "")[-STATE_CHARS:] for n, _ in failed
                     if previous.get(n)},
    }


def decide(failed, previous, ask, timeout_s=10.0):
    """{gate: {fix_kind, fix_confidence, model, same}} plus "_usage"."""
    response, latency_ms = ask({"state": state(failed, previous), "model": MODEL,
                                "questions": questions(failed, previous)},
                               timeout_s=timeout_s)
    answers = response.get("answers") or {}
    out = {}
    for name, _ in failed:
        k = _key(name)
        kind = answers.get("fix_kind::" + k) or {}
        out[name] = {
            "fix_kind": kind.get("choice") or "code_edit",
            "fix_confidence": kind.get("confidence", 0.0),
            "model": (answers.get("model::" + k) or {}).get("choice") or "sonnet",
            "same": float((answers.get("same::" + k) or {}).get("noul") or 0.0),
        }
    usage = response.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0)
    out["_usage"] = {"input_tokens": tokens, "latency_ms": latency_ms,
                     "cost_usd": tokens * PRICE_PER_MTOK_USD / 1e6}
    return out
