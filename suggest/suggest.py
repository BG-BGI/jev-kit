"""Pick at most one skill for a user turn: TypeSafe's skill-suggestion recipe.

docs/jev-reference/typesafe-docs/cookbooks_skill_suggestion.md is the source.
Two requests per turn:

  1. rank the whole roster with one Choice (`which`), and ask three Nouls
     whether the turn wants an action taken at all (`gate::*`). The mean of
     the three, `prose_suffices` inverted, under GATE_THRESHOLD suggests
     nothing. Only skills the ranking gave some weight reach the shortlist.
  2. rerank the top SHORTLIST with each skill's full description and the
     opening of its SKILL.md, plus one `fits::<name>` Noul per candidate. A
     shortlist whose best fit is under FITS_THRESHOLD suggests nothing.

Measured there, on 488 requests against Haiku 4.5: wrong skill loads fell
from 16.8% to 7.3%, and loads when nothing fits from 9.8% to 4.0%.

`ask` is injected: the hook passes airlock.client.ask (warm daemon first,
HTTPS fallback), tests pass a fake. Anything that goes wrong raises to the
caller, which fails open.
"""
import time

SHORTLIST = 3
# The cookbook's gate is 0.30. Coding and file requests ("write a launch
# file", "build a workbook") scored 0.30-0.40 against it on eval/skill-cases,
# a hair from being silenced, while every no-skill request scored 0.25 or
# less. 0.20 lets those through; the rerank's fits check still rejects a
# turn nothing fits (python3 -m suggest.eval, 2026-09-29).
GATE_THRESHOLD = 0.20
MIN_RANK_PROBABILITY = 0.01
FITS_THRESHOLD = 0.30
WIDE_DESCRIPTION_CHARS = 250
MAX_CHOICE_OPTIONS = 250
MIN_WORDS = 4
MODEL = "jev-latest"

CHOICE_INSTRUCTIONS = (
    "Which of these skills, if any, is the right one to load to help with the "
    "user's latest request?"
)
RERANK_INSTRUCTIONS = (
    "Exactly one of these skills is the right one to load for the user's latest "
    "request. Which one? Read what each actually does, not just its name."
)
GATE_QUESTIONS = {
    "acts_on_user_system": (
        "Is the assistant being asked to act on the user's files, accounts, devices, "
        "or online services, rather than only to explain or advise?"
    ),
    "would_follow_documented_procedure": (
        "Would a careful expert answering this consult a specific documented procedure "
        "or set of commands, rather than answering from general understanding?"
    ),
    "prose_suffices": (
        "Could a knowledgeable generalist fully satisfy this request in prose, with "
        "no tools, no documentation, and no access to the user's files or accounts?"
    ),
}
INVERTED = frozenset(["prose_suffices"])

# The cookbook's measured wording. It is an input the agent was graded on, so
# a reworded block is an unmeasured one.
SUGGEST_TEXT = (
    "Relevant to the current request: %s. Ignore this if it does not fit what the "
    "user actually asked for."
)
NONE_TEXT = "No skill in the roster appears relevant to this request."


def skip_reason(prompt):
    """Why this prompt is not worth a judgement, or None. Code only, free.

    A slash command already names its skill, a `!` line is a shell command,
    and a turn of a few words ("2", "commit it") leans on context the
    request alone does not carry, so ranking it would be a guess."""
    text = (prompt or "").strip()
    if not text:
        return "empty"
    if text.startswith("/"):
        return "slash_command"
    if text.startswith("!"):
        return "shell"
    if "<command-name>" in text:
        return "slash_command"
    if len(text.split()) < MIN_WORDS:
        return "too_short"
    return None


def _state(prompt):
    return {"request": prompt, "recent_context": ""}


def _chunks(roster):
    for start in range(0, len(roster), MAX_CHOICE_OPTIONS):
        yield roster[start:start + MAX_CHOICE_OPTIONS]


