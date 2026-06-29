#!/usr/bin/env python3
"""PostToolUse hook — anti-rot monitor.

Estimates context size from the transcript. When it crosses the configured
threshold (default 45% of ENGRAM_CONTEXT_BUDGET) and we haven't already
checkpointed this context window, it:
  1. spawns a detached distillation of the session so far (memory persists),
  2. injects a reminder that it's a good moment to /compact or /clear.

It never forces compaction (user's choice) and never blocks. Coexists with the
existing gsd-context-monitor (separate matcher group).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engram import config  # noqa: E402
from engram.hooks._common import (  # noqa: E402
    emit_additional_context,
    estimate_tokens,
    read_hook_input,
    run,
    spawn_detached,
)
from engram.hooks._state import checkpoint_done, mark_checkpoint  # noqa: E402

EVENT = "PostToolUse"
WORKER = Path(__file__).resolve().parent / "_worker.py"


def main() -> int:
    data = read_hook_input()
    session_id = data.get("session_id") or ""
    transcript_path = data.get("transcript_path")
    cwd = data.get("cwd") or ""

    threshold = int(config.CONTEXT_BUDGET * config.CONTEXT_PCT)
    tokens = estimate_tokens(transcript_path)
    if tokens < threshold:
        return 0
    if checkpoint_done(session_id, threshold):
        return 0

    # Persist a checkpoint in the background (does not block). Skipped on a
    # read-only machine (shared store, another machine is the indexer).
    distilling = config.distill_enabled()
    if distilling:
        spawn_detached(
            [sys.executable or "python3", str(WORKER), str(transcript_path or ""),
             str(cwd), "engram-checkpoint"]
        )
    mark_checkpoint(session_id, tokens)

    pct = int(tokens / config.CONTEXT_BUDGET * 100)
    saved = (
        "Memory checkpoint saved in the background — durable decisions persist "
        "across sessions. "
        if distilling
        else ""
    )
    emit_additional_context(
        EVENT,
        f"🧠 engram: context ~{pct}% (~{tokens} tok). {saved}This is a good "
        "moment to /compact or /clear to avoid context rot; nothing will be lost.",
    )
    return 0


if __name__ == "__main__":
    run(main)
