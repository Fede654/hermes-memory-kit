#!/usr/bin/env python3
"""Corpus policy for HMK: file blocking + content-level secret scanning.

Design contract (v3.9.0):
- Loads policy JSON from HMK_CORPUS_POLICY env var or falls back to
  the shipped default (scripts/default_corpus_policy.json).
- Fail-closed: an unreadable policy file causes ingest to refuse.
- Two layers:
  1. File-level blocking: NEVER-TOUCH names / globs / patterns.
  2. Content-level secret scan: structural credential patterns.
- Content scan returns a reason string so the caller can flag
  embed_disabled without blocking storage (lexical retrieval still works).
- Extension classification provides source_kind hints for selective
  embedding (code/config = embed_disabled by policy).

References:
  ~/Projects/collective-memory/config/corpus_policy.example.json
  ~/Projects/collective-memory/tools/leak_check.py (rules 4-5)
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Extension classification for selective embedding (t_c2f96fa4)
# ---------------------------------------------------------------------------

CODE_EXTENSIONS = frozenset(
    {".py", ".sh", ".bash", ".zsh", ".js", ".jsx", ".ts", ".tsx",
     ".vue", ".svelte", ".c", ".cc", ".cpp", ".h", ".hpp", ".cu", ".cuh"}
)

CONFIG_EXTENSIONS = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".css", ".html"}
)


def classify_source_kind(file_path: str | Path) -> str:
    """Return 'code', 'config', or 'file' based on extension.

    Used by add_file / ingest_any to set books.source_kind; the
    selective-embedding layer then disables embeddings for code/config.
    """
    suffix = Path(file_path).suffix.lower()
    if suffix in CODE_EXTENSIONS:
        return "code"
    if suffix in CONFIG_EXTENSIONS:
        return "config"
    return "file"


# ---------------------------------------------------------------------------
# Structural secret patterns (content-level scan)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # Specific private key types first (before the generic PEM block)
    (re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----"), "OpenSSH private key"),
    (re.compile(r"-----BEGIN EC PRIVATE KEY-----"), "EC private key"),
    (re.compile(r"-----BEGIN RSA PRIVATE KEY-----"), "RSA private key"),
    (re.compile(r"-----BEGIN DSA PRIVATE KEY-----"), "DSA private key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block (PEM)"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key (AKIA...)"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "OpenAI-style sk- key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "GitHub classic token (ghp_)"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "GitHub fine-grained PAT"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"), "Stripe live key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "Google API key"),
    (re.compile(r"\bglpat-[0-9A-Za-z_-]{16,}\b"), "GitLab access token"),
    (re.compile(r"\bnpm_[0-9A-Za-z]{20,}\b"), "npm access token"),
    # Common credential assignment forms, case-insensitive.
    (
        re.compile(
            r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
            r"secret[_-]?key|password)\s*[:=]\s*['\"][^'\"]{16,}['\"]",
            re.IGNORECASE,
        ),
        "credential assignment",
    ),
    # JWT-shaped tokens: three base64url parts separated by dots
    (re.compile(r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}"),
     "JWT-shaped token"),
    # Bearer tokens in Authorization headers
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Authorization: Bearer token"),
]

# Secret scanning is fail-closed for every byte length. Callers that only need
# an embedding-size heuristic must apply that separately.
_MIN_CONTENT_BYTES = 0


def scan_content_for_secrets(
    text: str, minimum_bytes: int = _MIN_CONTENT_BYTES
) -> Optional[str]:
    """Scan text for structural credential patterns.

    Returns the first matched reason string, or None if clean.
    Multiple matches collapse — first hit wins so the caller sees at
    least one concrete reason in embed_disable_reason.
    """
    if len(text) < minimum_bytes:
        return None
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(text):
            return reason
    return None


# ---------------------------------------------------------------------------
# File-name blocking (NEVER-TOUCH list)
# ---------------------------------------------------------------------------

_NEVER_TOUCH_NAMES = frozenset({
    ".env", ".htpasswd", ".git-credentials", ".netrc",
    ".pgpass", ".npmrc", ".pypirc", ".dockercfg",
})

_NEVER_TOUCH_GLOBS = (
    "*.key", "*.pem", "id_rsa*", "id_ed25519*",
    "*.tfstate", "*.kubeconfig",
)

_NAME_CONTAINS_BLOCKED = ("secret", "credential", "password")


def should_block_file(file_path: str | Path) -> Tuple[bool, str]:
    """Check whether a file should be blocked from ingestion entirely.

    Returns (blocked: bool, reason: str) where reason cites the matched
    rule.  Reason is empty when blocked=False.
    """
    p = Path(file_path)
    fname = p.name
    fname_lower = fname.lower()

    # 1) exact name match
    if fname in _NEVER_TOUCH_NAMES:
        return True, f"never-touch filename: {fname}"

    # 2) glob match
    for glob_pat in _NEVER_TOUCH_GLOBS:
        if fnmatch.fnmatch(fname, glob_pat):
            return True, f"never-touch glob: {glob_pat} (matched {fname})"

    # 3) name-contains (case-insensitive)
    for term in _NAME_CONTAINS_BLOCKED:
        if term in fname_lower:
            return True, f"filename contains '{term}': {fname}"

    # A harmless basename under a credential-bearing directory is still part
    # of a protected source tree.
    for parent in p.parts[:-1]:
        parent_lower = parent.lower()
        if parent_lower in {name.lower() for name in _NEVER_TOUCH_NAMES}:
            return True, f"never-touch parent directory: {parent}"
        for term in _NAME_CONTAINS_BLOCKED:
            if term in parent_lower:
                return True, f"parent directory contains '{term}': {parent}"

    return False, ""


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

_DEFAULT_POLICY_PATH = SCRIPT_DIR / "default_corpus_policy.json"
_policy_cache: Optional[Dict] = None
_policy_cache_path: Optional[Path] = None


def load_policy() -> dict:
    """Return the active corpus policy dict.

    Cached after first load.  Fail-closed: if HMK_CORPUS_POLICY points
    at an unreadable file, this raises SystemExit(2).
    """
    policy_env = os.environ.get("HMK_CORPUS_POLICY")
    if policy_env:
        policy_path = Path(policy_env).expanduser().resolve()
    else:
        policy_path = _DEFAULT_POLICY_PATH.resolve()

    global _policy_cache, _policy_cache_path
    if _policy_cache is not None and _policy_cache_path == policy_path:
        return _policy_cache  # type: ignore[return-value]

    try:
        _policy_cache = json.loads(policy_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError) as exc:
        raise SystemExit(
            f"ERROR: corpus policy not readable at {policy_path}: {exc}\n"
            f"  Set HMK_CORPUS_POLICY to a valid JSON file or ensure the\n"
            f"  default policy is present at {_DEFAULT_POLICY_PATH}."
        )
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"ERROR: corpus policy at {policy_path} is not valid JSON: {exc}"
        )

    assert _policy_cache is not None, "policy load succeeded but cache is None"
    _policy_cache_path = policy_path
    return _policy_cache
