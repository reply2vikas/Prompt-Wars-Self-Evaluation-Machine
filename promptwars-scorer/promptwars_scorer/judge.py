"""LLM-as-judge over the gathered evidence.

Model is configurable for routing/cost control (the playbook cares about token
asymmetry). Default judge model comes from PROMPTWARS_JUDGE_MODEL. The judge
call is isolated behind `judge()` so tests can inject a fake and never touch the
network or spend a key.
"""

from __future__ import annotations

import json
import os
from typing import Callable

from .rubric import PARAMETERS, ParamScore, build_judge_prompt, JUDGE_SYSTEM

# Fable 5 is the default judge (frontier coding capability). Override via env for
# cheaper routing on iteration passes, e.g. PROMPTWARS_JUDGE_MODEL=claude-sonnet-5.
DEFAULT_MODEL = os.environ.get("PROMPTWARS_JUDGE_MODEL", "claude-fable-5")

# A caller can pass their own function (challenge, evidence, sample, model) -> raw JSON str.
JudgeFn = Callable[[str, str, str, str], str]


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
        out[name] = ParamScore(
            name=name,
            score=score,
            rationale=str(entry.get("rationale", "")).strip(),
            fixes=[str(f) for f in entry.get("fixes", [])][:3],
        )
    return out


def anthropic_judge(challenge: str, evidence: str, sample: str, model: str) -> str:
    """Real judge call. Requires `anthropic` and ANTHROPIC_API_KEY."""
    from anthropic import Anthropic  # imported lazily so tests don't need it

    client = Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=1500,
        system=JUDGE_SYSTEM,
        messages=[{
            "role": "user",
            "content": build_judge_prompt(challenge, evidence, sample),
        }],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def judge(
    challenge: str,
    evidence: str,
    sample: str,
    model: str = DEFAULT_MODEL,
    judge_fn: JudgeFn = anthropic_judge,
) -> dict[str, ParamScore]:
    raw = judge_fn(challenge, evidence, sample, model)
    return parse_judgement(raw)
