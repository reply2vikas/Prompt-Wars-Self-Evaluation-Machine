"""Labeled records: submissions whose REAL Hack2Skill scores are known.

This is the project's ground truth and the only thing the accuracy layer learns
from. Two uses:

1. Anchors (few-shot calibration of the judge). A handful of records are shown
   to the judge as "this submission got Code Quality 89 from the real grader",
   so it scores on the grader's scale rather than its own.
2. Calibration (see calibration.py). Engine score vs real score pairs are used
   to fit a small monotone correction, validated leave-one-out.

A record stores a compact deterministic PROFILE of the repo (evidence summary +
coverage), never its source code, so the store stays small and holds nothing
sensitive. Storage is one JSON file: $PROMPTWARS_DATA_DIR/labeled_records.json
(default ./data).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .rubric import PARAMETERS

SCORE_KEYS = ("composite",) + PARAMETERS
MAX_ANCHORS = 4


def data_dir() -> Path:
    d = Path(os.environ.get("PROMPTWARS_DATA_DIR", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return data_dir() / "labeled_records.json"


def normalize_repo(url: str) -> str:
    """owner/repo in lowercase, so the same repo always matches itself."""
    u = (url or "").strip().lower()
    u = re.sub(r"^(https?://)?(www\.)?github\.com/", "", u)
    u = re.sub(r"\.git$", "", u).strip("/")
    parts = u.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else u


def load() -> list[dict]:
    p = _path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return []
    return data.get("records", []) if isinstance(data, dict) else data


def save(records: list[dict]) -> None:
    _path().write_text(json.dumps({"schema": 1, "records": records}, indent=2) + "\n")


def clean_scores(h2s: dict) -> dict:
    """Keep only known keys with numeric 0-100 values."""
    out: dict[str, float] = {}
    for k in SCORE_KEYS:
        v = (h2s or {}).get(k)
        if v in (None, ""):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0 <= f <= 100:
            out[k] = round(f, 2)
    return out


def profile_from_evidence(ev) -> dict:
    """Compact, source-free description of a repo, used when it is an anchor."""
    return {
        "summary": ev.summary(),
        "coverage_pct": {p: ev.coverage.get(p, {}).get("coverage_pct") for p in PARAMETERS},
        "readme_pct": ev.readme_map.get("coverage_pct"),
        "file_count": ev.file_count,
        "size_mb": ev.size_mb,
    }


def upsert(
    repo_url: str,
    h2s: dict,
    *,
    edition: str = "",
    challenge: str = "",
    challenge_profile: str | None = None,
    attempt: int | None = None,
    source: str = "own",
    notes: str = "",
    profile: dict | None = None,
) -> dict:
    """Add or update a record. Identity = repo + edition + attempt."""
    scores = clean_scores(h2s)
    if not scores:
        raise ValueError("At least one real score (composite or a parameter) is required.")
    records = load()
    rid = f"{normalize_repo(repo_url)}|{edition.strip().lower()}|{attempt or 0}"
    rec = next((r for r in records if r.get("id") == rid), None)
    if rec is None:
        rec = {"id": rid, "created": time.strftime("%Y-%m-%d")}
        records.append(rec)
    rec.update({
        "repo_url": repo_url.strip(),
        "edition": edition.strip(),
        "challenge": challenge.strip(),
        "challenge_profile": challenge_profile,
        "attempt": attempt,
        "source": source,
        "notes": notes.strip(),
        "h2s": scores,
    })
    if profile is not None:
        rec["profile"] = profile
    save(records)
    return rec


def set_engine(rid: str, engine: dict) -> None:
    records = load()
    for r in records:
        if r.get("id") == rid:
            r["engine"] = engine
    save(records)


def select_anchors(records: list[dict], *, exclude_repo: str = "", edition: str = "",
                   k: int = MAX_ANCHORS) -> list[dict]:
    """Pick up to k anchors: profiled, not the repo being scored, spread across
    the score range, preferring the same edition and records with a
    per-parameter breakdown (those teach the judge far more than a composite)."""
    ex = normalize_repo(exclude_repo)
    pool = [r for r in records
            if r.get("profile") and r.get("h2s") and normalize_repo(r.get("repo_url", "")) != ex]
    if not pool:
        return []

    def level(r):
        h = r["h2s"]
        return h.get("composite", sum(h.values()) / len(h))

    def richness(r):
        return (len([p for p in PARAMETERS if p in r["h2s"]]),
                1 if edition and r.get("edition", "").lower() == edition.lower() else 0)

    pool.sort(key=lambda r: (richness(r), level(r)), reverse=True)
    pool = pool[: max(k * 3, k)]
    pool.sort(key=level)
    if len(pool) <= k:
        return pool
    # evenly spaced across the sorted score range, always including both ends
    idx = sorted({round(i * (len(pool) - 1) / (k - 1)) for i in range(k)})
    return [pool[i] for i in idx]


def render_anchors(anchors: list[dict]) -> str:
    """Judge-ready block. Empty string when there are no anchors."""
    if not anchors:
        return ""
    lines = [
        "REFERENCE SUBMISSIONS — scores below were given by the REAL hackathon grader.",
        "Use them to set your scale: score the current submission relative to these,",
        "parameter by parameter. They are different projects; compare evidence, not topic.",
    ]
    for i, r in enumerate(anchors, 1):
        h, pr = r["h2s"], r["profile"]
        scores = ", ".join(f"{k}={h[k]:g}" for k in SCORE_KEYS if k in h)
        cov = ", ".join(f"{p}={v:g}%" for p, v in (pr.get("coverage_pct") or {}).items() if v is not None)
        lines.append(f"[{i}] real scores: {scores}")
        lines.append(f"    evidence coverage: {cov}; README map {pr.get('readme_pct')}%")
        lines.append(f"    profile: {pr.get('summary', '')}")
        if r.get("notes"):
            lines.append(f"    note: {r['notes']}")
    return "\n".join(lines)
