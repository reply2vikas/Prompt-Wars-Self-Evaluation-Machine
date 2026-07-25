"""Static analysis of a submission repo.

Gathers cheap, deterministic signals *before* the LLM judge runs, so the judge
reasons over facts rather than guessing. Also produces hard flags (secrets,
>10MB repo, missing GenAI) that the scorer turns into score caps / DQ warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", "target",
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
    r"(?i)your[_-]?key|xxx+|placeholder|example|dummy|<[^>]+>|changeme|\.\.\.",
)

GENAI_HINTS = (
    "anthropic", "openai", "google.generativeai", "genai", "@google/genai",
    "vertexai", "@anthropic-ai/sdk", "messages.create", "chat.completions",
    "generateContent", "gemini",
)


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
            hits.append(f"{label} in {name}")
    return hits


def gather(root: str, sample_budget: int = 6000) -> Evidence:
    rp = Path(root)
    ev = Evidence(root=str(rp))
    total_bytes = 0
    samples: list[str] = []
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

        if ext in CODE_EXT and len("".join(samples)) < sample_budget:
            samples.append(f"\n--- {p.relative_to(rp)} ---\n{text[:1200]}")

    ev.size_mb = round(total_bytes / (1024 * 1024), 3)
    ev.a11y_signals = a11y
    ev.configs = sorted(set(ev.configs))
    ev.source_sample = "".join(samples)[:sample_budget] or "(no source files sampled)"
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
