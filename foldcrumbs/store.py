"""File-backed memory store + MEMORY.md index.

One Markdown file per memory in the project memory dir. Retrieval at runtime
is the agent's own grep over this folder; this module handles writing,
loading, dedup and index regeneration. Pure stdlib (difflib for fuzzy match).
"""

from __future__ import annotations

import itertools
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

# Ceiling on how much of one foreign store a single recall will read. Recall
# must stay responsive: an unbounded scan across four roots is a hang waiting
# for a large store or a slow mount.
_MAX_FEDERATED_SCAN = 500

_DEDUP_THRESHOLD = 0.85  # title+content similarity above which two memories match


class ForeignMemoryError(PermissionError):
    """Refused: the record belongs to another instance's store.

    Federation is read-only across roots by design. The rendered blocks tell
    the model so, but a prompt is guidance, not a guarantee — this is the
    guarantee. Every write path checks it, because a federated recall now
    hands callers records they did not author and nothing else would stop one
    from being written back under this root.
    """


def _resolve_in_store(
    name: str, cwd: str | os.PathLike[str] | None = None
) -> Path | None:
    """Resolve a memory filename *inside* the store, or None if it escapes.

    ``memory_dir / name`` is not containment: an absolute name replaces the
    directory outright, and "../x" walks out of it. Since ``forget --hard``
    unlinks whatever it resolves and ``from_markdown`` parses any text into an
    "Untitled" record, an unchecked name is arbitrary file deletion. Every
    filename-addressed operation goes through here.
    """
    if not name or os.path.isabs(name) or os.sep in name or "/" in name:
        return None
    d = config.memory_dir(cwd)
    target = (d / name).resolve()
    try:
        if target.parent != d.resolve():
            return None
    except OSError:
        return None
    return d / name


def _refuse_if_foreign(rec: MemoryRecord, action: str) -> None:
    if rec.is_foreign:
        raise ForeignMemoryError(
            f"cannot {action} '{rec.title}': it belongs to {rec.origin_root} "
            f"({rec.origin_path}). Record your own memory instead."
        )


def _ensure_dir(cwd: str | os.PathLike[str] | None) -> Path:
    d = config.memory_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_memories(cwd: str | os.PathLike[str] | None = None) -> Iterator[MemoryRecord]:
    """Yield every memory in **this instance's** store (skips the index file).

    Deliberately local, and it must stay that way: every write path in this
    module is built on it, so federating it here would let a foreign record be
    validated, superseded or republished under the current root. Federated
    reading has its own entry point, ``iter_federated``.
    """
    yield from iter_memories_in(config.memory_dir(cwd))


def iter_memories_in(directory: Path) -> Iterator[MemoryRecord]:
    """Yield every memory in an arbitrary store directory."""
    d = Path(directory)
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


def iter_federated(cwd: str | os.PathLike[str] | None = None) -> Iterator[MemoryRecord]:
    """Yield active memories from every *other* registered instance.

    Reads the foreign stores themselves rather than their index shards: the
    shards carry titles and descriptions, and recall scores on content. Each
    record comes back tagged with its origin, which marks it read-only and
    lets callers say whose memory it is.

    Unreachable roots are skipped rather than waited on — this runs inside
    ``recall``, and one unplugged drive must not hang it.
    """
    from foldcrumbs import federation, index_shard

    for ref in federation.iter_roots():
        if ref.is_current():
            continue
        if ref.available_within(index_shard._AVAILABILITY_TIMEOUT) is not True:
            continue
        d = ref.memory_dir(cwd)
        seen = 0
        try:
            for rec in iter_memories_in(d):
                # Count every file read, not just the ones kept: a store full
                # of superseded records would otherwise be scanned without
                # limit, which is exactly the cost this bounds.
                seen += 1
                if seen > _MAX_FEDERATED_SCAN:
                    config.log_event(
                        f"federation: stopped scanning {ref.label} after "
                        f"{_MAX_FEDERATED_SCAN} files"
                    )
                    break
                if rec.status != "active":
                    continue
                rec.origin_root = ref.label
                rec.origin_root_id = ref.id
                rec.origin_path = str(d / (rec.source_path or rec.filename()))
                yield rec
        except OSError:
            config.log_event(f"federation: recall skipped {ref.label} (unreadable)")
            continue


