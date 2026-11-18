"""Orchestrates a full score: evidence -> anchored judge -> hard caps -> calibration.

The static caps encode rules the LLM shouldn't be trusted to enforce on its own:
a leaked secret caps Security, missing GenAI caps Problem Alignment (DQ risk),
and an oversized repo is flagged. Submission artifacts feed the judge's Problem
Alignment context, and missing artifacts become flags.

Accuracy layer (all optional, all driven by labeled records — see records.py):
  * anchors: real graded submissions shown to the judge to set its scale
  * runs:    median of N judge passes to cut noise
  * calibration: a validated small-data correction, applied only when it has
    proven itself out of sample; otherwise it abstains.
"""

from __future__ import annotations

import warnings

from . import calibration, records as rec_store
from .evidence import gather, static_flags
from .judge import DEFAULT_MODEL, JudgeFn, anthropic_judge, judge
from .rubric import ENGINE_VERSION, PARAMETERS, RUBRIC_VERSION, WEIGHTS, ScoreCard
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


def _context(sub: Submission, genai_services: str, changes: str) -> str:
    extra = sub.judge_context()
    if genai_services.strip():
        extra += ("\n\n# Declared GenAI services (author's claim — verify against the evidence)\n"
                  + genai_services.strip())
    if changes.strip():
        extra += "\n\n# Declared changes in the deployed version (author's claim)\n" + changes.strip()
    return extra


def scan_repo(root: str, challenge_profile: str | None = None,
              submission: Submission | None = None) -> dict:
    """Deterministic-only result, in the same shape as ScoreCard.as_dict() with
    every judge-derived field left null. Needs no API key."""
    ev = gather(root, challenge_profile=challenge_profile)
    sub = submission if submission is not None else Submission.load(root)
    return {
        "composite": None,
        "calibrated_score": None,
        "calibration": {},
        "anchors_used": [],
        "judge_runs": 0,
        "score_spread": {},
        "model": None,
        "flags": static_flags(ev) + sub.flags(),
        "submission": sub.as_dict(),
        "coverage": ev.coverage,
        "readme_map": ev.readme_map,
        "sampled_files": ev.sampled_files,
        "provenance": {"engine_version": ENGINE_VERSION, "rubric_version": RUBRIC_VERSION,
                       "judge_model": None},
        "parameters": {p: {"score": None, "weight": WEIGHTS[p], "confidence": None,
                           "rationale": None, "fixes": [], "evidence": []} for p in PARAMETERS},
        "judge_status": "deterministic_only",
    }


def score_repo(
    root: str,
    challenge: str = DEFAULT_CHALLENGE,
    model: str = DEFAULT_MODEL,
    judge_fn: JudgeFn = anthropic_judge,
    submission: Submission | None = None,
    challenge_profile: str | None = None,
    *,
    runs: int = 1,
    use_anchors: bool = True,
    calibrate: bool = True,
    repo_url: str = "",
    edition: str = "",
    genai_services: str = "",
    changes: str = "",
) -> ScoreCard:
    if challenge is DEFAULT_CHALLENGE:
        warnings.warn(
            "score_repo is using the built-in default challenge; pass challenge=... "
            "for reliable Problem Alignment (critical when benchmarking real submissions).",
            stacklevel=2,
        )
    ev = gather(root, challenge_profile=challenge_profile)
    sub = submission if submission is not None else Submission.load(root)

    # Anchors: never include the repo being scored (that would leak its own label).
    anchors = (rec_store.select_anchors(rec_store.load(), exclude_repo=repo_url, edition=edition)
               if use_anchors else [])
    context = ev.judge_context()
    context["anchors"] = rec_store.render_anchors(anchors)

    judge_challenge = challenge + _context(sub, genai_services, changes)
    params = judge(judge_challenge, context, ev.source_sample, model, judge_fn, runs=runs)

    flags = static_flags(ev) + sub.flags()

    # Deterministic caps override the judge where rules are hard.
    if ev.secrets:
        params["security"].score = min(params["security"].score, 40)
        params["security"].fixes.insert(0, "Remove hardcoded secrets; load from env / secrets store.")
    if not ev.genai_signals:
        params["problem_alignment"].score = min(params["problem_alignment"].score, 30)
        params["problem_alignment"].fixes.insert(0, "Add genuine, functional GenAI — mandatory or DQ.")

    card = ScoreCard(
        params=params,
        flags=flags,
        evidence_summary=ev.summary(),
        model=model,
        submission=sub.as_dict(),
        coverage=ev.coverage,
        readme_map=ev.readme_map,
        sampled_files=ev.sampled_files,
        anchors_used=[{"repo": rec_store.normalize_repo(a.get("repo_url", "")),
                       "edition": a.get("edition", ""), "h2s": a["h2s"]} for a in anchors],
        judge_runs=max(1, int(runs)),
        score_spread={p: params[p].spread for p in PARAMETERS},
    )
    if calibrate:
        cal = calibration.load_model()
        card.calibration = {"composite": calibration.apply(cal, "composite", card.composite)}
        for p in PARAMETERS:
            card.calibration[p] = calibration.apply(cal, p, params[p].score)
        card.calibrated = card.calibration["composite"]["estimate"]
    return card
