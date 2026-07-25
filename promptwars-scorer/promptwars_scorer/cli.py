"""Command-line entry point.

    promptwars-score ./my-repo
    promptwars-score ./my-repo --challenge challenge.md --gate 90 --json
    promptwars-score ./my-repo --model claude-haiku-4-5   # cheaper routing

Exits nonzero when the composite is below --gate, so it works as a CI hard gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .rubric import PARAMETERS, WEIGHTS
from .scorer import DEFAULT_CHALLENGE, score_repo
from .judge import DEFAULT_MODEL

_BARW = 24


def _bar(score: int) -> str:
    filled = round(score / 100 * _BARW)
    return "█" * filled + "·" * (_BARW - filled)


def _render(card) -> str:
    lines = [f"\nPromptWars self-score   composite: {card.composite}/100   (model: {card.model})", "=" * 62]
    for p in sorted(PARAMETERS, key=lambda x: -WEIGHTS[x]):
        ps = card.params[p]
        w = {3: "High", 2: "Med", 1: "Low"}[WEIGHTS[p]]
        lines.append(f"{p:<18} {ps.score:>3}  {_bar(ps.score)}  [{w}]")
        if ps.rationale:
            lines.append(f"    → {ps.rationale}")
        for fix in ps.fixes:
            lines.append(f"    · fix: {fix}")
    sub = {k: v for k, v in (card.submission or {}).items() if v}
    if sub:
        lines.append("\nSUBMISSION ARTIFACTS:")
        for k, v in sub.items():
            lines.append(f"  {k.replace('_', ' ')}: {v}")
    if card.flags:
        lines.append("\nFLAGS:")
        lines.extend(f"  ⚠ {f}" for f in card.flags)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="promptwars-score")
    ap.add_argument("repo", help="path to the submission repo")
    ap.add_argument("--challenge", help="path to a challenge/problem-statement file")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="judge model (routing)")
    ap.add_argument("--gate", type=float, default=0.0,
                    help="fail (exit 1) if composite < this value")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    challenge = DEFAULT_CHALLENGE
    if args.challenge:
        challenge = Path(args.challenge).read_text()

    card = score_repo(args.repo, challenge=challenge, model=args.model)

    if args.json:
        print(json.dumps(card.as_dict(), indent=2))
    else:
        print(_render(card))

    if card.composite < args.gate:
        print(f"\nGATE FAILED: {card.composite} < {args.gate}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
