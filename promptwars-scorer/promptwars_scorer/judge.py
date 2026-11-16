"""LLM-as-judge over the gathered evidence.

Model is configurable for routing/cost control (the playbook cares about token
asymmetry). Default judge model comes from PROMPTWARS_JUDGE_MODEL. The judge
call is isolated behind `judge()` so tests can inject a fake and never touch the
network or spend a key.
"""

from __future__ import annotations

import json
import os
import statistics
from typing import Callable

from .rubric import PARAMETERS, ParamScore, build_judge_prompt, JUDGE_SYSTEM

# Fable 5 is the default judge (frontier coding capability). Override via env for
# cheaper routing on iteration passes, e.g. PROMPTWARS_JUDGE_MODEL=claude-sonnet-5.
DEFAULT_MODEL = os.environ.get("PROMPTWARS_JUDGE_MODEL", "claude-fable-5")

# A caller can pass their own function (challenge, evidence, sample, model) -> raw JSON str.
# `evidence` may be a plain summary string or Evidence.judge_context() (a dict).
JudgeFn = Callable[[str, object, str, str], str]


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    return raw.strip()


def parse_judgement(raw: str) -> dict[str, ParamScore]:
    """Parse the judge's JSON into ParamScore objects, defensively."""
    data = json.loads(_strip_fences(raw))
    params = data.get("parameters", data)
    out: dict[str, ParamScore] = {}
    for name in PARAMETERS:
        entry = params.get(name, {})
        score = int(round(float(entry.get("score", 0))))
        score = max(0, min(100, score))
        try:
            confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        cites = entry.get("evidence", [])
        cites = [e for e in cites if isinstance(e, dict)][:3] if isinstance(cites, list) else []
        out[name] = ParamScore(
            name=name,
            score=score,
            rationale=str(entry.get("rationale", "")).strip(),
            fixes=[str(f) for f in entry.get("fixes", [])][:3],
            confidence=confidence,
            evidence=cites,
        )
    return out


def anthropic_judge(challenge: str, evidence, sample: str, model: str) -> str:
    """Real judge call. Requires `anthropic` and ANTHROPIC_API_KEY.
    `evidence` may be a dict (Evidence.judge_context()) or a summary string."""
    from anthropic import Anthropic  # imported lazily so tests don't need it

    client = Anthropic()
    kwargs = dict(
        model=model,
        max_tokens=2000,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": build_judge_prompt(challenge, evidence, sample),
        }],
    )
    try:
        msg = client.messages.create(temperature=0, **kwargs)
    except Exception as exc:  # some models reject sampling params; retry without
        if "temperature" not in str(exc).lower():
            raise
        msg = client.messages.create(**kwargs)
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def judge(
    challenge: str,
    evidence,
    sample: str,
    model: str = DEFAULT_MODEL,
    judge_fn: JudgeFn = anthropic_judge,
    runs: int = 1,
) -> dict[str, ParamScore]:
    """Score once, or `runs` times and take the per-parameter MEDIAN.

    An LLM judge is noisy; the median of 3 removes most single-run jitter. The
    rationale/fixes/citations come from the run closest to the median, and the
    max-min spread is kept on each ParamScore as `spread` for the UI.
    """
    runs = max(1, int(runs))
    results = [parse_judgement(judge_fn(challenge, evidence, sample, model)) for _ in range(runs)]
    if runs == 1:
        return results[0]
    out: dict[str, ParamScore] = {}
    for name in PARAMETERS:
        scores = [r[name].score for r in results]
        med = int(round(statistics.median(scores)))
        best = min(results, key=lambda r: abs(r[name].score - med))[name]
        best.score = med
        best.confidence = round(statistics.median(r[name].confidence for r in results), 2)
        best.spread = max(scores) - min(scores)
        out[name] = best
    return out
