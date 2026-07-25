"""Tests for the static evidence layer and scorer orchestration.

The LLM judge is always mocked — tests never hit the network or spend a key.
"""

import json
import textwrap
from pathlib import Path

import pytest

from promptwars_scorer import gather, static_flags, parse_judgement, score_repo
from promptwars_scorer.rubric import PARAMETERS


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content))
    return tmp_path


# --- evidence -----------------------------------------------------------------

def test_detects_hardcoded_secret(tmp_path):
    repo = _make_repo(tmp_path, {
        "app.py": 'API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n',
    })
    ev = gather(str(repo))
    assert ev.secrets, "should flag a real anthropic key"
    assert any("SECRET LEAK" in f for f in static_flags(ev))


def test_ignores_placeholder_secret(tmp_path):
    repo = _make_repo(tmp_path, {
        ".env.example": 'ANTHROPIC_API_KEY="your-key-here"\n',
        "cfg.py": 'API_KEY = "your_key_placeholder"\n',
    })
    ev = gather(str(repo))
    assert not ev.secrets


def test_detects_genai_and_tests(tmp_path):
    repo = _make_repo(tmp_path, {
        "svc.py": "from anthropic import Anthropic\nclient = Anthropic()\n",
        "test_svc.py": "def test_empty_input():\n    assert True  # edge case: empty\n",
    })
    ev = gather(str(repo))
    assert "anthropic" in ev.genai_signals
    assert ev.test_files == 1
    assert ev.edge_case_hits >= 1
    assert "NO GENAI DETECTED" not in " ".join(static_flags(ev))


def test_missing_genai_is_flagged(tmp_path):
    repo = _make_repo(tmp_path, {"main.py": "print('hello')\n"})
    ev = gather(str(repo))
    assert not ev.genai_signals
    assert any("NO GENAI" in f for f in static_flags(ev))


def test_accessibility_signals(tmp_path):
    repo = _make_repo(tmp_path, {
        "index.html": '<main><img alt="crest" src="x"><nav aria-label="menu" role="navigation"></nav></main>',
    })
    ev = gather(str(repo))
    assert ev.a11y_signals["alt"] >= 1
    assert ev.a11y_signals["semantic_tags"] >= 2
    assert ev.a11y_signals["aria"] >= 1


# --- judge parsing ------------------------------------------------------------

def test_parse_clamps_and_fills_all_params():
    raw = json.dumps({"parameters": {
        "code_quality": {"score": 150, "rationale": "r", "fixes": ["a", "b", "c", "d"]},
    }})
    parsed = parse_judgement(raw)
    assert set(parsed) == set(PARAMETERS)
    assert parsed["code_quality"].score == 100          # clamped
    assert len(parsed["code_quality"].fixes) == 3       # capped
    assert parsed["security"].score == 0                # missing -> 0


def test_parse_strips_code_fences():
    raw = '```json\n{"parameters": {"testing": {"score": 88, "rationale": "ok"}}}\n```'
    parsed = parse_judgement(raw)
    assert parsed["testing"].score == 88


# --- scorer caps --------------------------------------------------------------

def _fake_judge(_c, _e, _s, _m):
    return json.dumps({"parameters": {
        p: {"score": 95, "rationale": "looks strong", "fixes": []} for p in PARAMETERS
    }})


def test_secret_caps_security_score(tmp_path):
    repo = _make_repo(tmp_path, {
        "svc.py": 'from anthropic import Anthropic\nKEY="sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n',
    })
    card = score_repo(str(repo), judge_fn=_fake_judge)
    assert card.params["security"].score <= 40   # judge said 95, cap wins


def test_missing_genai_caps_alignment(tmp_path):
    repo = _make_repo(tmp_path, {"main.py": "print(1)\n"})
    card = score_repo(str(repo), judge_fn=_fake_judge)
    assert card.params["problem_alignment"].score <= 30


def test_composite_is_weighted(tmp_path):
    repo = _make_repo(tmp_path, {
        "svc.py": "from anthropic import Anthropic\n",
        "test_x.py": "def test_edge_empty(): assert True\n",
    })
    card = score_repo(str(repo), judge_fn=_fake_judge)
    # all params 95, no caps triggered -> composite 95
    assert card.composite == pytest.approx(95.0)


# --- submission artifacts -----------------------------------------------------

def test_submission_loaded_from_json(tmp_path):
    import json as _json
    repo = _make_repo(tmp_path, {
        "svc.py": "from anthropic import Anthropic\n",
        "submission.json": _json.dumps({
            "app_live_link": "https://demo.example/app",
            "linkedin_post_link": "https://linkedin.com/posts/x",
            "prototype_brief": "Volunteer copilot: reasons over crowd density.",
        }),
    })
    card = score_repo(str(repo), judge_fn=_fake_judge)
    assert card.submission["app_live_link"].endswith("/app")
    assert "NO LIVE APP LINK" not in " ".join(card.flags)
    assert "NO DEMO VIDEO" in " ".join(card.flags)  # not provided


def test_missing_submission_artifacts_flagged(tmp_path):
    repo = _make_repo(tmp_path, {"svc.py": "from anthropic import Anthropic\n"})
    card = score_repo(str(repo), judge_fn=_fake_judge)
    joined = " ".join(card.flags)
    assert "NO LINKEDIN POST" in joined and "NO LIVE APP LINK" in joined


def test_brief_reaches_judge_context():
    from promptwars_scorer import Submission
    ctx = Submission(prototype_brief="deep persona story").judge_context()
    assert "deep persona story" in ctx