def load_all(cwd: str | os.PathLike[str] | None = None) -> list[MemoryRecord]:
    return list(iter_memories(cwd))


def _path_for(rec: MemoryRecord, cwd: str | os.PathLike[str] | None) -> Path:
    return config.memory_dir(cwd) / rec.filename()


def write_memory(
    rec: MemoryRecord, cwd: str | os.PathLike[str] | None = None
) -> Path:
    """Write a memory atomically (tmp + os.replace). Returns the file path."""
    _refuse_if_foreign(rec, "write")
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


def _stems(rec: MemoryRecord) -> set[str]:
    """Crude 5-char prefix stems of the content words (>3 chars).

    Enough to make "published"/"publishing" or "deferred"/"defer" collide
    without a stemmer dependency; the LLM verdict downstream absorbs the noise.
    """
    import re

    return {
        w[:5]
        for w in re.findall(r"\w+", f"{rec.title} {rec.content}".lower())
        if len(w) > 3
    }


def find_conflict_candidates(
    rec: MemoryRecord,
    cwd: str | os.PathLike[str] | None = None,
    limit: int = 3,
    min_overlap: float = 0.4,
    federated: bool = False,
) -> list[MemoryRecord]:
    """Existing active memories that plausibly describe the same subject as
    ``rec`` without being near-duplicates (those are handled by dedup).

    Candidates share enough stemmed words to be about the same thing — e.g. a
    "PyPI publishing deferred" decision vs a "published to PyPI" fact. Cross-type
    on purpose: a new fact often obsoletes an old decision. This is only a cheap
    pre-filter; whether one actually supersedes the other is the LLM's call."""
    wa = _stems(rec)
    if not wa:
        return []
    out: list[tuple[float, MemoryRecord]] = []
    # Reading the union is the point: a decision reversed in another instance
    # is exactly the contradiction worth catching. Acting on it is a different
    # matter — the caller records an assertion rather than writing their file.
    pool = iter_memories(cwd)
    if federated:
        pool = itertools.chain(pool, iter_federated(cwd))
    for m in pool:
        if m.status != "active" or m.id == rec.id:
            continue
        if _similarity(rec, m) >= _DEDUP_THRESHOLD:
            continue
        wb = _stems(m)
        if not wb:
            continue
        overlap = len(wa & wb) / min(len(wa), len(wb))
        if overlap >= min_overlap:
            out.append((overlap, m))
    out.sort(key=lambda t: (t[0], t[1].origin_root or "", t[1].filename()),
             reverse=True)
    return [m for _, m in out[:limit]]


def upsert(
    rec: MemoryRecord, cwd: str | os.PathLike[str] | None = None
) -> tuple[str, Path]:
    """Write with dedup. Returns (action, path).

    action ∈ {"created", "validated"}. If a near-duplicate exists, we bump its
    validation count (trust) instead of adding a second copy.
    """
    _refuse_if_foreign(rec, "store")
    dup = find_duplicate(rec, cwd)
    if dup is not None:
        dup.validate()
        path = write_memory(dup, cwd)
        return "validated", path
    return "created", write_memory(rec, cwd)


