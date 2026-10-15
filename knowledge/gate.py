"""The Jev gate for the experience graph: one decide-shaped call per candidate.

`judge` asks one batch of typed questions (store, bucket, novelty, target,
durability, scope, promote) over a redacted candidate and its nearest
neighbours, through `airlock.client.ask` (warm daemon first, the same client
decide/server.py uses). Answers act only at the kit's shared 0.8/0.4 bar. On a
timeout, an error, or an answer below the bar, `judge` returns None and the
caller drops the candidate. Jev never writes prose here, it only gates and
routes.

`pick_for_prompt` is the skill-suggest recipe over stored nodes: one Choice
over node previews plus one `fits::` Noul each, silence is the default.

Every call, timeout, error and below-bar drop is a row in metrics.db through
`airlock.log.append` (guard "knowledge", kind "knowledge"). Nothing here
raises: an exception becomes None or [].
"""
import datetime
import socket
from dataclasses import dataclass
from typing import Optional

from airlock import log, paths, redact

from . import config

MODEL = "jev-latest"
CONFIDENT = (0.8, 0.4)      # confidence, margin: the decide server's bar
NOUL_BAR = CONFIDENT[0] - 0.5  # |p - 0.5| must reach this
FITS_THRESHOLD = 0.50       # suggest/suggest.py
NEIGHBOUR_CHARS = 300
CANDIDATE_CHARS = 1500
PROMPT_CHARS = 1500
MAX_PROMPT_CANDIDATES = 12
LOG_NAME = "knowledge.jsonl"

NOVELTY = ("duplicate", "refines", "contradicts", "new")
SCOPE_LEVELS = ("repo", "machine", "global")
DURABILITY_LEVELS = ["this session only", "this repo", "indefinitely"]

QUESTIONS = {
    "store": ("Is the candidate worth keeping for future sessions at all? Answer no for "
              "noise, one-off output or anything already obvious."),
    "bucket": "Which kind of knowledge is the candidate?",
    "novelty": ("Against the existing nodes listed, is the candidate a duplicate of one, "
                "does it refine one, does it contradict one, or is it new?"),
    "target": "Which existing node does the novelty answer refer to?",
    "durability": "How long will the candidate stay useful?",
    "scope": "Where does the candidate apply?",
    "promote": ("Together with the existing nodes listed, is there now a third supporting "
                "episode that justifies distilling a general pattern?"),
}
BUCKET_HELP = {
    "failure": "an error signature worth matching again",
    "fix": "what resolved a recorded failure",
    "pattern": "a distilled do-X-when-Y",
    "danger": "a near-miss, a denied or destructive call caught",
    "decision": "a choice with its rationale and rejected alternatives",
    "constraint": "a standing rule the human laid down",
    "doc": "an external reference that answered something",
    "env": "a machine fact: versions, ports, a flaky service",
}
NOVELTY_HELP = {
    "duplicate": "says the same thing as an existing node",
    "refines": "adds detail to an existing node",
    "contradicts": "an existing node is now wrong",
    "new": "nothing listed covers it",
}
SCOPE_HELP = {
    "repo": "only this repository",
    "machine": "anywhere on this machine",
    "global": "any machine",
}


@dataclass
class Verdict:
    store: bool
    bucket: str
    novelty: str            # duplicate | refines | contradicts | new
    durability: float       # 0.0 session only .. 1.0 indefinitely
    scope: str              # "repo:<path>" | "machine" | "global"
    promote: bool
    confident: bool
    target: Optional[int] = None  # id of the neighbour novelty refers to


# --- metrics ---------------------------------------------------------------

def log_event(action, **fields):
    """One row in metrics.db (and knowledge.jsonl). Never raises."""
    try:
        entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "guard": "knowledge", "kind": "knowledge", "action": action}
        entry.update({k: v for k, v in fields.items() if v is not None})
        log.append(entry, log_file=paths.state_file(LOG_NAME))
    except Exception:
        return


def _is_timeout(exc):
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _ask(body, timeout_s):
    from airlock import client
    return client.ask(body, timeout_s=timeout_s)


def _usage(response):
    usage = (response or {}).get("usage") if isinstance(response, dict) else None
    return usage if isinstance(usage, dict) else None


# --- answer reading --------------------------------------------------------

