"""File-backed memory store + MEMORY.md index.

One Markdown file per memory in the project memory dir. Retrieval at runtime
is the agent's own grep over this folder; this module handles writing,
loading, dedup and index regeneration. Pure stdlib (difflib for fuzzy match).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from difflib import SequenceMatcher
from pathlib import Path

from . import config
from .schema import MemoryRecord

# Index render order: hard rules first, soft context last (mirrors profile.py).
_TYPE_ORDER = [
    "instruction",
    "decision",
    "commitment",
    "preference",
    "error",
    "learning",
    "fact",
    "goal",
    "observation",
    "relationship",
    "artifact",
    "event",
    "context",
    # legacy host types
    "project",
    "feedback",
    "reference",
    "session",
    "user",
    "incident",
]

_TYPE_LABEL = {
    "instruction": "Rules",
    "decision": "Decisions",
    "commitment": "Commitments",
    "preference": "Preferences",
    "error": "Failure modes",
    "learning": "Lessons",
    "fact": "Facts",
    "goal": "Goals",
    "observation": "Observations",
    "relationship": "Relationships",
    "artifact": "Artifacts",
    "event": "Events",
    "context": "Background",
    "project": "Projects",
    "feedback": "Feedback",
    "reference": "References",
    "session": "Sessions",
    "user": "User",
    "incident": "Incidents",
}

_DEDUP_THRESHOLD = 0.85  # title+content similarity above which two memories match


def _ensure_dir(cwd: str | os.PathLike[str] | None) -> Path:
    d = config.memory_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_memories(cwd: str | os.PathLike[str] | None = None) -> Iterator[MemoryRecord]:
    """Yield every memory in the store (skips the index file)."""
    d = config.memory_dir(cwd)
    if not d.exists():
        return
    for path in sorted(d.glob("*.md")):
        if path.name in (config.INDEX_NAME, config.HANDOFF_NAME):
            continue
        try:
            rec = MemoryRecord.from_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Remember where it actually lives so the index links to the real file,
        # not a name re-derived from the title (which breaks on imported files
        # or after a title edit).
        rec.source_path = path.name
        yield rec


def load_all(cwd: str | os.PathLike[str] | None = None) -> list[MemoryRecord]:
    return list(iter_memories(cwd))


def _path_for(rec: MemoryRecord, cwd: str | os.PathLike[str] | None) -> Path:
    return config.memory_dir(cwd) / rec.filename()


def write_memory(
    rec: MemoryRecord, cwd: str | os.PathLike[str] | None = None
) -> Path:
    """Write a memory atomically (tmp + os.replace). Returns the file path."""
    d = _ensure_dir(cwd)
    target = d / rec.filename()
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rec.to_markdown())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def _similarity(a: MemoryRecord, b: MemoryRecord) -> float:
    sa = f"{a.title}\n{a.content}".lower()
    sb = f"{b.title}\n{b.content}".lower()
    return SequenceMatcher(None, sa, sb).ratio()


def find_duplicate(
    rec: MemoryRecord,
    cwd: str | os.PathLike[str] | None = None,
    threshold: float = _DEDUP_THRESHOLD,
) -> MemoryRecord | None:
    """Return the most similar existing active memory above ``threshold``."""
    best: MemoryRecord | None = None
    best_score = threshold
    for existing in iter_memories(cwd):
        if existing.status != "active" or existing.type != rec.type:
            continue
        score = _similarity(rec, existing)
        if score >= best_score:
            best, best_score = existing, score
    return best


def upsert(
    rec: MemoryRecord, cwd: str | os.PathLike[str] | None = None
) -> tuple[str, Path]:
    """Write with dedup. Returns (action, path).

    action ∈ {"created", "validated"}. If a near-duplicate exists, we bump its
    validation count (trust) instead of adding a second copy.
    """
    dup = find_duplicate(rec, cwd)
    if dup is not None:
        dup.validate()
        path = write_memory(dup, cwd)
        return "validated", path
    return "created", write_memory(rec, cwd)


def write_handoff(text: str, cwd: str | os.PathLike[str] | None = None) -> Path:
    """Overwrite the single working-state handoff snapshot (atomic)."""
    d = _ensure_dir(cwd)
    target = d / config.HANDOFF_NAME
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text.strip() + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def read_handoff(cwd: str | os.PathLike[str] | None = None) -> str | None:
    p = config.handoff_path(cwd)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def search(
    query: str, limit: int = 10, cwd: str | os.PathLike[str] | None = None
) -> list[MemoryRecord]:
    """Grep-like search over active memories: substring + word-overlap + fuzzy.

    Shared by the CLI (recall/answer) and the MCP server so ranking is
    consistent. In-agent recall is still native grep; this is the programmatic
    equivalent for tooling.
    """
    import re

    q = query.lower()
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2]
    scored: list[tuple[float, MemoryRecord]] = []
    for m in iter_memories(cwd):
        if m.status != "active":
            continue
        hay = f"{m.title}\n{m.content}\n{' '.join(m.tags)}".lower()
        if q in hay:
            score = 1.0
        elif words:
            overlap = sum(1 for w in words if w in hay) / len(words)
            score = overlap * 0.9 + SequenceMatcher(None, q, hay).ratio() * 0.1
        else:
            score = SequenceMatcher(None, q, hay).ratio()
        if score >= 0.22:
            scored.append((score, m))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [m for _, m in scored[:limit]]


def rebuild_index(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Regenerate MEMORY.md from the store (grouped by type, recency within)."""
    d = _ensure_dir(cwd)
    mems = [m for m in iter_memories(cwd) if m.status == "active"]

    grouped: dict[str, list[MemoryRecord]] = {}
    for m in mems:
        grouped.setdefault(m.type, []).append(m)
    for lst in grouped.values():
        lst.sort(key=lambda m: m.updated_at, reverse=True)

    ordered = [t for t in _TYPE_ORDER if t in grouped]
    ordered += [t for t in grouped if t not in _TYPE_ORDER]

    lines = [
        "# MEMORY.md — engram index",
        "",
        f"_{len(mems)} memories. One line each; read the linked file for detail._",
        "",
    ]
    for t in ordered:
        label = _TYPE_LABEL.get(t, t.capitalize())
        lines.append(f"## {label}")
        for m in grouped[t]:
            tag = "" if m.compute_confidence() >= 0.6 else " *(tentative)*"
            hook = m.description or m.title
            target = m.source_path or m.filename()
            lines.append(f"- [{m.title}]({target}) — {hook}{tag}")
        lines.append("")

    target = d / config.INDEX_NAME
    fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target