def import_store(
    src_dir: str | os.PathLike[str],
    cwd: str | os.PathLike[str] | None = None,
    apply: bool = False,
) -> dict[str, list[str]]:
    """Merge another store's active memories into this one, record-level.

    Unlike ``migrate --from`` (raw file copy), this goes through ``upsert`` so
    near-duplicates validate the existing memory instead of clobbering or
    doubling it. Skipped: index/handoff files, files without frontmatter, and
    non-active records (superseded/deleted history stays where it is).

    Dry-run by default — returns the plan {created, validated, skipped} as
    lists of source filenames; with ``apply`` it writes and rebuilds the index.
    """
    src = Path(src_dir).expanduser()
    plan: dict[str, list[str]] = {"created": [], "validated": [], "skipped": []}
    for path in sorted(src.glob("*.md")):
        if path.name == config.INDEX_NAME or path.name.startswith("HANDOFF"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            plan["skipped"].append(path.name)
            continue
        # No frontmatter -> not a memory record (a stray note, a README).
        if not text.startswith("---"):
            plan["skipped"].append(path.name)
            continue
        try:
            rec = MemoryRecord.from_markdown(text)
        except Exception:
            plan["skipped"].append(path.name)
            continue
        if rec.status != "active":
            plan["skipped"].append(path.name)
            continue
        if apply:
            action, _ = upsert(rec, cwd)
        else:
            action = "validated" if find_duplicate(rec, cwd) else "created"
        plan[action].append(path.name)
    if apply and (plan["created"] or plan["validated"]):
        rebuild_index(cwd)
    return plan


def get(
    name: str, cwd: str | os.PathLike[str] | None = None
) -> MemoryRecord | None:
    """Load a single memory by its on-disk filename (as linked in MEMORY.md)."""
    p = _resolve_in_store(name, cwd)
    if p is None or not p.is_file() or p.name in (config.INDEX_NAME,
                                                  config.HANDOFF_NAME):
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
    target = _resolve_in_store(name, cwd)
    if target is None:
        return None
    if hard:
        try:
            target.unlink()
        except OSError:
            return None
        action = "removed"
    else:
        rec.status = "deleted"
        rec.updated_at = datetime.now(timezone.utc)
        # Write back to the file it was read from, not a name re-derived from
        # the title (imported files can live under non-canonical names).
        _write_text(target, rec.to_markdown())
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


def mark_superseded_on_disk(
    old: MemoryRecord, new_id: str, cwd: str | os.PathLike[str] | None = None
) -> Path:
    """Mark ``old`` as superseded by ``new_id`` and write it back in place.

    Writes to the file the record was loaded from (``source_path``), not a name
    re-derived from the title — imported files can live under non-canonical
    names. The file stays on disk (auditable, cleared later by prune) but drops
    out of the index and recall. Unlike ``supersede`` (which resolves both sides
    by filename), this takes an already-loaded record — the distill contradiction
    pass calls it with candidates it just scored."""
    # The contradiction pass now scores foreign candidates too, so this is
    # reachable with someone else's record. Writing it would edit another
    # instance's store *and* land the edit at the wrong path, since the target
    # is built from this root's memory dir.
    _refuse_if_foreign(old, "supersede")
    name = old.source_path or old.filename()
    target = _resolve_in_store(name, cwd)
    if target is None:
        raise ForeignMemoryError(f"refusing to write outside the store: {name}")
    _ensure_dir(cwd)
    old.mark_superseded(new_id)
    _write_text(target, old.to_markdown())
    return target


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
    federated: bool = True,
) -> list[MemoryRecord]:
    """Grep-like search over active memories: substring + word-overlap + fuzzy.

    Shared by the CLI (recall/answer) and the MCP server so ranking is
    consistent. In-agent recall is still native grep; this is the programmatic
    equivalent for tooling. ``types``/``tags`` narrow the candidates before
    scoring (a memory matches ``tags`` if it carries at least one of them).

    ``federated`` also scores the other instances' stores, ranked together so
    the best answer wins regardless of whose store holds it. It matters most
    where there is no grep to fall back on: OpenCode recalls purely through
    this function over MCP, so without it federation would be invisible there.
    Callers that act on a result — forgetting, superseding — must pass False,
    since only the owning instance may write its own store.
    """
    import re

    q = query.lower()
    # \w+ (Unicode) instead of [a-z0-9]+: queries in accented languages must not
    # lose their words ("città" would otherwise tokenize to "citt" + nothing).
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    want_types = {t.lower() for t in types} if types else None
    want_tags = {t.lower() for t in tags} if tags else None
    scored: list[tuple[float, MemoryRecord]] = []
    candidates = iter_memories(cwd)
    if federated:
        candidates = itertools.chain(candidates, iter_federated(cwd))
    for m in candidates:
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
    # Ties broken by locality then filename: a local memory outranks an
    # identical foreign one (it is the one this instance can act on), and the
    # rest is deterministic rather than dependent on directory order.
    scored.sort(key=lambda t: (-t[0], t[1].is_foreign,
                               t[1].source_path or t[1].filename()))
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

    # Publish the same set to this root's federated shard. MEMORY.md above is
    # deliberately unchanged by federation — keeping it byte-stable is what
    # lets the injected prefix stay cached — so the shared view lives entirely
    # in the shard. Best-effort: an unpublishable shard costs visibility to
    # other instances, never the local index.
    try:
        from foldcrumbs import index_shard
        index_shard.write_shard(cwd)
    except Exception:  # noqa: BLE001 - never fail a rebuild over federation
        config.log_event("federation: index shard not published")
    return target
