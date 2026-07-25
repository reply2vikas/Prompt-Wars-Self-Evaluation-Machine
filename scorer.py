"""Orchestrates a full score: evidence -> judge -> hard static caps -> ScoreCard.

The static caps encode rules the LLM shouldn't be trusted to enforce on its own:
a leaked secret caps Security, missing GenAI caps Problem Alignment (DQ risk),
and an oversized repo is flagged. This keeps the judge advisory while the
deterministic rules stay authoritative — the same split the playbook recommends
(the per-parameter breakdown is ground truth; opinion is secondary).
"""

from __future__ import annotations

from .evidence import gather, static_flags
from .judge import DEFAULT_MODEL, JudgeFn, anthropic_judge, judge
from .rubric import ScoreCard

DEFAULT_CHALLENGE = (
    "PromptWars 4 — Smart Stadiums & Tournament Operations. Build a "
    "GenAI-enabled solution that enhances stadium operations and the tournament "
    "experience for fans, organizers, volunteers, or venue staff during the "
    "FIFA World Cup 2026, across navigation, crowd management, accessibility, "
    "transportation, sustainability, multilingual assistance, operational "
    "intelligence, or real-time decision support. Pick one persona and go deep; "
    "GenAI must genuinely reason/generate, not stand in for rule-based logic."
)


def score_repo(
    root: str,
    challenge: str = DEFAULT_CHALLENGE,
    model: str = DEFAULT_MODEL,
    judge_fn: JudgeFn = anthropic_judge,
) -> ScoreCard:
    ev = gather(root)
    params = judge(challenge, ev.summary(), ev.source_sample, model, judge_fn)
    flags = static_flags(ev)

    # Deterministic caps override the judge where rules are hard.
    if ev.secrets:
        params["security"].score = min(params["security"].score, 40)
        params["security"].fixes.insert(0, "Remove hardcoded secrets; load from env / secrets store.")
    if not ev.genai_signals:
        params["problem_alignment"].score = min(params["problem_alignment"].score, 30)
        params["problem_alignment"].fixes.insert(0, "Add genuine, functional GenAI — mandatory or DQ.")

    return ScoreCard(
        params=params,
        flags=flags,
        evidence_summary=ev.summary(),
        model=model,
    )
