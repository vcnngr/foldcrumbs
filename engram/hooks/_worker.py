#!/usr/bin/env python3
"""Detached distillation worker.

Spawned (fire-and-forget) by the context monitor and SessionEnd hook so the
LLM call never blocks Claude. Reads the transcript, distills durable typed
memories (dedup + index), and writes a fresh working-state handoff so a /clear
can be resumed.

Usage: _worker.py <transcript_path> <cwd> <source>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engram import distill, store  # noqa: E402
from engram.hooks._common import read_transcript_text  # noqa: E402


def main() -> int:
    # Recursion guard: never distill inside an engram-spawned `claude -p`.
    if os.environ.get("ENGRAM_DISABLE"):
        return 0
    if len(sys.argv) < 3:
        return 0
    transcript_path = sys.argv[1]
    cwd = sys.argv[2] or None
    source = sys.argv[3] if len(sys.argv) > 3 else "engram-distill"

    summary = read_transcript_text(transcript_path, max_messages=80, max_chars=8000)
    if not summary:
        return 0
    try:
        distill.distill_and_store(summary, cwd=cwd, source=source)
    except Exception:
        pass
    try:
        handoff = distill.make_handoff(summary)
        if handoff:
            store.write_handoff(handoff, cwd=cwd)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