def _margin(probabilities):
    values = sorted((float(v) for v in (probabilities or {}).values()), reverse=True)
    if not values:
        return None
    return values[0] - (values[1] if len(values) > 1 else 0.0)


def _noul(answer):
    """(value, confident) for a yes/no answer."""
    if not isinstance(answer, dict) or answer.get("noul") is None:
        return False, False  # no answer is not a confident no
    p = float(answer["noul"])
    return p >= 0.5, abs(p - 0.5) >= NOUL_BAR


def _choice(answer, key="choice"):
    """(pick, confident) for a choice or score answer."""
    a = answer or {}
    pick = a.get(key)
    conf = a.get("confidence")
    margin = _margin(a.get("probabilities"))
    ok = bool(pick is not None and conf is not None and margin is not None
              and conf >= CONFIDENT[0] and margin >= CONFIDENT[1])
    return pick, ok


# --- judge -----------------------------------------------------------------

def _clip(text, n):
    return redact.redact(str(text or ""))[:n]


def _context(c, neighbours):
    lines = ["Candidate (source: %s, tool: %s, bucket hint: %s):" % (
        c.source, c.tool or "-", c.bucket_hint or "none"),
        _clip(c.text, CANDIDATE_CHARS)]
    if c.exception_type:
        lines.append("Exception type: %s" % _clip(c.exception_type, 80))
    if c.entities:
        lines.append("Entities: %s" % _clip(", ".join(map(str, c.entities)), 300))
    lines.append("")
    if neighbours:
        lines.append("Existing nodes:")
        for i, n in enumerate(neighbours, 1):
            lines.append("n%d [%s, seen %d]: %s" % (
                i, n.bucket, n.evidence, _clip(n.text, NEIGHBOUR_CHARS)))
    else:
        lines.append("Existing nodes: none")
    return "\n".join(lines)


def build(c, neighbours):
    """The TypeSafe request body for one candidate. Redacts every string."""
    neighbours = list(neighbours or [])[:3]
    qs = {
        "store": {"type": "noul", "instructions": QUESTIONS["store"]},
        "bucket": {"type": "choice", "instructions": QUESTIONS["bucket"],
                   "criteria": {b: BUCKET_HELP[b] for b in config.BUCKETS}},
        "novelty": {"type": "choice", "instructions": QUESTIONS["novelty"],
                    "criteria": dict(NOVELTY_HELP)},
        "durability": {"type": "score", "instructions": QUESTIONS["durability"],
                       "criteria": list(DURABILITY_LEVELS)},
        "scope": {"type": "choice", "instructions": QUESTIONS["scope"],
                  "criteria": dict(SCOPE_HELP)},
        "promote": {"type": "noul", "instructions": QUESTIONS["promote"]},
    }
    if len(neighbours) >= 2:
        qs["target"] = {"type": "choice", "instructions": QUESTIONS["target"],
                        "criteria": {"n%d" % i: "existing node n%d" % i
                                     for i in range(1, len(neighbours) + 1)}}
    return {"state": {"context": _context(c, neighbours)}, "model": MODEL,
            "questions": qs}, neighbours


def _scope(answer, c):
    if answer == "global":
        return "global"
    if answer == "repo" and str(c.scope).startswith("repo:"):
        return c.scope
    if answer == "repo":
        return "machine"
    return "machine" if answer == "machine" else c.scope


def _durability(answers):
    score, ok = _choice(answers.get("durability"), key="score")
    if not ok:
        return 0.5
    top = len(DURABILITY_LEVELS) - 1
    try:
        return max(0.0, min(1.0, float(score) / top))
    except (TypeError, ValueError):
        return 0.5


