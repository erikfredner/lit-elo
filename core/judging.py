"""Prompting and scoring for LLM canonicity judgments.

Each vote asks the model only which of two items is more canonical (1 or 2).
Votes alternate presentation order (AB, BA, AB, …) to cancel position bias, and
the matchup's score for A is the share of votes A won: with two votes that is
1, 0.5 (the model followed presentation order) or 0. Probabilities come from
the rating fit across many games, not from the model's self-report.

Legacy votes carrying a stated confidence (50–100) or token logprobs are still
parsed and scored as probabilities, clamped to [P_MIN, P_MAX].
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
DEFAULT_PROMPT_NAME = "professor"
SYSTEM_PROMPT_PATH = PROMPT_DIR / f"{DEFAULT_PROMPT_NAME}.md"

OUTPUT_INSTRUCTIONS = "Answer 1 or 2."

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "canonicity_judgment",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "more_canonical": {"type": "integer", "enum": [1, 2]},
            },
            "required": ["more_canonical"],
            "additionalProperties": False,
        },
    },
}

# Clamp probabilistic votes (stated confidence or logprobs) so one overconfident
# vote can't produce a full 0/1 result on its own. Binary votes are not clamped.
P_MIN, P_MAX = 0.02, 0.98


def load_system_prompt(path: Path | None = None) -> str:
    return (path or SYSTEM_PROMPT_PATH).read_text(encoding="utf-8").strip()


def resolve_system_prompt(name: str | None = None, path: Path | None = None, replicate: bool = False):
    """The ``SystemPrompt`` a run should use, registering it on first use.

    ``path`` reads that file (named by its stem unless ``name`` is given);
    ``name`` alone reads ``prompts/<name>.md``, or reuses a registered prompt
    whose file is gone; neither gives the default prompt. A registered name
    whose file text has changed is an error, so matchups always point at the
    exact text they were judged under: save the new wording under a new name.
    Text already registered under another name is an error unless
    ``replicate`` (a test–retest run of the same wording under a new name).
    """
    from .models import SystemPrompt

    if path is None and name is None:
        default = SystemPrompt.objects.filter(is_default=True).first()
        if default is not None:
            return default
        name = DEFAULT_PROMPT_NAME
    if path is None:
        path = PROMPT_DIR / f"{name}.md"
    name = name or path.stem
    text = load_system_prompt(path) if path.exists() else None
    existing = SystemPrompt.objects.filter(name=name).first()
    if existing is not None:
        if text is not None and text != existing.text:
            raise ValueError(f"Prompt {name!r} is registered with different text; save the new wording under a new name.")
        return existing
    if text is None:
        raise ValueError(f"No prompt named {name!r} and no file {path}.")
    same = SystemPrompt.objects.filter(text=text).first()
    if same is not None and not replicate:
        raise ValueError(f"That text is already registered as {same.name!r} (pass replicate for a retest run).")
    return SystemPrompt.objects.create(
        name=name, text=text, is_default=not SystemPrompt.objects.filter(is_default=True).exists())


def item_label(item, mode: str) -> str:
    if mode in ("authors", "author"):
        return item.name
    return f"{item.title} — {item.author.name}"


def build_user_prompt(item_a, item_b, mode: str) -> str:
    return f"1: {item_label(item_a, mode)}\n2: {item_label(item_b, mode)}"


def vote_orders(votes_per_pairing: int) -> list[str]:
    return ["AB" if i % 2 == 0 else "BA" for i in range(votes_per_pairing)]


# ── parsing ───────────────────────────────────────────────────────────────────

_JSON_RE = re.compile(r"\{[^{}]*\}", re.S)


def parse_judgment(content: str | None) -> tuple[int, int | None] | None:
    """Return (verdict, confidence) from a model reply, or None if unparseable.

    Accepts strict JSON, JSON embedded in prose/code fences, or a bare 1/2.
    """
    if not content:
        return None
    text = content.strip()
    candidates = [text] + _JSON_RE.findall(text)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            v = obj.get("more_canonical", obj.get("verdict"))
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v in (1, 2):
                conf = obj.get("confidence")
                try:
                    conf = int(round(float(conf)))
                    conf = min(100, max(50, conf))
                except (TypeError, ValueError):
                    conf = None
                return v, conf
    m = re.fullmatch(r"\W*([12])\W*", text)
    if m:
        return int(m.group(1)), None
    return None


def logprob_p_first(logprobs: dict | None) -> float | None:
    """P(first-shown item wins) from chat-completions token logprobs.

    Finds the token carrying the verdict digit (the first "1"/"2" token after
    ``more_canonical``) and renormalises over the "1" and "2" alternatives in
    its ``top_logprobs``.
    """
    if not logprobs:
        return None
    tokens = logprobs.get("content") or []
    seen_key = False
    text_so_far = ""
    for tok in tokens:
        t = tok.get("token", "")
        text_so_far += t
        if not seen_key:
            if "more_canonical" in text_so_far or "verdict" in text_so_far:
                seen_key = True
            continue
        if t.strip() not in ("1", "2"):
            continue
        probs = {"1": 0.0, "2": 0.0}
        alts = tok.get("top_logprobs") or [{"token": t, "logprob": tok.get("logprob", 0.0)}]
        for alt in alts:
            k = alt.get("token", "").strip()
            if k in probs:
                probs[k] += math.exp(alt.get("logprob", -100.0))
        total = probs["1"] + probs["2"]
        if total <= 0:
            return None
        return probs["1"] / total
    return None


def vote_record(order: str, content: str | None, logprobs: dict | None = None) -> dict | None:
    """Turn one model reply into a vote dict with ``p_a`` (P(item A wins))."""
    parsed = parse_judgment(content)
    if parsed is None:
        return None
    verdict, confidence = parsed
    p_logprob = logprob_p_first(logprobs)
    if p_logprob is not None:
        p_first = min(P_MAX, max(P_MIN, p_logprob))
    elif confidence is not None:
        c = confidence / 100.0
        p_first = min(P_MAX, max(P_MIN, c if verdict == 1 else 1.0 - c))
    else:  # binary vote
        p_first = 1.0 if verdict == 1 else 0.0
    p_a = p_first if order == "AB" else 1.0 - p_first
    return {
        "order": order,
        "verdict": verdict,
        "confidence": confidence,
        "p_logprob": None if p_logprob is None else round(p_logprob, 4),
        "p_a": round(p_a, 4),
    }


def score_from_votes(votes: list[dict]) -> float | None:
    if not votes:
        return None
    return sum(v["p_a"] for v in votes) / len(votes)


# ── decision models ───────────────────────────────────────────────────────────
# Decision models (OpenRouter's /api/alpha/decisions, e.g. typesafe/jev-1.14)
# take no chat messages and emit no text: they answer typed questions about a
# ``state``. A pairing is one ``choice`` question whose options are "1" and "2",
# answered with the pick and a probability per option.

DECISION_QUESTION = "more_canonical"


def build_decision_body(first, second, mode: str, instructions: str) -> dict:
    return {
        "state": build_user_prompt(first, second, mode),
        "questions": {
            DECISION_QUESTION: {
                "type": "choice",
                "instructions": instructions,
                "criteria": {"1": item_label(first, mode), "2": item_label(second, mode)},
            },
        },
    }


def decision_vote_record(order: str, answers: dict | None, probabilistic: bool = False) -> dict | None:
    """Turn a decisions answer into a vote dict, like ``vote_record``.

    Binary by default (the pick), so votes score like every other judge's;
    with ``probabilistic`` the reported probability is used, clamped like logprobs.
    A refusal, a missing answer or an unexpected type is unusable, never a loss.
    """
    answer = (answers or {}).get(DECISION_QUESTION) or {}
    if answer.get("type") != "choice" or str(answer.get("choice")) not in ("1", "2"):
        return None
    verdict = int(answer["choice"])
    probs = answer.get("probabilities") or {}
    try:
        p1, p2 = float(probs.get("1", 0)), float(probs.get("2", 0))
        p_model = p1 / (p1 + p2) if p1 + p2 > 0 else None
    except (TypeError, ValueError):
        p_model = None
    if probabilistic and p_model is not None:
        p_first = min(P_MAX, max(P_MIN, p_model))
    else:
        p_first = 1.0 if verdict == 1 else 0.0
    p_a = p_first if order == "AB" else 1.0 - p_first
    return {
        "order": order,
        "verdict": verdict,
        "confidence": None,
        "p_logprob": None,
        "p_decision": None if p_model is None else round(p_model, 4),
        "p_a": round(p_a, 4),
    }
