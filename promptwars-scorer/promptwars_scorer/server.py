"""Thin FastAPI wrapper around the scorer, plus the single-page web UI.

    uvicorn promptwars_scorer.server:app --reload
    # open http://127.0.0.1:8000

The scorer runs server-side (it reads a local repo path), so this is a local
dev tool — do not expose it publicly, since it reads arbitrary local paths.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .scorer import DEFAULT_CHALLENGE, score_repo
from .judge import DEFAULT_MODEL
from .submission import Submission

app = FastAPI(title="PromptWars Self-Scorer", version="0.1.0")
_WEB = Path(__file__).parent / "web" / "index.html"


class ScoreRequest(BaseModel):
    repo_path: str
    challenge: str | None = None
    model: str = DEFAULT_MODEL
    app_live_link: str = ""
    deck_link: str = ""
    demo_video_link: str = ""
    prototype_brief: str = ""
    linkedin_post_link: str = ""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _WEB.read_text()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/score")
def score(req: ScoreRequest) -> dict:
    root = Path(req.repo_path).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {root}")
    ui_sub = Submission.from_dict(req.model_dump())
    # Use UI-provided artifacts if any were filled, else fall back to submission.json.
    sub = ui_sub if any(ui_sub.as_dict().values()) else None
    card = score_repo(
        str(root),
        challenge=req.challenge or DEFAULT_CHALLENGE,
        model=req.model,
        submission=sub,
    )
    return card.as_dict()
