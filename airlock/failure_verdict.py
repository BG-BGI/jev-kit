"""Read a run of tool failures and say whether retrying is the right move.

One Jev request over the session's recent failures (airlock/failure_state.py).
The `verdict` Choice has five answers; only three are ever said to the model,
and only past the shared deny bar (confidence >= 0.8, margin >= 0.4):

  change_approach  the same kind of attempt keeps failing; retrying it again
                   is the expensive mistake this exists to stop
  gather_info      the errors point at state nobody has looked at yet
  ask_user         credentials, permissions, network, or a decision only the
                   person can make
  retry_with_fix   a small, obvious correction to the last call -- silent
  expected         failing was the point (a red test, a probe) -- silent

Everything that leaves the machine goes through airlock/redact.py first: the
tool name, a one-line summary of each call and the head of each error.
"""
from . import policy, redact

MODEL = "jev-latest"
SPOKEN = ("change_approach", "gather_info", "ask_user")
SUMMARY_CHARS = 300
ERROR_CHARS = 300

# One Choice, measured against the alternative. Four atomic Nouls combined in
# code were tried too and did worse on eval/failure-cases.jsonl (5 of 13 right,
# 5 wrong verdicts said aloud): "is the agent guessing" read high on nearly
# every failure, and "only the person can fix this" read low on a plain 403.
QUESTION = {
    "question": (
        "A coding agent's recent tool calls in this session failed, listed oldest "
        "first in `failures`. What should the agent do next?"
    ),
    "focus": (
        "Judge the run as a whole, not just the last error. Repeated failures of "
        "the same kind of attempt mean the approach is wrong, even when each "
        "error looks small."
    ),
}

CRITERIA = {
    "retry_with_fix": {
        "what": "The last failure has a small, obvious fix to the same call: a typo, "
                "a wrong path or flag, a missing argument. The earlier failures, if "
                "any, were different problems that are already solved.",
        "not_for": "The same error, or the same kind of attempt, failing again.",
    },
    "change_approach": {
        "what": "The same kind of attempt has failed more than once for the same "
                "underlying reason. Another variation of it will fail too; the agent "
                "should step back and try a different approach.",
        "examples": ["the same test failing with the same assertion after three edits",
                     "a build failing on the same missing symbol after repeated tweaks"],
    },
    "gather_info": {
        "what": "The errors point at state the agent has not inspected: a file, "
                "config, version, schema or output it is guessing about. It should "
                "read or query that before trying again.",
    },
    "ask_user": {
        "what": "Only the person can unblock this: missing credentials or keys, "
                "permission denied by policy, a network or service outage, hardware "
                "that is not connected, or a choice between options that is theirs.",
    },
    "expected": {
        "what": "The failure is informative and the agent meant it: a test written "
                "to fail first, grep finding no match, a probe for whether "
                "something exists.",
    },
}


def call_summary(tool_name, tool_input):
    """One redacted line saying what the failed call was."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "pattern", "url", "query", "description"):
        if ti.get(key):
            text = "%s: %s" % (key, ti.get(key))
            break
    else:
        text = ", ".join("%s=%s" % (k, str(v)[:60]) for k, v in list(ti.items())[:4])
    return redact.redact(" ".join(str(text).split()))[:SUMMARY_CHARS]


def state(failures):
    return {"failures": [
        {"tool": f.get("tool"), "call": f.get("summary"),
         "error": redact.redact(str(f.get("error") or ""))[:ERROR_CHARS]}
        for f in failures]}


def questions():
    return {"verdict": {"type": "choice", "instructions": QUESTION, "criteria": CRITERIA}}


def decide(failures, ask, timeout_s=2.0):
    """The verdict for a run of failures, as a dict: verdict, confidence,
    margin, speak (bool), latency_ms, usage. Raises on transport errors; the
    hook turns those into silence."""
    response, latency_ms = ask({"state": state(failures), "model": MODEL,
                                "questions": questions()}, timeout_s=timeout_s)
    answer = (response.get("answers") or {}).get("verdict") or {}
    verdict = answer.get("choice")
    confidence = answer.get("confidence", 0.0)
    margin = policy.compute_margin(answer.get("probabilities"))
    return {
        "verdict": verdict,
        "confidence": confidence,
        "margin": margin,
        "speak": verdict in SPOKEN and policy.meets_deny_bar(confidence, margin),
        "latency_ms": latency_ms,
        "usage": response.get("usage"),
    }


ADVICE = {
    "change_approach": (
        "The same kind of attempt has now failed %d times in a row. Another "
        "variation is unlikely to work: step back, say what you have learned, and "
        "try a different approach."
    ),
    "gather_info": (
        "%d failures so far, and the errors point at something not yet inspected. "
        "Read or query it before the next attempt instead of guessing."
    ),
    "ask_user": (
        "%d failures so far, and this looks like something only the user can fix "
        "(credentials, permissions, network, hardware or a decision). Stop "
        "retrying and ask them."
    ),
}


def advice(verdict, count):
    return "airlock failure check: %s\nAdvice only -- nothing was blocked." % (
        ADVICE[verdict] % count)
