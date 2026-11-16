"""Command-line entry point.

Score a repo (local path or public GitHub URL):
    promptwars-score ./my-repo --challenge challenge.md
    promptwars-score https://github.com/owner/repo --challenge challenge.md --runs 3
    promptwars-score ./my-repo --gate 90 --json          # CI hard gate

Teach it from real results (the accuracy layer):
    promptwars-score record add https://github.com/me/repo --edition "PW C3" \\
        --challenge c3.md --composite 97.4 --code-quality 89 --security 99 ...
    promptwars-score record import my_scores.csv
    promptwars-score record list
    promptwars-score evaluate --runs 3     # run the engine on every labeled repo
    promptwars-score calibrate             # fit + validate; switches itself off if it doesn't help

Exits nonzero when the composite is below --gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from . import calibration, records as rec_store
from .evidence import gather
from .judge import DEFAULT_MODEL
from .rubric import ENGINE_VERSION, PARAMETERS, WEIGHTS
from .scorer import DEFAULT_CHALLENGE, score_repo

_BARW = 24
_SUBCOMMANDS = {"record", "evaluate", "calibrate"}


def _bar(score: int) -> str:
    filled = round(score / 100 * _BARW)
    return "█" * filled + "·" * (_BARW - filled)


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@")) or s.startswith("github.com/")


@contextmanager
def _checkout(target: str):
    """Yield a local path for a path or a GitHub URL (cloned to a sandbox)."""
    if _is_url(target):
        from .ingest import sandbox_clone
        with sandbox_clone(target) as path:
            yield str(path)
    else:
        yield target


def _render(card) -> str:
    cal = (card.calibration or {}).get("composite", {})
    head = f"\nPromptWars self-score   engine composite: {card.composite}/100   (model: {card.model}"
    head += f", median of {card.judge_runs} runs)" if card.judge_runs > 1 else ")"
    lines = [head, "=" * 70]
    if cal.get("estimate") is not None:
        lines.append(f"Estimated real score: {cal['estimate']} ± {cal['band']}  "
                     f"[{cal['status']}, n={cal['n']}, held-out error {cal['loo_mae']}]")
    else:
        lines.append(f"Estimated real score: not available ({cal.get('status', 'not_fitted')})")
    if card.anchors_used:
        lines.append(f"Judge anchored on {len(card.anchors_used)} real graded submission(s).")
    for p in sorted(PARAMETERS, key=lambda x: -WEIGHTS[x]):
        ps = card.params[p]
        w = {3: "High", 2: "Med", 1: "Low"}[WEIGHTS[p]]
        cov = card.coverage.get(p, {})
        extra = f"  cov {cov.get('found_count', 0)}/{cov.get('expected_count', 0)}"
        if ps.spread:
            extra += f"  ±{ps.spread} across runs"
        lines.append(f"{p:<18} {ps.score:>3}  {_bar(ps.score)}  [{w}]{extra}")
        if ps.rationale:
            lines.append(f"    → {ps.rationale}")
        for fix in ps.fixes:
            lines.append(f"    · fix: {fix}")
        if cov.get("missing"):
            lines.append(f"    · not observed: {', '.join(cov['missing'])}")
    sub = {k: v for k, v in (card.submission or {}).items() if v}
    if sub:
        lines.append("\nSUBMISSION ARTIFACTS:")
        for k, v in sub.items():
            lines.append(f"  {k.replace('_', ' ')}: {v}")
    if card.flags:
        lines.append("\nFLAGS:")
        lines.extend(f"  ⚠ {f}" for f in card.flags)
    lines.append("\nProxy only — not Hack2Skill's grader. Trust the fixes more than the number.")
    return "\n".join(lines)


# ---- score -------------------------------------------------------------------

def _cmd_score(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="promptwars-score")
    ap.add_argument("repo", help="path to the submission repo, or a public GitHub URL")
    ap.add_argument("--challenge", help="path to a challenge/problem-statement file")
    ap.add_argument("--profile", help="challenge profile for evidence expectations, e.g. legal_ai")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="judge model (routing)")
    ap.add_argument("--runs", type=int, default=1, help="judge passes; the median is used (try 3)")
    ap.add_argument("--no-anchors", action="store_true", help="do not show labeled records to the judge")
    ap.add_argument("--genai", default="", help="declared GenAI services and where they are used")
    ap.add_argument("--gate", type=float, default=0.0, help="fail (exit 1) if composite < this value")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    challenge = Path(args.challenge).read_text() if args.challenge else DEFAULT_CHALLENGE
    with _checkout(args.repo) as path:
        card = score_repo(path, challenge=challenge, model=args.model, challenge_profile=args.profile,
                          runs=args.runs, use_anchors=not args.no_anchors,
                          repo_url=args.repo if _is_url(args.repo) else "",
                          genai_services=args.genai)

    print(json.dumps(card.as_dict(), indent=2) if args.json else _render(card))
    if card.composite < args.gate:
        print(f"\nGATE FAILED: {card.composite} < {args.gate}", file=sys.stderr)
        return 1
    return 0


# ---- record ------------------------------------------------------------------

def _add_record(repo: str, h2s: dict, *, edition: str, challenge: str, profile: str | None,
                attempt: int | None, source: str, notes: str) -> dict:
    with _checkout(repo) as path:
        prof = rec_store.profile_from_evidence(gather(path, challenge_profile=profile))
    return rec_store.upsert(repo, h2s, edition=edition, challenge=challenge, challenge_profile=profile,
                            attempt=attempt, source=source, notes=notes, profile=prof)


def _cmd_record(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="promptwars-score record")
    sp = ap.add_subparsers(dest="action", required=True)

    add = sp.add_parser("add", help="save one submission's real scores")
    add.add_argument("repo", help="public GitHub URL (or local path)")
    add.add_argument("--edition", default="", help='e.g. "PromptWars C3"')
    add.add_argument("--challenge", help="path to that edition's problem-statement file")
    add.add_argument("--profile")
    add.add_argument("--attempt", type=int)
    add.add_argument("--source", default="own", choices=["own", "public"])
    add.add_argument("--notes", default="")
    add.add_argument("--composite", type=float)
    for p in PARAMETERS:
        add.add_argument(f"--{p.replace('_', '-')}", type=float, dest=p)

    imp = sp.add_parser("import", help="bulk-load records from a CSV")
    imp.add_argument("csv", help="columns: repo_url, edition, attempt, composite, "
                                 + ", ".join(PARAMETERS) + ", challenge_file, profile, source, notes")
    sp.add_parser("list", help="show labeled records")
    args = ap.parse_args(argv)

    if args.action == "list":
        recs = rec_store.load()
        if not recs:
            print("No labeled records yet. Add one with: promptwars-score record add <repo> --composite ...")
            return 0
        for r in recs:
            scores = "  ".join(f"{k}={v:g}" for k, v in r["h2s"].items())
            eng = f"  | engine {r['engine']['composite']}" if r.get("engine") else ""
            print(f"{rec_store.normalize_repo(r['repo_url']):<40} {r.get('edition', ''):<18} {scores}{eng}")
        return 0

    if args.action == "add":
        h2s = {k: getattr(args, k) for k in rec_store.SCORE_KEYS}
        challenge = Path(args.challenge).read_text() if args.challenge else ""
        rec = _add_record(args.repo, h2s, edition=args.edition, challenge=challenge, profile=args.profile,
                          attempt=args.attempt, source=args.source, notes=args.notes)
        print(f"Saved {rec['id']}  real scores: {rec['h2s']}")
        return 0

    ok = failed = 0
    base = Path(args.csv).parent
    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            repo = (row.get("repo_url") or "").strip()
            if not repo:
                continue
            cf = (row.get("challenge_file") or "").strip()
            try:
                _add_record(repo, row, edition=row.get("edition", ""),
                            challenge=(base / cf).read_text() if cf else "",
                            profile=(row.get("profile") or "").strip() or None,
                            attempt=int(row["attempt"]) if (row.get("attempt") or "").strip() else None,
                            source=(row.get("source") or "public").strip(), notes=row.get("notes", ""))
                ok += 1
                print(f"  saved   {repo}")
            except Exception as e:  # keep going: one private repo must not sink the batch
                failed += 1
                print(f"  SKIPPED {repo}: {e}")
    print(f"\n{ok} saved, {failed} skipped.")
    return 0


# ---- evaluate / calibrate ----------------------------------------------------

def _cmd_evaluate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="promptwars-score evaluate",
                                 description="Run the engine on every labeled repo (needs ANTHROPIC_API_KEY).")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--only-missing", action="store_true", help="skip records already evaluated")
    args = ap.parse_args(argv)

    recs = rec_store.load()
    if not recs:
        print("No labeled records. Add some first.")
        return 1
    for r in recs:
        if args.only_missing and (r.get("engine") or {}).get("engine_version") == ENGINE_VERSION:
            continue
        name = rec_store.normalize_repo(r["repo_url"])
        challenge = r.get("challenge") or DEFAULT_CHALLENGE
        if not r.get("challenge"):
            print(f"  note: {name} has no challenge text; Problem Alignment will be unreliable.")
        try:
            with _checkout(r["repo_url"]) as path:
                # Anchors exclude the repo itself, so this is a fair held-out read.
                card = score_repo(path, challenge=challenge, model=args.model,
                                  challenge_profile=r.get("challenge_profile"), runs=args.runs,
                                  repo_url=r["repo_url"], edition=r.get("edition", ""), calibrate=False)
        except Exception as e:
            print(f"  FAILED  {name}: {e}")
            continue
        rec_store.set_engine(r["id"], {
            "composite": card.composite,
            "params": {p: card.params[p].score for p in PARAMETERS},
            "model": args.model, "runs": args.runs, "engine_version": ENGINE_VERSION,
        })
        real = r["h2s"].get("composite")
        print(f"  {name:<40} engine {card.composite:>5}   real {real if real is not None else '—'}")
    print("\nNow run: promptwars-score calibrate")
    return 0


def _cmd_calibrate(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="promptwars-score calibrate").parse_args(argv)
    model = calibration.fit_all()
    print(f"Calibration {model['version']}: {model['evaluated_records']} evaluated of "
          f"{model['labeled_records']} labeled record(s)\n")
    print(f"{'key':<18} {'n':>3}  {'status':<21} {'raw err':>8} {'avg-guess':>10} {'held-out':>9}  rank-corr")
    for k, v in model["keys"].items():
        print(f"{k:<18} {v['n']:>3}  {v.get('status', ''):<21} {str(v.get('raw_mae', '—')):>8} "
              f"{str(v.get('mean_baseline_mae', '—')):>10} {str(v.get('loo_mae', '—')):>9}  {v.get('spearman', '—')}")
    on = [k for k, v in model["keys"].items() if v["enabled"]]
    print(f"\nActive for: {', '.join(on) if on else 'nothing — the engine reports raw scores only'}")
    print("A key is switched on only when it beats both the raw score and the average-guess on held-out data.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _SUBCOMMANDS:
        return {"record": _cmd_record, "evaluate": _cmd_evaluate, "calibrate": _cmd_calibrate}[argv[0]](argv[1:])
    return _cmd_score(argv)


if __name__ == "__main__":
    raise SystemExit(main())
