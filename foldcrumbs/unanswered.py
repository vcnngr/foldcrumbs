"""Unanswered-query log: which recalls found nothing, so the gaps are visible.

Observation only. A recall that returns no served memory appends one line to
``STATE_DIR/unanswered.jsonl``. Nothing reads this to change a result, and
nothing is ever deleted by it: it is a list of questions the store could not
answer, to be read by a human or an agent deciding what to write next.

Design choices, each for a reason:

* Only the user-facing recall path logs (MCP ``recall``). Internal callers of
  the search (supersede, ask) would otherwise fill the log with false gaps.
* Only the query text and a timestamp are kept. No result, no memory body, so
  the log cannot leak content that was never served.
* Append-only JSON lines. Writes are best-effort: a logging failure must never
  break a recall, so any OSError is swallowed here, and only here.
* Repeated identical queries are kept, not collapsed: frequency is the signal
  (a question asked ten times with no answer is the most useful gap).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import config

FILENAME = "unanswered.jsonl"
MAX_QUERY_CHARS = 300


def log_path() -> Path:
    return Path(config.STATE_DIR) / FILENAME


def record(query: str, *, now: datetime | None = None) -> bool:
    """Append one unanswered query. Returns True if written, False otherwise.

    Empty or whitespace-only queries are not questions, so they are skipped.
    """
    text = (query or "").strip()
    if not text:
        return False
    when = now or datetime.now(timezone.utc)
    line = json.dumps(
        {"at": when.isoformat(timespec="seconds"), "query": text[:MAX_QUERY_CHARS]},
        ensure_ascii=False,
    )
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return False
    return True


def read(limit: int | None = None) -> list[dict]:
    """Most recent entries first. Malformed lines are skipped, never fatal."""
    path = log_path()
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in raw:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("query"), str):
            out.append(obj)
    out.reverse()
    return out[:limit] if limit else out


def top_gaps(n: int = 10) -> list[tuple[str, int]]:
    """Most frequent unanswered queries, case-insensitive, as (query, count)."""
    counts: dict[str, int] = {}
    shown: dict[str, str] = {}
    for obj in read():
        key = obj["query"].casefold()
        counts[key] = counts.get(key, 0) + 1
        shown.setdefault(key, obj["query"])
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [(shown[k], c) for k, c in ranked[:n]]
