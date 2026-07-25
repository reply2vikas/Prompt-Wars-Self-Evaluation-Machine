"""Submission artifacts that sit alongside the code but are part of what gets
judged — especially the LinkedIn post (the documentation layer PromptWars treats
as equally important) and the prototype brief, which both feed Problem Alignment.

Loaded from an optional `submission.json` at the repo root, or passed directly.
Kept dependency-free (stdlib json) so the core library stays install-light.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_FIELDS = ("app_live_link", "deck_link", "demo_video_link",
           "prototype_brief", "linkedin_post_link")


@dataclass
class Submission:
    app_live_link: str = ""
    deck_link: str = ""
    demo_video_link: str = ""
    prototype_brief: str = ""
    linkedin_post_link: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Submission":
        return cls(**{k: str(d.get(k, "")).strip() for k in _FIELDS})

    @classmethod
    def load(cls, repo_root: str) -> "Submission":
        p = Path(repo_root) / "submission.json"
        if p.is_file():
            try:
                return cls.from_dict(json.loads(p.read_text()))
            except (ValueError, OSError):
                return cls()
        return cls()

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in _FIELDS}

    def judge_context(self) -> str:
        """Extra context handed to the judge for Problem Alignment."""
        if not self.prototype_brief and not self.linkedin_post_link:
            return ""
        parts = []
        if self.prototype_brief:
            parts.append(f"Prototype brief (author's own framing):\n{self.prototype_brief}")
        if self.linkedin_post_link:
            parts.append(f"A LinkedIn write-up exists: {self.linkedin_post_link}")
        return "\n\n# Submission documentation\n" + "\n".join(parts)

    def flags(self) -> list[str]:
        out = []
        if not self.linkedin_post_link:
            out.append("NO LINKEDIN POST: documentation is a mandatory submission layer.")
        if not self.app_live_link:
            out.append("NO LIVE APP LINK: a deployed, working link is required.")
        if not self.demo_video_link:
            out.append("NO DEMO VIDEO: recommended for functional/manual review.")
        return out
