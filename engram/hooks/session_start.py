#!/usr/bin/env python3
"""SessionStart hook — inject the MEMORY.md index so Claude reopens informed.

No LLM, no network: reads the curated index for the current project and emits
it as additionalContext. Resets the per-session anti-rot checkpoint flag when
the session starts fresh after a clear/compact.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engram import config  # noqa: E402
from engram.hooks._common import (  # noqa: E402
    emit_additional_context,
    read_hook_input,
    run,
)
from engram.hooks._state import clear_checkpoint  # noqa: E402

EVENT = "SessionStart"


def main() -> int:
    data = read_hook_input()
    cwd = data.get("cwd")
    session_id = data.get("session_id")
    source = (data.get("source") or "").lower()

    # A clear/compact starts a fresh context window — allow a new checkpoint.
    if source in ("clear", "compact") and session_id:
        clear_checkpoint(session_id)

    idx = config.index_path(cwd)
    if not idx.exists():
        return 0
    try:
        body = idx.read_text(encoding="utf-8").strip()
    except Exception:
        return 0
    if not body:
        return 0

    block = (
        "<engram-index>\n"
        "Persistent project memory (from previous sessions). Honour it; do not "
        "re-ask what is already recorded. To recall detail, read the linked "
        "file or grep the memory folder:\n"
        f"{config.memory_dir(cwd)}\n\n"
        f"{body}\n"
        "</engram-index>"
    )
    emit_additional_context(EVENT, block)
    return 0


if __name__ == "__main__":
    run(main)
