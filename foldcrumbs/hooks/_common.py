"""Shared plumbing for engram's Claude Code lifecycle hooks.

Design rules (these run on the developer's hot path):
* Never break Claude Code — any failure exits 0 silently (``run``).
* Stay fast — SessionStart/PostCompact/PostToolUse do no LLM work inline;
  distillation is spawned detached (``spawn_detached``).
* Be schema-tolerant — the transcript JSONL format is not officially pinned.

The schema-tolerant transcript reader follows the approach used by memanto's
hooks (MIT); the token estimator and detached-spawn helper are engram's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def run(main: Callable[[], int]) -> None:
    """Execute a hook entry point under the never-break-Claude contract."""
    # Master kill-switch: a `claude -p` distillation subprocess sets this so the
    # nested headless session's hooks no-op — otherwise claude-cli distillation
    # would recurse (session_end → worker → claude -p → session_end → ...).
    if os.environ.get("ENGRAM_DISABLE"):
        raise SystemExit(0)
    try:
        code = main()
    except Exception:
        code = 0
    raise SystemExit(code)


def read_hook_input() -> dict[str, Any]:
    """Parse the hook's stdin JSON. Returns {} if absent/malformed."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def emit_additional_context(event_name: str, context: str) -> None:
    """Emit the JSON that injects ``context`` for Claude to read."""
    if not context:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def spawn_detached(args: list[str]) -> None:
    """Start a fully detached background process (distillation).

    Robust regardless of whether the hook is registered ``async``: we fork a
    new session, redirect all stdio to /dev/null, and return immediately.
    """
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        subprocess.Popen(  # noqa: S603
            args,
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Transcript reading (schema-tolerant) — approach follows memanto
# --------------------------------------------------------------------------- #


def read_transcript_text(
    transcript_path: str | None,
    max_messages: int = 60,
    max_chars: int = 8000,
) -> str:
    """Plain-text rendering of the most recent transcript messages (tail)."""
    if not transcript_path:
        return ""
    path = Path(transcript_path)
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""

    pieces: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role, text = _extract_role_text(entry)
        if not text:
            continue
        pieces.append(f"{role}: {text}" if role else text)

    rendered = "\n".join(pieces[-max_messages:])
    return rendered[-max_chars:]


def estimate_tokens(transcript_path: str | None) -> int:
    """Rough context-size estimate: total characters / 3.5.

    Reads the whole transcript (not just the tail) since we want the full
    conversation size, not a sample. ~3.5 chars/token is a conservative mean
    for code+prose. Returns 0 on any problem (monitor then no-ops).
    """
    if not transcript_path:
        return 0
    path = Path(transcript_path)
    if not path.exists():
        return 0
    try:
        total = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _, text = _extract_role_text(entry)
                total += len(text)
        return int(total / 3.5)
    except Exception:
        return 0


def _extract_role_text(entry: Any) -> tuple[str | None, str]:
    if not isinstance(entry, dict):
        return None, ""
    message = entry.get("message", entry)
    if isinstance(message, dict):
        role = message.get("role") or entry.get("role") or entry.get("type")
        content = message.get("content")
    else:
        role = entry.get("role") or entry.get("type")
        content = entry.get("content")
    return role, _flatten_content(content)


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                if block.get("type") in (None, "text") and block.get("text"):
                    out.append(str(block["text"]))
        return " ".join(s.strip() for s in out if s.strip())
    return ""
