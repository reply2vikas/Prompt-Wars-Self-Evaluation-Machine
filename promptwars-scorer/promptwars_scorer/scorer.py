"""Orchestrates a full score: evidence -> judge -> hard static caps -> ScoreCard.

The static caps encode rules the LLM shouldn't be trusted to enforce on its own:
a leaked secret caps Security, missing GenAI caps Problem Alignment (DQ risk),
and an oversized repo is flagged. Submission artifacts (prototype brief, LinkedIn
post) feed the judge's Problem Alignment context, and missing artifacts (live
link, LinkedIn post, demo video) become flags — the same "breakdown is ground
truth, opinion is secondary" split the playbook recommends.
"""

from __future__ import annotations

from .evidence import gather, static_flags
from .judge import DEFAULT_MODEL, JudgeFn, anthropic_judge, judge
from .rubric import ScoreCard
from .submission import Submission

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
    submission: Submission | None = None,
) -> ScoreCard:
    ev = gather(root)
    sub = submission if submission is not None else Submission.load(root)

    judge_challenge = challenge + sub.judge_context()
    params = judge(judge_challenge, ev.summary(), ev.source_sample, model, judge_fn)

    flags = static_flags(ev) + sub.flags()

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
        submission=sub.as_dict(),
    )