def wide_questions(roster):
    qs = {}
    for i, chunk in enumerate(_chunks(roster)):
        qs["which::%d" % i] = {
            "type": "choice",
            "instructions": CHOICE_INSTRUCTIONS,
            "criteria": {s["name"]: (s["description"][:WIDE_DESCRIPTION_CHARS] or None)
                         for s in chunk},
        }
    for key, text in GATE_QUESTIONS.items():
        qs["gate::%s" % key] = {"type": "noul", "instructions": text}
    return qs


def rerank_questions(candidates):
    qs = {
        "which": {
            "type": "choice",
            "instructions": RERANK_INSTRUCTIONS,
            "criteria": {s["name"]: (" -- ".join(t for t in (s["description"], s["body"]) if t) or None)
                         for s in candidates},
        }
    }
    for s in candidates:
        qs["fits::%s" % s["name"]] = {
            "type": "noul",
            "instructions": _fits_text(s),
        }
    return qs


def _fits_text(skill):
    text = ("Does the skill '%s' do the specific thing the user's request asks for?"
            % skill["name"])
    if skill["description"]:
        text += " It is described as: %s" % skill["description"]
    return text


def _gate(answers):
    values = {}
    for key, answer in answers.items():
        if key.startswith("gate::"):
            values[key[len("gate::"):]] = float((answer or {}).get("noul") or 0.0)
    if not values:
        return 0.0, values
    oriented = [(1.0 - v) if k in INVERTED else v for k, v in values.items()]
    return sum(oriented) / len(oriented), values


def _ranked(answers):
    pairs = []
    for key, answer in answers.items():
        if key.startswith("which::"):
            for name, p in ((answer or {}).get("probabilities") or {}).items():
                pairs.append((float(p or 0.0), name))
    pairs.sort(key=lambda kv: (-kv[0], kv[1]))
    ranked = [name for p, name in pairs if p >= MIN_RANK_PROBABILITY]
    return ranked or [name for _, name in pairs[:1]]


def suggest(prompt, roster, ask, timeout_s=2.0, model=MODEL):
    """At most one skill name for a prompt. Returns a result dict:

      skill      the name, or None
      reason     "suggested", "gate", "nothing_fits", "no_roster"
      gate, gate_values, shortlist, fits, winner, latency_ms, tokens
    """
    result = {"skill": None, "reason": "no_roster", "latency_ms": 0, "tokens": 0}
    if not roster:
        return result
    by_name = {s["name"]: s for s in roster}
    started = time.perf_counter()

    wide, _ = ask({"state": _state(prompt), "model": model, "questions": wide_questions(roster)},
                  timeout_s=timeout_s)
    answers = wide.get("answers") or {}
    result["tokens"] += _tokens(wide)
    gate, values = _gate(answers)
    result.update({"gate": round(gate, 3), "gate_values": values})
    if gate < GATE_THRESHOLD:
        result["reason"] = "gate"
        result["latency_ms"] = _ms(started)
        return result

    shortlist = [n for n in _ranked(answers) if n in by_name][:SHORTLIST]
    result["shortlist"] = shortlist
    if not shortlist:
        result["reason"] = "nothing_fits"
        result["latency_ms"] = _ms(started)
        return result

    second, _ = ask({"state": _state(prompt), "model": model,
                     "questions": rerank_questions([by_name[n] for n in shortlist])},
                    timeout_s=timeout_s)
    answers2 = second.get("answers") or {}
    result["tokens"] += _tokens(second)
    fits = {k[len("fits::"):]: float((a or {}).get("noul") or 0.0)
            for k, a in answers2.items() if k.startswith("fits::")}
    winner = (answers2.get("which") or {}).get("choice")
    result.update({"fits": fits, "winner": winner, "latency_ms": _ms(started)})
    if not fits or max(fits.values()) < FITS_THRESHOLD or winner not in by_name:
        result["reason"] = "nothing_fits"
        return result
    result.update({"skill": winner, "reason": "suggested"})
    return result


def context_block(skill):
    body = SUGGEST_TEXT % skill if skill else NONE_TEXT
    return "<skill_relevance>\n%s\n</skill_relevance>" % body


def _tokens(response):
    usage = (response or {}).get("usage") or {}
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _ms(started):
    return int((time.perf_counter() - started) * 1000)
