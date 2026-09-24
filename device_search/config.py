"""
config.py — setup modes, what gets indexed, and the secret patterns.

⚠️ THE TWO MODES ARE THE WHOLE PRIVACY STORY, so they are defined in one place and printed
loudly at setup time. A search index is a COPY OF YOUR FILES in a second location. That is the
point, and it is also the risk: anything you index is now readable by anything that can read
the index.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()

# ⚠️ THE INDEX LIVES OUTSIDE THE REPO. Two reasons, both load-bearing:
#   1. SIZE — a home-directory index is hundreds of MB to a few GB. Never in git.
#   2. PRIVACY — the index contains the TEXT of your files. If it lived in a repo you might
#      commit it, and then it is published. `.gitignore` is a backstop, not the defence.
INDEX_DIR = HOME / ".device-search"
DB_PATH = INDEX_DIR / "index.db"
VEC_PATH = INDEX_DIR / "vectors.npy"
META_PATH = INDEX_DIR / "meta.json"


# =============================================================================
# THE TWO SETUP MODES
# =============================================================================
@dataclass
class Mode:
    name: str
    description: str
    roots: list[Path] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)


def curated_roots() -> list[Path]:
    """Mode A — the folders most people actually mean by 'my stuff'."""
    candidates = [
        HOME / "Documents", HOME / "Desktop", HOME / "Downloads",
        HOME / "WebstormProjects", HOME / "IdeaProjects", HOME / "Projects",
    ]
    return [p for p in candidates if p.is_dir()]


MODES = {
    "curated": Mode(
        name="curated",
        description="Documents, Desktop, Downloads and your code folders.",
        roots=curated_roots(),
        warns=["Nothing outside these folders is indexed."],
    ),
    "everything": Mode(
        name="everything",
        description="YOUR ENTIRE HOME DIRECTORY, NO EXCLUSIONS.",
        roots=[HOME],
        warns=[
            "⚠️ THIS INCLUDES .ssh, .aws, .env FILES, BROWSER PROFILES AND KEYCHAINS.",
            "⚠️ The index will contain the TEXT of those files in ~/.device-search.",
            "⚠️ Anything that can read that folder can read your credentials.",
            "⚠️ If you later enable --llm, matched content can be sent to an API.",
            "   (A secret interlock blocks that for recognised key formats — see secrets.py,",
            "    but do not rely on a regex to be complete.)",
        ],
    ),
}


# =============================================================================
# TECHNICAL LIMITS, NOT PRIVACY EXCLUSIONS
# =============================================================================
# ⚠️ THE DIFFERENCE MATTERS AND THE USER ASKED FOR "NO EXCLUSIONS". These are not policy
# choices — they are the boundaries of what a text index can hold:
#   - a 4 GB disk image has no text to index and would blow up the store
#   - a binary file decoded as utf-8 produces megabytes of replacement characters
#   - a symlink loop never terminates
# So they are enforced in BOTH modes, and reported as "skipped: N", never silently.
MAX_FILE_BYTES = 2 * 1024 * 1024          # 2 MB of text per file is plenty for search
MAX_LINE_BYTES = 4000                     # minified JS / single-line dumps
SKIP_DIR_NAMES = {
    # Version-control internals: thousands of compressed binary blobs, no searchable text.
    ".git", ".hg", ".svn",
    # Python bytecode caches: regenerated, binary, enormous count.
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # ⚠️ These are dependency trees. They are TEXT and they are huge (often 100k+ files each).
    # They are NOT excluded for privacy — they are excluded because they drown real results.
    # `--include-deps` turns them back on, because sometimes you DO want to search them.
    "node_modules", ".venv", "venv", "site-packages",
}
SKIP_EXTS = {
    # Binary media
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".svgz", ".heic",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac", ".m4a", ".aac", ".webm",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".dmg", ".pkg", ".iso",
    # Compiled / binary
    ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".class", ".jar",
    ".bin", ".dat", ".db", ".sqlite", ".sqlite3", ".mdb", ".pack", ".idx",
    # Fonts and design
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".psd", ".ai", ".sketch", ".fig",
    # Model weights
    ".pt", ".pth", ".safetensors", ".ckpt", ".onnx", ".h5", ".pb", ".gguf", ".npy", ".npz",
}

# Code and prose we DO want. Extension -> language label, for display and weighting.
TEXT_EXTS = {
    ".py": "python", ".pyi": "python", ".ipynb": "notebook",
    ".js": "js", ".jsx": "js", ".ts": "ts", ".tsx": "ts", ".mjs": "js", ".cjs": "js",
    ".java": "java", ".kt": "kotlin", ".scala": "scala", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".m": "objc", ".dart": "dart",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".fish": "shell", ".ps1": "powershell",
    ".sql": "sql", ".graphql": "graphql", ".proto": "proto",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".xml": "xml", ".json": "json", ".jsonl": "jsonl", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "conf", ".properties": "props",
    ".env": "env", ".envrc": "env", ".lock": "lock",
    ".md": "markdown", ".mdx": "markdown", ".rst": "rst", ".txt": "text", ".log": "log",
    ".csv": "csv", ".tsv": "tsv", ".tex": "latex", ".org": "org", ".adoc": "asciidoc",
    ".dockerfile": "docker", ".tf": "terraform", ".gradle": "gradle", ".cmake": "cmake",
}
# ⚠️ FILES WITH NO EXTENSION BUT ALWAYS TEXT. `Dockerfile`, `Makefile`, `.env`, `LICENSE` etc.
# Missing these loses exactly the files people search for most.
TEXT_NAMES = {
    "dockerfile", "makefile", "cmakelists.txt", "rakefile", "gemfile", "procfile",
    "license", "licence", "notice", "readme", "changelog", "authors", "contributing",
    "requirements.txt", "pipfile", "poetry.lock", "package.json", "tsconfig.json",
    ".gitignore", ".dockerignore", ".npmrc", ".editorconfig", ".prettierrc",
    ".bashrc", ".zshrc", ".bash_profile", ".profile", ".env", ".envrc", ".gitconfig",
}


# =============================================================================
# SECRET PATTERNS
# =============================================================================
# ⚠️⚠️ THIS IS AN INTERLOCK, NOT A GUARANTEE, AND THE DISTINCTION IS THE POINT.
# A regex cannot know that a random 40-character string in a notes file is a password. What it
# CAN do is stop the obvious, catastrophic cases — an RSA private key, an AWS key, a `.env`
# line — from being sent to a third-party API by an optional feature.
#
# ⚠️ Saying "it detects secrets" would be a lie. Saying "it blocks the recognised formats, and
# treats everything it cannot recognise as your responsibility" is the truth, and it is written
# here so nobody has to infer which one is meant.
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "huggingface_token": re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    "env_assignment": re.compile(
        r"(?i)\b[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*[:=]\s*\S+"),
    "connection_string": re.compile(r"(?i)\b(?:postgres|mysql|mongodb|redis|amqp)://[^\s\"']+"),
}

# ⚠️ These are the files a secrets audit should always look at FIRST, regardless of content.
SENSITIVE_PATH_HINTS = (
    ".ssh", ".aws", ".gnupg", "id_rsa", "id_ed25519", "credentials", ".netrc",
    ".env", "secrets", ".npmrc", ".pypirc", ".docker/config.json", "keychain",
    "login.keychain", ".git-credentials", "wp-config.php",
)


def is_text_file(path: Path, size: int) -> tuple[bool, str]:
    """Decide whether to index a file, and why not. ⚠️ Returns the REASON so stats can report it."""
    if size > MAX_FILE_BYTES:
        return False, "too_large"
    name = path.name.lower()
    ext = path.suffix.lower()
    if ext in SKIP_EXTS:
        return False, "binary_ext"
    if ext in TEXT_EXTS:
        return True, "ext"
    if name in TEXT_NAMES or name.startswith(".env"):
        return True, "known_name"
    # ⚠️ NO EXTENSION AND NOT A KNOWN NAME: sniff the first bytes for NUL. A file with a null
    # byte in its first 8 KB is not text, whatever it is called. This is how `Makefile` and
    # `README` get picked up without a list of every possible name.
    if ext == "":
        try:
            with open(path, "rb") as fh:
                return (b"\x00" not in fh.read(8192)), "sniffed"
        except OSError:
            return False, "unreadable"
    return False, "unknown_ext"


def lang_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return TEXT_EXTS[ext]
    name = path.name.lower()
    if name in ("dockerfile", "makefile", "cmakelists.txt"):
        return {"dockerfile": "docker", "makefile": "make", "cmakelists.txt": "cmake"}[name]
    if name.startswith(".env"):
        return "env"
    return "text"


def redact(text: str, keep: int = 4) -> str:
    """Mask a matched secret, keeping a short prefix so the user can identify it."""
    if len(text) <= keep:
        return "*" * len(text)
    return text[:keep] + "*" * min(28, len(text) - keep)


def find_secrets(text: str, limit: int = 20) -> list[tuple[str, str]]:
    """Return [(kind, redacted_sample)] — ⚠️ NEVER the full secret, so this is safe to print."""
    found: list[tuple[str, str]] = []
    for kind, rx in SECRET_PATTERNS.items():
        for m in rx.finditer(text):
            found.append((kind, redact(m.group(0))))
            if len(found) >= limit:
                return found
    return found


def ensure_index_dir() -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(INDEX_DIR, 0o700)   # ⚠️ owner-only. The index holds file contents.