def read(answers, c, neighbours):
    """A Verdict from the answers, or (None, reason) when below the bar."""
    store, store_ok = _noul(answers.get("store"))
    if not store_ok:
        return None, "below_bar:store"
    if not store:
        return Verdict(False, c.bucket_hint or "", "new", 0.0, c.scope, False, True), ""
    bucket, bucket_ok = _choice(answers.get("bucket"))
    if not bucket_ok:
        # A hook's own bucket hint stands in for a vague answer.
        bucket = c.bucket_hint if c.bucket_hint in config.BUCKETS else None
    if bucket not in config.BUCKETS:
        return None, "below_bar:bucket"
    novelty, nov_ok = _choice(answers.get("novelty"))
    if not nov_ok or novelty not in NOVELTY:
        return None, "below_bar:novelty"
    target = None
    if novelty != "new":
        if len(neighbours) == 1:
            target = neighbours[0].id
        elif len(neighbours) >= 2:
            pick, ok = _choice(answers.get("target"))
            if not ok or not str(pick).startswith("n"):
                return None, "below_bar:target"
            try:
                target = neighbours[int(str(pick)[1:]) - 1].id
            except (ValueError, IndexError):
                return None, "below_bar:target"
        else:
            novelty = "new"  # nothing to be a duplicate of
    scope_pick, _ = _choice(answers.get("scope"))
    promote, promote_ok = _noul(answers.get("promote"))
    return Verdict(True, bucket, novelty, _durability(answers), _scope(scope_pick, c),
                   bool(promote and promote_ok), True, target), ""


def judge(c, neighbours, ask=None, timeout_s=None):
    """One Jev call for one candidate. A Verdict, or None on timeout, error,
    or an answer below the 0.8/0.4 bar. Every outcome is logged."""
    timeout_s = config.GATE_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        body, neighbours = build(c, neighbours)
    except Exception as exc:
        log_event("gate_error", source=c.source, session_id=c.session_id,
                  error="build: %s" % type(exc).__name__)
        return None
    base = {"source": c.source, "session_id": c.session_id, "tool": c.tool or None,
            "n_neighbours": len(neighbours), "n_questions": len(body["questions"])}
    try:
        response, latency_ms = (ask or _ask)(body, timeout_s=timeout_s)
    except Exception as exc:
        action = "gate_timeout" if _is_timeout(exc) else "gate_error"
        log_event(action, error=str(exc)[:200] or type(exc).__name__, **base)
        return None
    try:
        verdict, reason = read((response or {}).get("answers") or {}, c, neighbours)
    except Exception as exc:
        verdict, reason = None, "read:%s" % type(exc).__name__
    fields = dict(base, latency_ms=latency_ms, usage=_usage(response))
    if verdict is None:
        log_event("gate_below_bar", error=reason, **fields)
        return None
    log_event("gate", bucket=verdict.bucket, novelty=verdict.novelty, store=verdict.store,
              promote=verdict.promote, durability=verdict.durability, **fields)
    return verdict


# --- prompt retrieval ------------------------------------------------------

def pick_for_prompt(prompt, candidates, ask=None, timeout_s=None):
    """Ids of at most PROMPT_NODES nodes worth injecting for this prompt. []
    is the default and the answer to every failure."""
    timeout_s = config.GATE_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        nodes = list(candidates or [])[:MAX_PROMPT_CANDIDATES]
        if not nodes or not str(prompt or "").strip():
            return []
        keys = {"n%d" % n.id: n for n in nodes}
        qs = {"which": {
            "type": "choice",
            "instructions": ("Which of these stored notes, if any, is the most useful to "
                             "have in front of the assistant for the user's latest request?"),
            "criteria": {k: _clip(n.text, config.PREVIEW_CHARS * 2) or None
                         for k, n in keys.items()}}}
        for k, n in keys.items():
            qs["fits::" + k] = {"type": "noul", "instructions": (
                "Would this stored note change what the assistant does for the user's "
                "request? Note: %s" % _clip(n.text, config.PREVIEW_CHARS * 2))}
        body = {"state": {"request": _clip(prompt, PROMPT_CHARS), "recent_context": ""},
                "model": MODEL, "questions": qs}
        try:
            response, latency_ms = (ask or _ask)(body, timeout_s=timeout_s)
        except Exception as exc:
            log_event("prompt_timeout" if _is_timeout(exc) else "prompt_error",
                      error=str(exc)[:200] or type(exc).__name__, n_questions=len(qs))
            return []
        answers = (response or {}).get("answers") or {}
        fits = {k: float((answers.get("fits::" + k) or {}).get("noul") or 0.0) for k in keys}
        winner = (answers.get("which") or {}).get("choice")
        order = sorted(keys, key=lambda k: (k != winner, -fits[k]))
        picked = [keys[k].id for k in order if fits[k] >= FITS_THRESHOLD][:config.PROMPT_NODES]
        log_event("prompt_pick" if picked else "prompt_silent", latency_ms=latency_ms,
                  usage=_usage(response), n_questions=len(qs), n_picked=len(picked))
        return picked
    except Exception:
        return []
