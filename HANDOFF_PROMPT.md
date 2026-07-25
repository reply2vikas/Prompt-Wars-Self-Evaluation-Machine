# Handoff prompt — build "PromptWars Self-Scorer"

> Paste everything below the line into another LLM (GPT-5.6 Sol, Gemini 3.x,
> GLM-5.2, etc.). It is self-contained — the model does not need to see the
> reference implementation. The final section explicitly asks the model to
> propose ONE unique enhancement, so you can compare ideas across LLMs and
> cherry-pick the best.

---

## Role

You are a senior Python + TypeScript engineer. Build a small, well-tested tool
called **PromptWars Self-Scorer**. Ship a complete repo: library + CLI + a thin
FastAPI web app, with tests. Prioritise correctness, honest design, and
tidiness over feature count.

## What the tool does

It estimates how a hackathon submission would score on the **six PromptWars
judging parameters** and returns a per-parameter breakdown with concrete fixes.
It is an explicit **proxy** for a hidden grader — say so in the README; never
imply it reproduces the real rubric.

The six parameters and their composite weights (High=3, Med=2, Low=1):

| Parameter | Weight |
| --- | --- |
| Code Quality | High |
| Problem Alignment | High |
| Security | Med |
| Efficiency | Med |
| Testing | Low |
| Accessibility | Low |

Weights are a tunable constant, derived from observed rank behaviour, not
official. Expose them so they can be recalibrated later.

## Core design principle: a HYBRID scorer

Do **not** hand the whole judgement to the LLM. Split into two layers:

1. **Deterministic layer (authoritative).** Pure static analysis over the repo,
   no LLM. Gather: total repo size in MB (relevant to a 10 MB submission limit),
   file count, LOC by extension, dependency inventory, count of test files and
   edge-case test hits, presence of lint/type/CI configs, accessibility signals
   (aria / alt / role / semantic tags), GenAI-usage signals (imports/SDK calls
   for Anthropic/OpenAI/Google GenAI), and **hardcoded secrets** (narrow regexes;
   ignore `.env.example` and obvious placeholders). Turn hard issues into
   **flags** and **score caps** the LLM cannot override:
   - any hardcoded secret → cap Security at 40
   - no GenAI detected → cap Problem Alignment at 30 (GenAI is mandatory; missing = DQ risk)
   - repo > 10 MB, no tests, no CI → flags
2. **LLM-as-judge layer (advisory).** Feed the static evidence + a sample of
   source to one model call that returns STRICT JSON: for each parameter, an
   integer 0–100, a ≤2-sentence rationale grounded in the evidence, and up to 3
   concrete fixes. Parse defensively (strip code fences, clamp 0–100, fill
   missing params with 0). Isolate the model call behind an injectable function
   so tests mock it and never touch the network.

Encode these judging nuances in the judge's prompt (they matter):
- **Code Quality** rewards *genuine engineering depth* (real domain modelling,
  multi-view app, sound patterns) — NOT comment density or late modularity, and
  it *penalises padding*. A tidy-but-thin single-page app should top out in the
  high 80s.
- **Testing** specifically rewards **edge-case** tests, not just happy paths.
- **Problem Alignment** rewards one deep persona with a real before/after story
  and GenAI that genuinely reasons/generates (not rule-based logic in disguise).
- **Efficiency** rewards small bundle, fast cold start, and **cost-aware model
  routing** (not always the biggest model).

## Judge model

Default judge model = **Claude Fable 5** (`claude-fable-5`), read from
`PROMPTWARS_JUDGE_MODEL`. Support routing to a cheaper model for iteration
passes. Use the Anthropic Messages API (`max_tokens` ~1500, a strict-JSON system
prompt). Keep the provider swappable via the injectable judge function so the
same tool can target another API if needed.

## Submission artifacts

Accept an optional `submission.json` at the repo root with these fields:
`app_live_link`, `deck_link`, `demo_video_link`, `prototype_brief`,
`linkedin_post_link`. Feed `prototype_brief` and `linkedin_post_link` into the
judge's Problem Alignment context (the LinkedIn write-up is a real, equally
weighted documentation layer). Missing artifacts → flags (no live link, no
LinkedIn post, no demo video), so the tool doubles as a submission checklist.
The web UI must let the user enter these five fields (they override the file)
and display them with the results.

## Interfaces

- **Library:** `score_repo(root, challenge=..., model=..., judge_fn=..., submission=...) -> ScoreCard`.
  `ScoreCard` exposes a weighted `composite`, per-parameter scores, flags,
  submission dict, and an `as_dict()`.
- **CLI:** `promptwars-score <repo> [--challenge FILE] [--model M] [--gate N] [--json]`.
  Print a readable table with per-parameter bars, rationale, fixes, artifacts,
  and flags. `--gate` makes it exit nonzero when composite < N (CI hard gate).
- **Web app:** thin FastAPI (`GET /` serves a single-file UI; `POST /api/score`
  runs the scorer). The UI: a repo-path input, model field, challenge textarea,
  the five artifact fields, and a results view with six gauges + flags + links.
  It is a **local dev tool** (reads local paths server-side) — say so; don't add auth.

## Tech + quality bar

- Python ≥3.10, standard library where possible; `anthropic` for the judge;
  `fastapi`/`uvicorn`/`pydantic` as an optional `[web]` extra. No PyYAML (use JSON).
- Frontend: one `index.html`, vanilla JS, no framework, no browser storage,
  accessible (labels, visible focus, `aria-live` on results), responsive to mobile.
- Tests: `pytest`, judge always mocked (no network, no key spent). Cover secret
  detection, placeholder-ignoring, GenAI/test detection, JSON clamp/fence-strip,
  the two score caps, weighted composite, and submission load + flags.
- Ship: `pyproject.toml` (entry point `promptwars-score`), a README with the
  honest-proxy caveat and CI-gate example, keep the repo tiny (< 1 MB).

## Deliverable

The full repo as files. Clean, commented where it earns it, no dead code
(padding lowers real Code-Quality scores — practise what the tool preaches).

## One thing I want from YOU specifically

After building the spec above, propose and implement **exactly one** novel
enhancement that would make this scorer more predictive or more useful, that is
NOT already described here. Put it behind a clear flag or module so it's easy to
keep or drop. In a short `ENHANCEMENT.md`, explain what it is, why it improves
predictiveness, and its cost/risk. Examples of the *kind* of thing (do not just
copy these — surprise me): a two-version diff mode that reports per-parameter
deltas; a calibration file that regresses the tool's scores against real
returned scores; an AST-based padding/duplication detector for Code Quality; a
real axe/Lighthouse pass for Accessibility instead of regex signals.
