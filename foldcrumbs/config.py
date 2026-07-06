"""Paths and environment configuration for engram.

Memory lives in Claude Code's per-project directory:
    ~/.claude/projects/<encoded-cwd>/memory/
where <encoded-cwd> is the absolute cwd with every "/" replaced by "-".
This matches the convention already used by the host (see CLAUDE.md).

Everything is overridable via env so the same code serves the CLI (uses
os.getcwd()) and the hooks (use the cwd passed in the hook payload). Settings
that a single machine should own (which LLM backend, its binary/endpoint) also
fall back to small files in the machine-local state dir (~/.engram), written by
`engram install`/`engram backend` — see ``_local_override``.
"""

from __future__ import annotations

import os
from pathlib import Path

# Ephemeral per-machine state (backend choice, checkpoint flags). Resolved first
# so the LLM constants below can fall back to it. NOT the memory store, and (when
# the store is shared via Syncthing) deliberately NOT synced — it's how one
# machine differs from the others.
STATE_DIR = Path(os.environ.get("ENGRAM_STATE_DIR", str(Path.home() / ".engram")))


def _local_override(name: str) -> str | None:
    """Read a machine-local override from the (non-synced) state dir.

    Lets a single machine differ from a shared, synced settings.json — e.g. one
    box with no local LLM selects the claude-cli backend here without forcing it
    on the machines that share its memory store.
    """
    try:
        p = STATE_DIR / name
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


# --- LLM (distillation only; recall never touches the LLM) ------------------
# Order for each: env var > machine-local state file > built-in default.
LLM_ENDPOINT = (os.environ.get("ENGRAM_LLM_ENDPOINT")
                or _local_override("llm-endpoint") or "http://localhost:8081")
LLM_MODEL = (os.environ.get("ENGRAM_LLM_MODEL")
             or _local_override("llm-model") or "gemma-4-26b-a4b-it")
LLM_API_KEY = os.environ.get("ENGRAM_LLM_API_KEY", "")
LLM_TIMEOUT = float(os.environ.get("ENGRAM_LLM_TIMEOUT", "120"))
# Master kill-switch. engram sets this in the env of any `claude -p` subprocess
# it spawns so the nested headless session's own hooks no-op and can't trigger
# another distillation — i.e. it stops claude-cli distillation from recursing.
DISABLED = bool(os.environ.get("ENGRAM_DISABLE"))
# Request OpenAI structured output (response_format json_schema) for distill.
# Best-effort: servers that ignore it still work (tolerant parser). Default on.
LLM_JSON_SCHEMA = os.environ.get("ENGRAM_LLM_JSON_SCHEMA", "1") not in ("0", "false", "")

# --- Anti-rot monitor -------------------------------------------------------
CONTEXT_BUDGET = int(os.environ.get("ENGRAM_CONTEXT_BUDGET", "200000"))
CONTEXT_PCT = float(os.environ.get("ENGRAM_CONTEXT_PCT", "0.45"))

# --- Distillation gate ------------------------------------------------------
MIN_CONFIDENCE = float(os.environ.get("ENGRAM_MIN_CONFIDENCE", "0.7"))

# Recognised distillation backends. "none" (a.k.a. heuristic-only) skips the LLM
# entirely and always falls through to the keyword heuristic — the last rung for
# a machine that can't host or reach any model.
BACKENDS = ("claude-cli", "codex", "openai", "none")
_NO_LLM_BACKENDS = ("none", "heuristic", "off")


def distill_enabled() -> bool:
    """Whether this machine should distill/write memories.

    Off when ENGRAM_NO_DISTILL is set or a ``no-distill`` marker exists in the
    state dir. The state dir is machine-local (a sibling of ~/.claude, not under
    it), so when the memory store is shared across machines — e.g. via Syncthing
    — one machine with a local LLM can be the sole indexer while the others stay
    read-only consumers (recall + index injection still work; only writing is
    disabled). Evaluated live so dropping/removing the marker takes effect at
    once.
    """
    if os.environ.get("ENGRAM_NO_DISTILL"):
        return False
    return not (STATE_DIR / "no-distill").exists()


