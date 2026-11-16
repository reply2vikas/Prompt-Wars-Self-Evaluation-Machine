"""FastAPI app: the H2S-style self-scorer page plus its JSON API.

    uvicorn promptwars_scorer.server:app --reload
    # open http://127.0.0.1:8000

Input is a public GitHub URL, cloned into a throwaway sandbox (ingest.py). Repo
code is never executed. Local paths are refused unless PROMPTWARS_ALLOW_LOCAL=1
(developer convenience only — never set it on a public host).

Without ANTHROPIC_API_KEY the app still works: it returns the deterministic
layer (coverage, flags, README map) and marks the judge fields as not scored.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import calibration, records as rec_store
from .evidence import gather
from .ingest import IngestError, probe_url, sandbox_clone
from .judge import DEFAULT_MODEL
from .rubric import ENGINE_VERSION, PARAMETERS
from .scorer import DEFAULT_CHALLENGE, scan_repo, score_repo
from .submission import Submission

app = FastAPI(title="PromptWars Self-Scorer", version=ENGINE_VERSION)
_WEB = Path(__file__).parent / "web" / "index.html"
MAX_RUNS = 3
MAX_TEXT = 6000

_RATE, _WINDOW = 6, 60
_hits: dict[str, deque] = defaultdict(deque)


def _limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > _WINDOW:
        q.popleft()
    if len(q) >= _RATE:
        raise HTTPException(status_code=429, detail="Too many requests. Try again in a minute.")
    q.append(now)


def _judge_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _challenges() -> list[dict]:
    out = [{"id": "pw4-smart-stadiums", "name": "PromptWars 4 — Smart Stadiums (built-in)",
            "statement": DEFAULT_CHALLENGE, "profile": None}]
    p = rec_store.data_dir() / "challenges.json"
    if p.is_file():
        try:
            out += [c for c in json.loads(p.read_text()) if c.get("id") and c.get("statement")]
        except ValueError:
            pass
    return out


class ScoreRequest(BaseModel):
    repo_url: str = ""
    repo_path: str = ""                      # only honoured with PROMPTWARS_ALLOW_LOCAL=1
    challenge_id: str = ""
    challenge_text: str = Field("", max_length=MAX_TEXT)
    challenge_profile: str | None = None
    edition: str = ""
    deployed_link: str = ""
    changes: str = Field("", max_length=2000)
    genai_services: str = Field("", max_length=2000)
    runs: int = 1
    model: str = DEFAULT_MODEL


class RecordRequest(BaseModel):
    repo_url: str
    h2s: dict
    edition: str = ""
    challenge_id: str = ""
    challenge_text: str = Field("", max_length=MAX_TEXT)
    challenge_profile: str | None = None
    attempt: int | None = None
    source: str = "own"
    notes: str = Field("", max_length=500)


def _resolve_challenge(cid: str, text: str) -> tuple[str, str | None]:
    if text.strip():
        return text.strip(), None
    for c in _challenges():
        if c["id"] == cid:
            return c["statement"], c.get("profile")
    raise HTTPException(status_code=422, detail="Choose a challenge or paste its problem statement.")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _WEB.read_text()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "judge_available": _judge_available(), "model": DEFAULT_MODEL,
            "engine_version": ENGINE_VERSION, "max_runs": MAX_RUNS}


@app.get("/api/challenges")
def challenges() -> list[dict]:
    return _challenges()


@app.post("/api/score")
def score(req: ScoreRequest, request: Request) -> dict:
    _limit(request)
    statement, profile = _resolve_challenge(req.challenge_id, req.challenge_text)
    profile = req.challenge_profile or profile
    sub = Submission(app_live_link=req.deployed_link.strip())
    runs = max(1, min(MAX_RUNS, req.runs))

    def run(path: str) -> dict:
        if not _judge_available():
            return scan_repo(path, profile, sub)
        card = score_repo(
            path, challenge=statement, model=req.model, submission=sub,
            challenge_profile=profile, runs=runs, repo_url=req.repo_url,
            edition=req.edition, genai_services=req.genai_services, changes=req.changes,
        )
        out = card.as_dict()
        out["judge_status"] = "scored"
        return out

    def tidy(result: dict) -> dict:
        # The web form does not collect a LinkedIn post or demo video, so those
        # reminders would be noise here (the CLI still raises them).
        result["flags"] = [f for f in result["flags"] if not f.startswith(("NO LINKEDIN", "NO DEMO VIDEO"))]
        return result

    try:
        if req.repo_path and os.environ.get("PROMPTWARS_ALLOW_LOCAL") == "1":
            root = Path(req.repo_path).expanduser()
            if not root.is_dir():
                raise HTTPException(status_code=400, detail=f"Not a directory: {root}")
            result = tidy(run(str(root)))
        else:
            with sandbox_clone(req.repo_url) as path:
                result = tidy(run(str(path)))
    except IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:  # judge/network failure: say so, never fabricate a score
        raise HTTPException(status_code=502, detail=f"Scoring failed: {type(e).__name__}")

    result["deployment_check"] = probe_url(req.deployed_link)
    result["repo"] = rec_store.normalize_repo(req.repo_url) or req.repo_path
    return result


@app.get("/api/records")
def list_records() -> dict:
    recs = rec_store.load()
    return {"count": len(recs), "records": [
        {k: r.get(k) for k in ("id", "repo_url", "edition", "attempt", "source", "h2s", "notes", "engine")}
        | {"profiled": bool(r.get("profile"))} for r in recs]}


@app.post("/api/records")
def add_record(req: RecordRequest, request: Request) -> dict:
    """Save a submission's REAL scores. Clones the repo once to store its
    evidence profile so it can serve as an anchor."""
    _limit(request)
    if not rec_store.clean_scores(req.h2s):
        raise HTTPException(status_code=422, detail="Enter at least one real score between 0 and 100.")
    statement, profile_name = "", req.challenge_profile
    if req.challenge_text.strip() or req.challenge_id:
        statement, p = _resolve_challenge(req.challenge_id, req.challenge_text)
        profile_name = profile_name or p
    try:
        with sandbox_clone(req.repo_url) as path:
            profile = rec_store.profile_from_evidence(gather(str(path), challenge_profile=profile_name))
    except IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rec = rec_store.upsert(req.repo_url, req.h2s, edition=req.edition, challenge=statement,
                           challenge_profile=profile_name, attempt=req.attempt, source=req.source,
                           notes=req.notes, profile=profile)
    return {"saved": rec["id"], "h2s": rec["h2s"], "total_records": len(rec_store.load())}


@app.get("/api/calibration")
def calibration_status() -> dict:
    recs = rec_store.load()
    return {
        "labeled_records": len(recs),
        "with_parameter_breakdown": len([r for r in recs if any(p in r.get("h2s", {}) for p in PARAMETERS)]),
        "evaluated_records": len([r for r in recs if r.get("engine")]),
        "model": calibration.load_model(),
    }


@app.post("/api/calibration/fit")
def calibration_fit(request: Request) -> dict:
    _limit(request)
    return calibration.fit_all()
