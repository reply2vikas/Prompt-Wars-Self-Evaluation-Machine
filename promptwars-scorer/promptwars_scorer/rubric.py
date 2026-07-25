"""The six PromptWars judging parameters, their relative weights, and the
LLM-as-judge prompt.

Weights are derived from observed behaviour across Challenge 3 (see
PROMPTWARS_PLAYBOOK): Code Quality and Problem Alignment moved rank the most and
are treated as High; Security and Efficiency as Medium; Testing and
Accessibility as Low. These are a *proxy* for Google's real grader, whose exact
weights are not published. Tune WEIGHTS if you gather better ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- The six parameters -------------------------------------------------------

PARAMETERS: tuple[str, ...] = (
    "code_quality",
    "security",
    "efficiency",
    "testing",
    "accessibility",
    "problem_alignment",
)

# Composite weights. High=3, Medium=2, Low=1 (normalised at compute time).
WEIGHTS: dict[str, int] = {
    "code_quality": 3,
    "problem_alignment": 3,
    "security": 2,
    "efficiency": 2,
    "testing": 1,
    "accessibility": 1,
}

# What each parameter rewards, written so the judge scores the same thing the
# playbook says the real grader scores. Key nuance for code_quality: genuine
# scope/depth, NOT comment density or late modularity, and padding is penalised.
PARAM_GUIDANCE: dict[str, str] = {
    "code_quality": (
        "Reward genuine engineering depth: real domain modelling, sound "
        "patterns used for real reasons, a well-structured multi-view app. "
        "Do NOT reward comment density, docstrings, or late modularity on a "
        "shallow app. Explicitly penalise padding (dead code, duplicated "
        "boilerplate, lines added for volume). A tidy but thin single-page app "
        "should top out in the high 80s; reserve 90+ for real scope and depth."
    ),
    "security": (
        "Reward input validation, security headers, dependency auditing "
        "(pip-audit/npm audit), pinned patched versions, and correct secret "
        "handling. Any hardcoded key or secret is a hard fail for this param."
    ),
    "efficiency": (
        "Reward small bundle size, fast cold start, sensible algorithmic "
        "choices, and cost-aware GenAI usage (model routing, not always the "
        "biggest model). Penalise obvious O(n^2) hot paths and bloat."
    ),
    "testing": (
        "Reward tests co-located per unit, meaningful coverage, and especially "
        "EDGE-CASE tests. A submission with only happy-path tests loses points "
        "here even at high line coverage."
    ),
    "accessibility": (
        "Reward semantic HTML, ARIA where needed, alt text, keyboard focus, "
        "and automated a11y checks (jsx-a11y, axe). Penalise div-soup UIs."
    ),
    "problem_alignment": (
        "Reward a clear persona and a real before/after story for that person. "
        "GenAI must genuinely matter (reasoning/generation, not rule-based code "
        "in disguise). Penalise touching many verticals shallowly instead of "
        "going deep on one. Missing/fake GenAI is a hard fail for this param."
    ),
}


@dataclass
class ParamScore:
    name: str
    score: int  # 0-100
    rationale: str
    fixes: list[str] = field(default_factory=list)


@dataclass
class ScoreCard:
    params: dict[str, ParamScore]
    flags: list[str] = field(default_factory=list)
    evidence_summary: str = ""
    model: str = ""
    submission: dict = field(default_factory=dict)

    @property
    def composite(self) -> float:
        total_w = sum(WEIGHTS[p] for p in PARAMETERS)
        return round(
            sum(self.params[p].score * WEIGHTS[p] for p in PARAMETERS) / total_w, 1
        )

    def as_dict(self) -> dict:
        return {
            "composite": self.composite,
            "model": self.model,
            "flags": self.flags,
            "submission": self.submission,
            "parameters": {
                p: {
                    "score": self.params[p].score,
                    "weight": WEIGHTS[p],
                    "rationale": self.params[p].rationale,
                    "fixes": self.params[p].fixes,
                }
                for p in PARAMETERS
            },
        }


JUDGE_SYSTEM = (
    "You are a strict, calibrated code assessor for the PromptWars hackathon. "
    "You score a submission on six parameters, 0-100 each, using ONLY the "
    "evidence provided. You are conservative: 90+ means genuine engineering "
    "depth, not surface polish. You never invent facts not in the evidence; if "
    "evidence is thin, you say so and score cautiously. Output STRICT JSON only, "
    "no prose, no markdown fences."
)


def build_judge_prompt(challenge: str, evidence: str, source_sample: str) -> str:
    guidance = "\n".join(f"- {k}: {v}" for k, v in PARAM_GUIDANCE.items())
    schema = (
        '{"parameters": {"<param>": {"score": <int 0-100>, '
        '"rationale": "<=2 sentences", "fixes": ["<concrete, code-level fix>", '
        "...]}}}"
    )
    return (
        f"# Challenge problem statement\n{challenge}\n\n"
        f"# Scoring guidance per parameter\n{guidance}\n\n"
        f"# Static evidence gathered from the repo\n{evidence}\n\n"
        f"# Sampled source (truncated)\n{source_sample}\n\n"
        f"# Task\nScore these six parameters: {', '.join(PARAMETERS)}.\n"
        f"For each, give an integer 0-100, a <=2 sentence rationale grounded in "
        f"the evidence, and up to 3 concrete fixes that would raise the score. "
        f"Only propose fixes that reference something real in the evidence.\n\n"
        f"Return STRICT JSON exactly matching this schema (no fences):\n{schema}"
    )
