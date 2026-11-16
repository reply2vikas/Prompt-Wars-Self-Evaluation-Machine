"""
Safe remote-repo ingestion for PromptWars Self-Scorer.

Turns a public GitHub URL into a local path your existing `score_repo` can read,
under the same gates the H2S grader enforces (public-only, < 10 MB), and without
ever executing anything from the untrusted repo.

Threat model this guards against:
  - SSRF / non-GitHub hosts               -> host allowlist + URL parse
  - Oversized repos / zip-bomb histories  -> GitHub API size precheck + depth-1 clone + local backstop
  - Symlink escapes out of the sandbox    -> core.symlinks=false on clone + no-follow size walk
  - Credential prompts hanging the server  -> GIT_TERMINAL_PROMPT=0, isolated HOME
  - Leftover disk                          -> ephemeral tempdir, always cleaned up

What it deliberately does NOT do: install dependencies, run setup.py/conftest,
execute tests, or import repo code. Everything downstream must treat the cloned
tree as untrusted DATA only. For true isolation in production, run this whole
path inside an ephemeral container with a disk quota, dropped capabilities, and
egress limited to your git host + your LLM API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {"github.com", "www.github.com"}
MAX_REPO_BYTES = 10 * 1024 * 1024        # H2S rule: repo < 10 MB
CLONE_TIMEOUT_SEC = 60
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class IngestError(Exception):
    """Raised when a repo can't be safely ingested. Message is user-safe."""


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}"


def parse_github_url(url: str) -> RepoRef:
    """Accept the many shapes people paste (…/tree/main, …/blob/…, trailing .git,
    trailing slash) and reduce to owner/repo. Reject anything not on GitHub."""
    url = (url or "").strip()
    if not url:
        raise IngestError("No repository URL provided.")
    if url.startswith("git@github.com:"):                      # scp-style
        path = url.split(":", 1)[1]
        host = "github.com"
    else:
        parsed = urlparse(url if "//" in url else "https://" + url)
        host = (parsed.netloc or "").lower()
        path = parsed.path
    if host not in ALLOWED_HOSTS:
        raise IngestError("Only public github.com repositories are supported.")
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise IngestError("URL must point to a repository, e.g. github.com/owner/repo.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not (_OWNER_REPO_RE.match(owner) and _OWNER_REPO_RE.match(repo)):
        raise IngestError("Invalid owner or repository name.")
    return RepoRef(owner=owner, repo=repo)


def precheck_via_api(ref: RepoRef, *, timeout: int = 10) -> None:
    """Cheap gate before spending a clone: confirm public and under the size limit.
    Uses the unauthenticated GitHub API (60 req/hr/IP — add a token for volume).
    `size` is in KB and covers the git repo; treat it as the parity check for
    H2S's < 10 MB rule."""
    req = urllib.request.Request(
        ref.api_url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "promptwars-scorer"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise IngestError("Repository not found or private. Public access is mandatory.") from None
        if e.code == 403:
            return  # rate-limited; skip precheck, the post-clone backstop still applies
        raise IngestError("Could not reach GitHub to verify the repository.") from None
    except Exception:
        return  # network hiccup: don't fail the whole run on the precheck, rely on backstop
    if data.get("private"):
        raise IngestError("Repository is private. Public access is mandatory.")
    size_kb = data.get("size")
    if isinstance(size_kb, int) and size_kb * 1024 > MAX_REPO_BYTES:
        raise IngestError(f"Repository is {size_kb/1024:.1f} MB; the limit is 10 MB.")


def _tree_size_bytes(root: Path) -> int:
    """Sum working-tree size (excluding .git), never following symlinks."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            fp = Path(dirpath) / name
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def _clone(ref: RepoRef, dest: Path, home: Path) -> None:
    cmd = [
        "git", "-c", "core.symlinks=false",
        "clone", "--depth", "1", "--single-branch", "--no-tags",
        ref.clone_url, str(dest),
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),                 # isolate from the server's gitconfig/creds
        "GIT_TERMINAL_PROMPT": "0",        # never prompt -> private repos fail fast
        "GIT_CONFIG_NOSYSTEM": "1",
        "GCM_INTERACTIVE": "never",
    }
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=CLONE_TIMEOUT_SEC, check=False,
        )
    except subprocess.TimeoutExpired:
        raise IngestError("Cloning the repository timed out.") from None
    if proc.returncode != 0:
        # Don't leak stderr verbatim (may contain the URL/token); classify instead.
        err = (proc.stderr or "").lower()
        if "authentication" in err or "could not read" in err or "permission" in err:
            raise IngestError("Repository is private or inaccessible. Public access is mandatory.")
        raise IngestError("Failed to clone the repository.")


@contextmanager
def sandbox_clone(url: str):
    """Yield a local Path to a safely-cloned repo, cleaned up on exit.

        with sandbox_clone(repo_url) as repo_path:
            card = score_repo(str(repo_path), challenge=...)
    """
    ref = parse_github_url(url)
    precheck_via_api(ref)
    workdir = Path(tempfile.mkdtemp(prefix="pw_ingest_"))
    home = workdir / "home"
    dest = workdir / "repo"
    home.mkdir()
    try:
        _clone(ref, dest, home)
        size = _tree_size_bytes(dest)
        if size > MAX_REPO_BYTES:
            raise IngestError(f"Repository working tree is {size/1024/1024:.1f} MB; the limit is 10 MB.")
        yield dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- Deployed-link probe (SSRF-guarded) ---------------------------------------

def probe_url(url: str, *, timeout: int = 6) -> dict:
    """Best-effort liveness check of a user-supplied deployed link.

    Evidence only: it never changes a score. Guards against SSRF: http(s) only,
    standard ports, every resolved address must be public, and redirects are not
    followed (a redirect is reported, not chased).
    """
    import ipaddress
    import socket
    import urllib.error

    url = (url or "").strip()
    if not url:
        return {"status": "not_provided"}
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"status": "blocked", "reason": "Only http(s) links are checked."}
    if parsed.port not in (None, 80, 443):
        return {"status": "blocked", "reason": "Non-standard port."}
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError:
        return {"status": "unreachable", "reason": "Host did not resolve."}
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return {"status": "blocked", "reason": "Link resolves to a non-public address."}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "promptwars-scorer"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(2048)
            return {"status": "reachable", "http_status": resp.status,
                    "https": parsed.scheme == "https"}
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return {"status": "redirects", "http_status": e.code, "https": parsed.scheme == "https"}
        return {"status": "error", "http_status": e.code}
    except Exception:
        return {"status": "unreachable", "reason": "No response within the time limit."}
