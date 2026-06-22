"""Paths and environment configuration for engram.

Memory lives in Claude Code's per-project directory:
    ~/.claude/projects/<encoded-cwd>/memory/
where <encoded-cwd> is the absolute cwd with every "/" replaced by "-".
This matches the convention already used by the host (see CLAUDE.md).

Everything is overridable via env so the same code serves the CLI (uses
os.getcwd()) and the hooks (use the cwd passed in the hook payload).
"""

from __future__ import annotations

import os
from pathlib import Path

# --- LLM (distillation only; recall never touches the LLM) ------------------
LLM_ENDPOINT = os.environ.get("ENGRAM_LLM_ENDPOINT", "http://localhost:8081")
LLM_MODEL = os.environ.get("ENGRAM_LLM_MODEL", "gemma-4-26b-a4b")
LLM_API_KEY = os.environ.get("ENGRAM_LLM_API_KEY", "")
LLM_TIMEOUT = float(os.environ.get("ENGRAM_LLM_TIMEOUT", "120"))

# --- Anti-rot monitor -------------------------------------------------------
CONTEXT_BUDGET = int(os.environ.get("ENGRAM_CONTEXT_BUDGET", "200000"))
CONTEXT_PCT = float(os.environ.get("ENGRAM_CONTEXT_PCT", "0.45"))

# --- Distillation gate ------------------------------------------------------
MIN_CONFIDENCE = float(os.environ.get("ENGRAM_MIN_CONFIDENCE", "0.7"))

# Ephemeral per-session state (checkpoint flags). Not the memory store.
STATE_DIR = Path(os.environ.get("ENGRAM_STATE_DIR", str(Path.home() / ".engram")))

INDEX_NAME = "MEMORY.md"


def encode_cwd(cwd: str | os.PathLike[str]) -> str:
    """Encode an absolute path the way Claude Code names project dirs."""
    return str(Path(cwd).resolve()).replace("/", "-")


def memory_dir(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the memory directory for a given working directory.

    Order: explicit ENGRAM_DIR env wins; otherwise derive from cwd.
    """
    override = os.environ.get("ENGRAM_DIR")
    if override:
        return Path(override).expanduser()
    cwd = cwd or os.getcwd()
    return (
        Path.home()
        / ".claude"
        / "projects"
        / encode_cwd(cwd)
        / "memory"
    )


def index_path(cwd: str | os.PathLike[str] | None = None) -> Path:
    return memory_dir(cwd) / INDEX_NAME
