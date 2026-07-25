# PromptWars Self-Scorer

A pre-submission gut-check that scores a repo on the six PromptWars parameters —
**Code Quality, Security, Efficiency, Testing, Accessibility, Problem Alignment** —
and gives you the per-parameter breakdown your playbook says to get *first*.

> **Honest caveat.** This is a **proxy**, not Google's grader. It can't see the
> real rubric or weights. It combines deterministic static checks (authoritative)
> with an LLM-as-judge (advisory). Use it to catch obvious gaps and to compare
> two versions of your own submission — not as ground truth for your rank.

## What it actually measures

- **Deterministic (hard, trusted):** repo size vs the 10 MB limit, hardcoded
  secrets, GenAI presence (mandatory — missing = DQ risk), test-file and
  edge-case counts, lint/type/CI configs, accessibility signals (aria/alt/
  semantic tags), dependency inventory. These produce **flags** and **score caps**
  the LLM can't override (a leaked key caps Security at 40; missing GenAI caps
  Problem Alignment at 30).
- **LLM-as-judge (advisory):** scores each parameter 0–100 with a rationale and
  concrete fixes, using the static evidence + sampled source. The prompt encodes
  your playbook lessons — e.g. Code Quality rewards *genuine depth*, not comment
  density or late modularity, and penalises padding.

Composite weighting: Code Quality & Problem Alignment = High, Security &
Efficiency = Med, Testing & Accessibility = Low (from Challenge 3 rank behaviour;
edit `WEIGHTS` in `rubric.py` if you gather better ground truth).

## Install

```bash
pip install -e ".[web,dev]"
export ANTHROPIC_API_KEY=sk-ant-...
# Judge defaults to Fable 5 (claude-fable-5). Route to a cheaper model on
# iteration passes, then use Fable 5 for the version you're about to submit:
export PROMPTWARS_JUDGE_MODEL=claude-fable-5      # or claude-sonnet-5 for cheap passes
```

## CLI

```bash
promptwars-score ./my-repo                         # table view
promptwars-score ./my-repo --json                  # machine-readable
promptwars-score ./my-repo --challenge challenge.md # custom problem statement
promptwars-score ./my-repo --model claude-haiku-4-5 # cheaper routing
promptwars-score ./my-repo --gate 90               # exit 1 if composite < 90 (CI gate)
```

## Submission artifacts

Drop a `submission.json` at the repo root and the scorer picks it up
automatically (or fill the fields in the web UI, which override the file):

```json
{
  "app_live_link": "https://your-app.vercel.app",
  "deck_link": "https://.../deck",
  "demo_video_link": "https://youtu.be/...",
  "linkedin_post_link": "https://www.linkedin.com/posts/...",
  "prototype_brief": "One-paragraph before/after story: persona, the moment GenAI improves, why it matters."
}
```

The `prototype_brief` and `linkedin_post_link` are fed into the judge's
**Problem Alignment** context (PromptWars treats the LinkedIn write-up as an
equally-weighted documentation layer). Missing artifacts become flags — no live
link, no LinkedIn post, no demo video — so the tool doubles as a submission
checklist.

## Web UI

```bash
uvicorn promptwars_scorer.server:app --reload
# open http://127.0.0.1:8000
```

Paste a local repo path → six scoreboard gauges + flags. Local dev tool only:
it reads arbitrary local paths server-side, so don't expose it publicly.

## Library

```python
from promptwars_scorer import score_repo
card = score_repo("./my-repo")           # uses anthropic_judge by default
print(card.composite, card.flags)
for name, p in card.params.items():
    print(name, p.score, p.fixes)
```

Inject your own judge (e.g. for tests or a different provider):

```python
score_repo("./my-repo", judge_fn=lambda challenge, evidence, sample, model: '{"parameters": {...}}')
```

## Use it as a CI gate

```yaml
- run: promptwars-score . --gate 90 --model claude-haiku-4-5
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

## Calibrate it

The judge's absolute numbers drift; its **deltas between your own versions** are
the useful signal (same as the real grader per your playbook — a param stuck at
the same number across different code is the real signal, ±1 flicker is noise).
When you get official per-parameter scores back, nudge `WEIGHTS` and the
`PARAM_GUIDANCE` strings in `rubric.py` toward them.

## Tests

```bash
pytest -q      # 10 tests, judge fully mocked — no network, no key spent
```
