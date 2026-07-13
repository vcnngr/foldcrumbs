"""File-backed memory store + MEMORY.md index.

One Markdown file per memory in the project memory dir. Retrieval at runtime
is the agent's own grep over this folder; this module handles writing,
loading, dedup and index regeneration. Pure stdlib (difflib for fuzzy match).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
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


def get(
    name: str, cwd: str | os.PathLike[str] | None = None
) -> MemoryRecord | None:
    """Load a single memory by its on-disk filename (as linked in MEMORY.md)."""
    p = config.memory_dir(cwd) / name
    if not p.is_file() or p.name in (config.INDEX_NAME, config.HANDOFF_NAME):
        return None
    try:
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    rec.source_path = p.name
    return rec


def forget(
    name: str, cwd: str | os.PathLike[str] | None = None, hard: bool = False
) -> str | None:
    """Forget a memory by filename; rebuilds the index. Returns the action taken.

    Soft by default: mark ``status=deleted`` so the file stays on disk (auditable,
    recoverable, cleaned later by ``prune``) but drops out of the index and
    recall. ``hard=True`` unlinks the file instead. Returns "deleted" /
    "removed", or None when the name doesn't resolve to a memory.
    """
    rec = get(name, cwd)
    if rec is None:
        return None
    d = config.memory_dir(cwd)
    if hard:
        try:
            (d / name).unlink()
        except OSError:
            return None
        action = "removed"
    else:
        rec.status = "deleted"
        rec.updated_at = datetime.now(timezone.utc)
        # Write back to the file it was read from, not a name re-derived from
        # the title (imported files can live under non-canonical names).
        _write_text(d / name, rec.to_markdown())
        action = "deleted"
    rebuild_index(cwd)
    return action


def supersede(
    old_name: str, new_name: str, cwd: str | os.PathLike[str] | None = None
) -> bool:
    """Mark ``old_name`` as superseded by ``new_name`` (both on-disk filenames).

    The old file stays on disk with ``status: superseded`` (confidence collapses
    to 0, drops out of index/recall; ``prune`` can clear it later). Returns False
    when either name doesn't resolve.
    """
    old, new = get(old_name, cwd), get(new_name, cwd)
    if old is None or new is None or old_name == new_name:
        return False
    old.mark_superseded(new.id)
    _write_text(config.memory_dir(cwd) / old_name, old.to_markdown())
    rebuild_index(cwd)
    return True


def _write_text(target: Path, text: str) -> None:
    """Atomic write (tmp + os.replace) to an explicit path."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
    query: str,
    limit: int = 10,
    cwd: str | os.PathLike[str] | None = None,
    types: list[str] | None = None,
    tags: list[str] | None = None,
) -> list[MemoryRecord]:
    """Grep-like search over active memories: substring + word-overlap + fuzzy.

    Shared by the CLI (recall/answer) and the MCP server so ranking is
    consistent. In-agent recall is still native grep; this is the programmatic
    equivalent for tooling. ``types``/``tags`` narrow the candidates before
    scoring (a memory matches ``tags`` if it carries at least one of them).
    """
    import re

    q = query.lower()
    # \w+ (Unicode) instead of [a-z0-9]+: queries in accented languages must not
    # lose their words ("città" would otherwise tokenize to "citt" + nothing).
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    want_types = {t.lower() for t in types} if types else None
    want_tags = {t.lower() for t in tags} if tags else None
    scored: list[tuple[float, MemoryRecord]] = []
    for m in iter_memories(cwd):
        if m.status != "active":
            continue
        if want_types and m.type not in want_types:
            continue
        if want_tags and not (want_tags & {t.lower() for t in m.tags}):
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
    """Regenerate MEMORY.md from the store (grouped by type, stable within).

    Within each type memories are ordered by immutable ``created_at`` (newest
    first) so the index is deterministic: only adding/removing a memory changes
    it, not a trust bump or re-touch. This keeps the injected prefix cacheable
    and the file diff-stable for Syncthing.
    """
    d = _ensure_dir(cwd)
    mems = [m for m in iter_memories(cwd) if m.status == "active"]

    grouped: dict[str, list[MemoryRecord]] = {}
    for m in mems:
        grouped.setdefault(m.type, []).append(m)
    for lst in grouped.values():
        # Order by created_at (immutable) so trust bumps / re-touches / distills
        # never reorder the same set of memories. A stable index keeps the
        # SessionStart-injected prefix identical across sessions (rides the
        # agent's prompt cache) and stops Syncthing from seeing spurious line
        # moves. filename() is the deterministic tiebreak for equal timestamps.
        lst.sort(key=lambda m: m.filename())
        lst.sort(key=lambda m: m.created_at, reverse=True)

    ordered = [t for t in _TYPE_ORDER if t in grouped]
    ordered += [t for t in grouped if t not in _TYPE_ORDER]

    lines = [
        "# MEMORY.md — foldcrumbs index",
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
