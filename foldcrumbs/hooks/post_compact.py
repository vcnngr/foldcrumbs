#!/usr/bin/env python3
"""PostCompact hook — re-inject the memory index after compaction.

Compaction replaces the live context with a summary; this re-primes the new
window with the curated MEMORY.md so durable knowledge survives the reset.
(PreCompact cannot inject context — only PostCompact can.) Also clears the
checkpoint flag so the monitor can fire again in the fresh window.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engram import config, store  # noqa: E402
from engram.hooks._common import (  # noqa: E402
    emit_additional_context,
    read_hook_input,
    run,
)
from engram.hooks._state import clear_checkpoint  # noqa: E402

EVENT = "PostCompact"


def main() -> int:
    data = read_hook_input()
    cwd = data.get("cwd")
    session_id = data.get("session_id")
    if session_id:
        clear_checkpoint(session_id)

    parts: list[str] = []
    idx = config.index_path(cwd)
    if idx.exists():
        try:
            body = idx.read_text(encoding="utf-8").strip()
        except Exception:
            body = ""
        if body:
            parts.append(
                "<engram-index>\nProject memory (re-injected after compaction). "
                "Honour it; do not re-ask what is recorded:\n\n"
                f"{body}\n</engram-index>"
            )

    handoff = store.read_handoff(cwd)
    if handoff:
        parts.append(
            "<engram-handoff>\nWhere you were before compaction — resume from "
            f"here:\n\n{handoff}\n</engram-handoff>"
        )

    if parts:
        emit_additional_context(EVENT, "\n\n".join(parts))
    return 0


if __name__ == "__main__":
    run(main)
