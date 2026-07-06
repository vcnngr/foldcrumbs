"""Per-session ephemeral state: anti-rot checkpoint flags.

Tiny JSON files under config.STATE_DIR keyed by session id. Used to avoid
re-firing the context-monitor checkpoint every tool call once we've already
checkpointed the current context window. Not the memory store.
"""

from __future__ import annotations

import json

from .. import config


def _path(session_id: str):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return config.STATE_DIR / f"state-{safe}.json"


def _read(session_id: str) -> dict:
    p = _path(session_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(session_id: str, data: dict) -> None:
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _path(session_id).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def checkpoint_done(session_id: str, threshold_tokens: int) -> bool:
    """True if we already checkpointed at/above this token threshold."""
    if not session_id:
        return False
    return int(_read(session_id).get("last_checkpoint_tokens", 0)) >= threshold_tokens


def mark_checkpoint(session_id: str, tokens: int) -> None:
    if not session_id:
        return
    data = _read(session_id)
    data["last_checkpoint_tokens"] = int(tokens)
    _write(session_id, data)


def clear_checkpoint(session_id: str) -> None:
    """Reset after a clear/compact so a new checkpoint can fire."""
    if not session_id:
        return
    _write(session_id, {})