def log_event(msg: str) -> None:
    """Append a line to the machine-local engram log (best-effort, never raises).

    Background hooks/workers have nowhere visible to print; this gives auto-prune
    and index self-heal an audit trail in ~/.engram/engram.log."""
    try:
        from datetime import datetime, timezone
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (STATE_DIR / "engram.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg.rstrip()}\n")
    except OSError:
        pass


def auto_prune_enabled() -> bool:
    """Auto-prune obvious artifact pollution after distill. On by default; off
    when ENGRAM_NO_AUTO_PRUNE is set."""
    return not os.environ.get("ENGRAM_NO_AUTO_PRUNE")


def llm_backend() -> str:
    """Distillation backend: env > machine-local file > "openai" default.

    "openai" = HTTP to LLM_ENDPOINT; "claude-cli" = shell out to the Claude CLI
    in print mode; "codex" = shell out to the Codex CLI in exec mode; "none"
    (or "heuristic"/"off") = no LLM, keyword heuristic only. The two CLI backends
    use the tool's own login — no endpoint, no API key.
    """
    val = os.environ.get("ENGRAM_LLM_BACKEND") or _local_override("llm-backend") or "openai"
    return val.strip().lower()


def claude_bin() -> str:
    """Path/name of the Claude CLI for the claude-cli backend.

    Prefer an absolute path (set via env or the ``claude-bin`` state file): hooks
    run without the user's interactive shell, so PATH may be minimal.
    """
    return os.environ.get("ENGRAM_CLAUDE_BIN") or _local_override("claude-bin") or "claude"


def codex_bin() -> str:
    """Path/name of the Codex CLI for the codex backend.

    Prefer an absolute path (set via env or the ``codex-bin`` state file): hooks
    run without the user's interactive shell, so PATH may be minimal.
    """
    return os.environ.get("ENGRAM_CODEX_BIN") or _local_override("codex-bin") or "codex"

INDEX_NAME = "MEMORY.md"
# Live working-state snapshot (overwritten each checkpoint), for resuming after
# a /clear. Distinct from durable memories; never indexed as one.
HANDOFF_NAME = "HANDOFF.md"


def claude_config_dir() -> Path:
    """Claude Code's config root for the *current* instance.

    Honors CLAUDE_CONFIG_DIR so engram follows the same per-instance dirs used by
    aliases like ``claude-work``/``claude-peo``/``claude-3sez`` (each of which
    runs ``CLAUDE_CONFIG_DIR=~/.claude-<x> claude``). When unset, falls back to
    the default ~/.claude. This keeps each instance's memory namespaced under its
    own config dir instead of bleeding into the personal store.
    """
    return Path(
        os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    ).expanduser()


def encode_cwd(cwd: str | os.PathLike[str]) -> str:
    """Encode an absolute path the way Claude Code names project dirs."""
    return str(Path(cwd).resolve()).replace("/", "-")


def memory_dir(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the memory directory for a given working directory.

    Order: explicit ENGRAM_DIR env wins; otherwise derive from cwd under the
    current instance's Claude config dir (CLAUDE_CONFIG_DIR-aware).
    """
    override = os.environ.get("ENGRAM_DIR")
    if override:
        return Path(override).expanduser()
    cwd = cwd or os.getcwd()
    return (
        claude_config_dir()
        / "projects"
        / encode_cwd(cwd)
        / "memory"
    )


def index_path(cwd: str | os.PathLike[str] | None = None) -> Path:
    return memory_dir(cwd) / INDEX_NAME


def handoff_path(cwd: str | os.PathLike[str] | None = None) -> Path:
    return memory_dir(cwd) / HANDOFF_NAME
