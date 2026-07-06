#!/usr/bin/env python3
"""SessionEnd hook — final distillation sweep.

When a session ends, spawn a detached distillation of the whole session so any
durable decisions made late are captured. Fire-and-forget: never blocks, never
fails the editor. (We deliberately do NOT distill on every Stop — that would
hammer the LLM each turn; the 45% monitor + this end-of-session sweep cover it.)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from foldcrumbs import config  # noqa: E402
from foldcrumbs.hooks._common import (  # noqa: E402
    read_hook_input,
    run,
    spawn_detached,
)

WORKER = Path(__file__).resolve().parent / "_worker.py"


def main() -> int:
    data = read_hook_input()
    transcript_path = data.get("transcript_path")
    cwd = data.get("cwd") or ""
    if not transcript_path:
        return 0
    if not config.distill_enabled():  # read-only machine on a shared store
        return 0
    spawn_detached(
        [sys.executable or "python3", str(WORKER), str(transcript_path),
         str(cwd), "foldcrumbs-session-end"]
    )
    return 0


if __name__ == "__main__":
    run(main)
