"""PromptWars self-scorer: a proxy for the six-parameter hackathon grader."""

from .evidence import Evidence, gather, static_flags
from .judge import judge, parse_judgement, anthropic_judge
from .rubric import PARAMETERS, WEIGHTS, ParamScore, ScoreCard
from .scorer import DEFAULT_CHALLENGE, score_repo
from .submission import Submission

__all__ = [
    "Evidence", "gather", "static_flags",
    "judge", "parse_judgement", "anthropic_judge",
    "PARAMETERS", "WEIGHTS", "ParamScore", "ScoreCard",
    "DEFAULT_CHALLENGE", "score_repo", "Submission",
]
__version__ = "0.1.0"
