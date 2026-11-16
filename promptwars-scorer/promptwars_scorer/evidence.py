"""Static analysis of a submission repo.

Gathers cheap, deterministic signals *before* the LLM judge runs, so the judge
reasons over facts rather than guessing. Also produces hard flags (secrets,
>10MB repo, missing GenAI) that the scorer turns into score caps / DQ warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

_BLOB_CAP = 4 * 1024 * 1024  # cap the coverage text blob to bound memory

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "target",
    "venv_packages", "site-packages", "vendor", "vendor_packages", ".tox",
}
TEXT_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".md", ".json",
    ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".env.example",
}
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx"}

# Secret patterns. Deliberately narrow to limit false positives; .env.example
# and obvious placeholders are excluded by the caller.
SECRET_PATTERNS = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_key": re.compile(r"sk-[A-Za-z0-9]{32,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "generic_assignment": re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*"
        r"['\"][A-Za-z0-9_\-]{16,}['\"]"
    ),
}
PLACEHOLDER = re.compile(
    r"(?i)your[_-]?key|xxx+|placeholder|example|dummy|mock|fake|sample|<[^>]+>|changeme|\.\.\.",
)

GENAI_HINTS = (
    "anthropic", "openai", "google.generativeai", "genai", "@google/genai",
    "vertexai", "@anthropic-ai/sdk", "messages.create", "chat.completions",
    "generateContent", "gemini",
    # more hosted providers
    "grok", "groq", "cohere", "mistral", "bedrock", "@ai-sdk", "litellm",
    # local ML / embeddings / RAG (e.g. sentence-transformers + retrieval)
    "sentence_transformers", "sentencetransformer", "huggingface", "from transformers",
    "faiss", "chromadb", "pinecone", "langchain", "llamaindex", "ollama",
    "rag_chain", "vectorstore", "retrieval augmented",
    # provider env vars
    "openai_api_key", "gemini_api_key", "anthropic_api_key", "groq_api_key",
    "grok_api_key", "cohere_api_key", "huggingface_api_key",
)

# --- Evidence coverage (folded in so gather() stays ONE filesystem pass) -------
# The signals above answer "does the capability exist?"; coverage answers "is it
# visible enough for a judge to reward?". Keyed by the six PARAMETERS (snake).
COVERAGE_EXPECTED: dict[str, list[tuple[str, dict]]] = {
    "code_quality": [
        ("linter config",      {"paths": ["*eslint*", "*.ruff.toml", "ruff.toml", "*.flake8", ".pylintrc", "*biome*"]}),
        ("formatter config",   {"paths": ["*prettier*", "pyproject.toml"], "text": ["black", "prettier"]}),
        ("type config",        {"paths": ["tsconfig*.json", "mypy.ini"], "text": ["strict", "mypy"]}),
        ("CI workflow",        {"paths": [".github/workflows/*", "*.gitlab-ci.yml", "*circleci*"]}),
        ("modular structure",  {"paths": ["packages/*", "apps/*", "src/*", "*/services/*", "*/routes/*"]}),
        ("design/architecture docs", {"paths": ["*prd*.md", "*trd*.md", "*architecture*.md", "*design*.md", "docs/*"]}),
    ],
    "security": [
        ("password hashing",   {"text": ["bcrypt", "pbkdf2", "argon2", "scrypt"]}),
        ("auth / sessions",    {"text": ["jwt", "session", "oauth", "authenticate", "authorization"]}),
        ("secret management",  {"paths": ["*.env.example", ".env.sample"], "text": ["process.env", "os.environ", "getenv"]}),
        ("input validation",   {"text": ["zod", "pydantic", "joi", "express-validator", "validator"]}),
        ("dependency auditing",{"text": ["pip-audit", "npm audit", "dependabot", "snyk"]}),
        ("prompt-injection defense", {"text": ["prompt injection", "prompt-injection", "untrusted", "sanitize"]}),
        ("security tests",     {"paths": ["*security*test*", "*test*security*"]}),
    ],
    "efficiency": [
        ("caching",            {"text": ["redis", "cache", "memoize", "lru_cache"]}),
        ("async / concurrency",{"text": ["async ", "await ", "worker", "queue", "asyncio", "threadpool"]}),
        ("pagination/streaming",{"text": ["paginat", "cursor", "stream", "chunk"]}),
        ("db indexing/migrations", {"paths": ["*/migrations/*", "*migrate*"], "text": ["index", "pgvector"]}),
        ("cost-aware GenAI",   {"text": ["model routing", "haiku", "mini", "flash", "cheaper model"]}),
    ],
    "testing": [
        ("test directory",     {"paths": ["tests/*", "test/*", "*/test/*", "*/__tests__/*"]}),
        ("test files",         {"paths": ["*test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.spec.*"]}),
        ("test framework config", {"paths": ["*vitest*", "*jest*", "pytest.ini"], "text": ["pytest", "vitest", "jest", "supertest"]}),
        ("coverage config",    {"text": ["coverage", "--cov", "codecov"]}),
        ("edge-case tests",    {"text": ["edge", "boundary", "invalid", "malformed", "empty input"]}),
        ("e2e / smoke tests",  {"text": ["e2e", "smoke", "playwright", "cypress"]}),
    ],
    "accessibility": [
        ("a11y test tooling",  {"text": ["axe-core", "jest-axe", "jsx-a11y", "@axe"]}),
        ("ARIA attributes",    {"text": ["aria-", "role=\"", "role='"]}),
        ("semantic HTML",      {"text": ["<main", "<nav", "<header", "<section", "<button"]}),
        ("alt text",           {"text": ["alt=", "aria-label"]}),
        ("a11y in docs",       {"paths": ["*uiux*.md", "*accessib*"], "text": ["accessibility", "wcag", "a11y"]}),
    ],
    "problem_alignment": [
        ("GenAI integration",  {"text": ["openai", "anthropic", "gemini", "llm", "embedding", "generatecontent"]}),
        ("problem stated in README", {"paths": ["readme*.md"], "text": ["problem", "challenge", "use case", "persona"]}),
        ("PRD / requirements", {"paths": ["*prd*.md", "*requirements*.md"]}),
        ("deploy config",      {"paths": ["render.yaml", "vercel.json", "*dockerfile*", "docker-compose*", "netlify.toml"]}),
        ("live demo / deployment", {"text": ["deployed", "live demo", "vercel.app", "onrender.com", "netlify.app"]}),
    ],
}

COVERAGE_README_MAP: dict[str, list[str]] = {
    "security architecture": ["security", "auth", "encryption", "hashing"],
    "testing strategy":      ["test", "coverage", "vitest", "pytest", "jest"],
    "AI integration":        ["ai", "genai", "llm", "openai", "anthropic", "gemini", "embedding"],
    "accessibility":         ["accessibility", "a11y", "wcag", "aria"],
    "deployment":            ["deploy", "docker", "render", "vercel", "hosting"],
    "limitations":           ["limitation", "disclaimer", "known issue", "caveat", "not legal advice"],
}

# Per-challenge overlays: a stadium app and a legal-AI app shouldn't share an
# evidence checklist. These ADD to the base set; pass challenge_profile to gather().
COVERAGE_PROFILES: dict[str, dict[str, list[tuple[str, dict]]]] = {
    "legal_ai": {
        "security": [
            ("document access control", {"text": ["access control", "rbac", "tenant"]}),
            ("PII handling",            {"text": ["pii", "redact", "anonymi", "gdpr"]}),
        ],
        "testing": [
            ("parser / citation tests", {"text": ["citation", "parser test", "extraction test"]}),
        ],
        "problem_alignment": [
            ("legal-domain grounding",  {"text": ["statute", "clause", "contract", "case law", "jurisdiction"]}),
        ],
    },
}


def _cov_effective(profile: str | None) -> dict[str, list[tuple[str, dict]]]:
    if not profile or profile not in COVERAGE_PROFILES:
        return COVERAGE_EXPECTED
    merged = {p: list(v) for p, v in COVERAGE_EXPECTED.items()}
    for p, extra in COVERAGE_PROFILES[profile].items():
        merged[p] = merged.get(p, []) + extra
    return merged


def _cov_match(detector: dict, paths: list[str], blob: str) -> bool:
    for pat in detector.get("paths", []):
        pat = pat.lower()
        if any(fnmatch(p, pat) for p in paths):
            return True
    for needle in detector.get("text", []):
        if needle.lower() in blob:
            return True
    return False


def compute_coverage(paths: list[str], blob: str, profile: str | None = None) -> dict[str, dict]:
    """found/missing/coverage_pct per parameter — deterministic, no LLM."""
    out: dict[str, dict] = {}
    for param, arts in _cov_effective(profile).items():
        found, missing = [], []
        for label, det in arts:
            (found if _cov_match(det, paths, blob) else missing).append(label)
        n, m = len(found), len(arts)
        out[param] = {
            "found": found, "missing": missing,
            "found_count": n, "expected_count": m,
            "coverage_pct": round(100 * n / m, 1) if m else 0.0,
        }
    return out


def analyze_readme(readme_text: str) -> dict:
    """Which of the six documentation dimensions the README actually names."""
    t = readme_text.lower()
    res = {theme: any(k in t for k in keys) for theme, keys in COVERAGE_README_MAP.items()}
    res["coverage_pct"] = round(100 * sum(v for v in res.values() if isinstance(v, bool)) / len(COVERAGE_README_MAP), 1)
    return res


@dataclass
class Evidence:
    root: str
    size_mb: float = 0.0
    file_count: int = 0
    loc_by_ext: dict[str, int] = field(default_factory=dict)
    deps: dict[str, list[str]] = field(default_factory=dict)
    test_files: int = 0
    edge_case_hits: int = 0
    configs: list[str] = field(default_factory=list)
    a11y_signals: dict[str, int] = field(default_factory=dict)
    genai_signals: list[str] = field(default_factory=list)
    has_ci: bool = False
    secrets: list[str] = field(default_factory=list)
    source_sample: str = ""
    coverage: dict = field(default_factory=dict)
    readme_map: dict = field(default_factory=dict)
    sampled_files: list[str] = field(default_factory=list)

    def summary(self) -> str:
        loc = ", ".join(f"{k}:{v}" for k, v in sorted(self.loc_by_ext.items()))
        deps = "; ".join(f"{k}={len(v)}" for k, v in self.deps.items()) or "none"
        a11y = ", ".join(f"{k}:{v}" for k, v in self.a11y_signals.items()) or "none"
        return (
            f"repo_size={self.size_mb:.2f}MB (limit 10MB), files={self.file_count}\n"
            f"loc_by_ext: {loc}\n"
            f"dependency_groups: {deps}\n"
            f"test_files={self.test_files}, edge_case_test_hits={self.edge_case_hits}\n"
            f"configs_present: {', '.join(self.configs) or 'none'}\n"
            f"accessibility_signals: {a11y}\n"
            f"genai_signals: {', '.join(sorted(set(self.genai_signals))) or 'NONE'}\n"
            f"ci_present={self.has_ci}\n"
            f"hardcoded_secrets={self.secrets or 'none'}"
        )

    def judge_context(self) -> dict:
        """Structured context for the judge: keeps coverage/readme legible instead
        of flattening everything into one human-readable string."""
        return {"summary": self.summary(), "coverage": self.coverage, "readme_map": self.readme_map}


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            yield p


def _scan_secrets(text: str, name: str) -> list[str]:
    hits = []
    for label, pat in SECRET_PATTERNS.items():
        for m in pat.finditer(text):
            snippet = m.group(0)
            if PLACEHOLDER.search(snippet):
                continue
            if name.endswith(".example") or "example" in name.lower():
                continue
            if re.search(r"(?i)test_|_test\.|\.test\.|\.spec\.|__tests__", name):
                continue  # test fixtures routinely contain fake creds
            hits.append(f"{label} in {name}")
    return hits


def _sample_priority(rel: str, name: str) -> int:
    """Lower = sampled first. Sends the judge the evidence-dense files (README,
    docs, security, tests, AI modules) before generic source, within the budget."""
    r = "/" + rel.lower()
    n = name.lower()
    if n.startswith("readme"):
        return 0
    if "/docs/" in r or r.endswith((".md",)) and any(k in r for k in ("prd", "trd", "architecture", "design")):
        return 1
    if any(k in r for k in ("security", "/auth")):
        return 2
    if "test" in r or "spec" in r:
        return 3
    if any(k in r for k in ("ai_service", "/llm", "rag", "genai", "embedding", "provider", "/ai/")):
        return 4
    if any(k in r for k in ("routes", "services", "/api")):
        return 5
    return 6


def gather(root: str, sample_budget: int = 6000, challenge_profile: str | None = None) -> Evidence:
    rp = Path(root)
    ev = Evidence(root=str(rp))
    total_bytes = 0
    cov_paths: list[str] = []
    blob_parts: list[str] = []
    blob_len = 0
    readme_parts: list[str] = []
    sample_cands: list[tuple[int, str, str]] = []
    a11y = {"aria": 0, "alt": 0, "semantic_tags": 0, "role": 0}
    edge_words = re.compile(r"(?i)\bedge|boundary|empty|invalid|malformed|overflow|null|None\b")

    for p in _iter_files(rp):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        total_bytes += size
        ev.file_count += 1
        name = p.name
        ext = p.suffix
        cov_paths.append(p.relative_to(rp).as_posix().lower())

        if name in {"requirements.txt", "pyproject.toml"}:
            ev.deps.setdefault("python", [])
        if name == "package.json":
            ev.deps.setdefault("node", [])
        if name in {"ruff.toml", ".ruff.toml", "mypy.ini", ".eslintrc",
                    ".eslintrc.json", ".eslintrc.cjs", "tsconfig.json",
                    ".prettierrc", "vitest.config.ts", "pytest.ini"}:
            ev.configs.append(name)
        if "pyproject.toml" in name or name.startswith("setup"):
            ev.configs.append(name)

        if p.suffix and (".github/workflows" in str(p)):
            ev.has_ci = True

        if ext not in TEXT_EXT and ext not in CODE_EXT:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue

        if ext in CODE_EXT or ext in {".html", ".css", ".md"}:
            ev.loc_by_ext[ext] = ev.loc_by_ext.get(ext, 0) + text.count("\n") + 1

        ev.secrets.extend(_scan_secrets(text, name))

        low = text.lower()
        if blob_len < _BLOB_CAP:
            blob_parts.append(low)
            blob_len += len(low)
        if name.lower().startswith("readme") and ext == ".md":
            readme_parts.append(low)
        for hint in GENAI_HINTS:
            if hint.lower() in low:
                ev.genai_signals.append(hint)

        if re.search(r"(?i)test_|\.test\.|\.spec\.", name):
            ev.test_files += 1
            ev.edge_case_hits += len(edge_words.findall(text))

        if ext in {".html", ".jsx", ".tsx"}:
            a11y["aria"] += len(re.findall(r"aria-[a-z]+", text))
            a11y["alt"] += len(re.findall(r"\balt\s*=", text))
            a11y["role"] += len(re.findall(r"\brole\s*=", text))
            a11y["semantic_tags"] += len(
                re.findall(r"<(?:main|nav|header|footer|section|article|aside)\b", text)
            )

        if name in {"requirements.txt"}:
            ev.deps["python"] = [
                ln.split("==")[0].strip()
                for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")
            ]
        if name == "package.json":
            ev.deps["node"] = re.findall(r'"([^"]+)"\s*:\s*"[\^~]?\d', text)

        rel_l = p.relative_to(rp).as_posix()
        if ext in CODE_EXT or name.lower().startswith("readme") or ext == ".md" or "/docs/" in "/" + rel_l.lower():
            sample_cands.append((_sample_priority(rel_l, name), rel_l, text[:1200]))

    ev.size_mb = round(total_bytes / (1024 * 1024), 3)
    ev.a11y_signals = a11y
    ev.configs = sorted(set(ev.configs))
    sample_cands.sort(key=lambda c: (c[0], c[1]))
    picked, used = [], 0
    for _prio, rel_l, chunk in sample_cands:
        block = f"\n--- {rel_l} ---\n{chunk}"
        if used + len(block) > sample_budget and picked:
            break
        picked.append(block)
        ev.sampled_files.append(rel_l)
        used += len(block)
    ev.source_sample = "".join(picked)[:sample_budget] or "(no source files sampled)"
    ev.coverage = compute_coverage(cov_paths, "\n".join(blob_parts), challenge_profile)
    ev.readme_map = analyze_readme("\n".join(readme_parts))
    return ev


def static_flags(ev: Evidence) -> list[str]:
    """Hard issues that map to score caps / DQ risk, per the challenge rules."""
    flags = []
    if ev.secrets:
        flags.append(f"SECRET LEAK: {len(ev.secrets)} hardcoded secret(s) — caps Security.")
    if ev.size_mb > 10:
        flags.append(f"REPO TOO BIG: {ev.size_mb:.1f}MB > 10MB submission limit.")
    if not ev.genai_signals:
        flags.append("NO GENAI DETECTED: GenAI usage is mandatory — DQ risk.")
    if ev.test_files == 0:
        flags.append("NO TESTS FOUND.")
    if not ev.has_ci:
        flags.append("NO CI WORKFLOW: a red/absent pipeline can read as a quality signal.")
    return flags
