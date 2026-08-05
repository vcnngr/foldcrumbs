"""File-backed memory store + MEMORY.md index.

One Markdown file per memory in the project memory dir. Retrieval at runtime
is the agent's own grep over this folder; this module handles writing,
loading, dedup and index regeneration. Pure stdlib (difflib for fuzzy match).
"""

from __future__ import annotations

import copy
import itertools
import os
import threading
import time
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import config
from . import embeddings
from . import recalls
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

# Wall-clock ceiling per foreign root. The reachability probe only stats the
# root; the directory listing and the reads after it can block just as long.
_FEDERATED_SCAN_TIMEOUT = 2.0

# The scan thread for a root that timed out is still blocked in the kernel on
# that mount; it cannot be killed. Starting another one on the next recall
# would stack a thread per call for as long as the mount stays hung — an
# afternoon of recalls in a long-lived MCP server adds up. Keep the one
# outstanding worker per root instead, and let a root back in only once its
# previous scan has actually finished, which is also how it recovers.
# Keyed on root *and* project: the records a scan collects belong to one
# memory directory, and the contested-by marking to one store's claims. Keying
# on the root alone let a recall for a different project join that scan and be
# handed another project's memories outright.
_pending_scans: dict[tuple[str, str], dict] = {}
# The dict is read and written from whatever thread calls recall — an MCP
# server can serve two at once — and "look, then start a worker" is a
# check-then-act like any other.
_pending_lock = threading.Lock()

# Stuck-ness belongs to the *root*, not to one project's scan of it. The slot
# above is per project so results are never crossed; without this, a hung
# mount would still get a fresh unkillable thread for every project a
# long-lived process touches.
# A *list* per root: two projects can time out on the same hung mount at once,
# and a single slot would let the second overwrite the first — forgetting a
# thread still blocked in the kernel, and reopening the gate as soon as the
# one we remembered happened to die.
# Keyed on the root's *location*, not its id. Ids deliberately survive a move,
# so an id-only gate kept skipping a root that had been relocated to a healthy
# path while the old path's worker stayed blocked — the root stayed invisible
# until the process restarted. The location still covers every project, which
# is what keeps one hung mount to a single thread.
# The mode belongs in the key for the same reason: switching a root between
# `config` and `explicit` leaves its id *and* its path untouched while moving
# its memory somewhere else entirely, so the old layout's blocked worker went
# on gating a layout it had never read.
_stuck_roots: dict[tuple[str, str, str], list[threading.Thread]] = {}

# The root a scan is *currently* running against, whatever project asked for
# it. The stuck list only fills once someone's timeout expires, so without
# this every concurrent recall for a different project would start its own
# worker against the same hung mount before anyone noticed.
_root_busy: dict[tuple[str, str, str], threading.Thread] = {}


def _reap_locked() -> None:
    """Drop finished scans from every slot, not only the one being asked for.

    A scan that timed out and later completed leaves its results — up to
    ``_MAX_FEDERATED_SCAN`` records — and a dead thread behind. Reaping just
    the requested slot means a long-lived process keeps one such entry for
    every project it ever timed out on. Callers already holding a reference to
    a reaped entry keep working from it; only the map forgets.

    The caller must hold ``_pending_lock``.
    """
    for key in [k for k, v in _pending_scans.items()
                if not v["thread"].is_alive()]:
        del _pending_scans[key]
    for rid in [r for r, ts in _stuck_roots.items()
                if not any(t.is_alive() for t in ts)]:
        del _stuck_roots[rid]
    for rid in [r for r, t in _root_busy.items() if not t.is_alive()]:
        del _root_busy[rid]

# Two matches count as equally relevant when their scores agree to this many
# decimals. Below that the difference comes from a fuzzy ratio, not from one
# answering the question better than the other.
_RANK_PRECISION = 2

# How the two secondary signals share the tiebreak. Freshness leads because it
# applies to every memory from the moment it is written, while a recall count
# starts at zero and says nothing until the memory has been needed a few times.
_FRESHNESS_SHARE = 0.6
_REINFORCEMENT_SHARE = 0.4

# Ceiling for the optional semantic signal, as a fraction of a perfect match.
# Deliberately below 1.0: a strong lexical match (exact substring = 1.0) can
# never be overtaken by a vector similarity, however high — the semantic score
# is an additional relevance signal that rescues candidates the words miss
# (paraphrases, zero word overlap), not a new owner of the ranking. Two stores
# holding the same memory must still agree on the order, and a signal whose
# value depends on which model happens to be installed cannot outrank one that
# doesn't.
_SEMANTIC_CAP = 0.8

# Minimum relevance a candidate needs to enter recall. Named (not a literal in
# search) because the optional semantic channel reuses it: a paraphrase rescued
# by the vector similarity still has to clear the same bar as a word match.
_RECALL_THRESHOLD = 0.22

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


def is_store_artifact(name: str) -> bool:
    """True for files that live in the store without being memories.

    The index, and any handoff — including the dated ones written by older
    versions and the ``sync-conflict`` copies Syncthing leaves behind. They
    parse as an "Untitled" record with the whole file as its body, so without
    this they surface in recall and, since federation, in what other
    instances are shown.
    """
    return name == config.INDEX_NAME or name.startswith("HANDOFF")


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


def iter_memories_in(
    directory: Path, max_files: int | None = None, report: dict | None = None
) -> Iterator[MemoryRecord]:
    """Yield every memory in a store directory, one file at a time.

    Lazy on purpose. The federated scan runs this on a thread it stops waiting
    for and keeps whatever arrived before the deadline, so a slow store still
    contributes what it managed to read. Building the list first would turn
    every timeout into an empty result.

    Unreadable and malformed files are skipped: one corrupt file must not
    blind the rest of the store. That means a short result is not evidence of
    a short store, so a caller deciding by *absence* can pass ``report`` and
    read ``report["complete"]`` afterwards. It starts False and becomes True
    only on reaching the end, which makes an abandoned scan incomplete by
    construction — exactly what a timed-out reader should conclude.

    A malformed file does not make a scan incomplete: it was read, and it
    holds no memory. An unreadable one does. So does truncation by
    ``max_files``, which bounds how many files are *read*, not how many
    records come out — unreadable and malformed ones cost a read too, so
    counting only what survives parsing bounds nothing.
    """
    if report is not None:
        report["complete"] = False
    d = Path(directory)
    try:
        if not d.is_dir():
            return
        names = sorted(d.glob("*.md"))
    except OSError:
        return
    truncated = max_files is not None and len(names) > max_files
    if max_files is not None:
        names = names[:max_files]
    readable = True
    for path in names:
        if is_store_artifact(path.name):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            readable = False
            continue
        except ValueError:
            # Bytes arrived, they just are not text (UnicodeDecodeError is a
            # ValueError, not an OSError — splitting read from parse let it
            # escape and abort the whole scan, so one junk file in the
            # directory blinded recall entirely).
            #
            # Counted as read, not as unreadable: the store only ever writes
            # UTF-8, so this is something else's file. Calling it unreadable
            # would switch off reconciliation for good — stale counts, and the
            # weights they leave for the next memory to reuse a filename —
            # which is worse than losing the count of one corrupt file.
            continue
        try:
            rec = MemoryRecord.from_markdown(text)
        except Exception:
            continue
        # Remember where it actually lives so the index links to the real file,
        # not a name re-derived from the title (which breaks on imported files
        # or after a title edit).
        rec.source_path = path.name
        yield rec
    if report is not None:
        report["complete"] = readable and not truncated


def scan_store(
    directory: Path, max_files: int | None = None
) -> tuple[list[MemoryRecord], bool]:
    """``iter_memories_in`` read to the end, with its completeness."""
    report: dict = {}
    records = list(iter_memories_in(directory, max_files, report))
    return records, bool(report.get("complete"))


def _gate_key(ref) -> tuple:
    """The key both `_stuck_roots` and `_root_busy` gate a root on.

    Per location *and* layout: a move or a mode change gets a fresh gate,
    while every project of a config root keeps sharing one. Built here rather
    than spelled out at each use so a test cannot seed a shape the code has
    stopped reading — which is how a gate test starts passing for free.
    """
    return (ref.id, str(ref.path), ref.mode)


def iter_federated(cwd: str | os.PathLike[str] | None = None,
                   local: list[MemoryRecord] | None = None) -> Iterator[MemoryRecord]:
    """Yield active memories from every *other* registered instance.

    Reads the foreign stores themselves rather than their index shards: the
    shards carry titles and descriptions, and recall scores on content. Each
    record comes back tagged with its origin, which marks it read-only and
    lets callers say whose memory it is.

    Every root's scan is time-bounded, not merely its reachability probe. The
    probe only stats the root: a project directory or a single file on the
    same mount can still stop responding afterwards, and the ``glob`` and
    ``read_text`` that follow would then block recall indefinitely. Each root
    is scanned on a throwaway thread we stop waiting for, so a slow store
    costs this recall its contribution and nothing more.
    """
    from foldcrumbs import federation, index_shard

    # Claims this store makes about other instances' memories. Resolved here
    # as well as in the injected block: a recall that ignores them can hand
    # back the very decision this store has already declared obsolete.
    claims = index_shard._external_supersessions(cwd, records=local)
    # The project, not just the directory. An ``explicit`` root serves every
    # cwd from one fixed path, so two projects share a memory dir — but not
    # their claims, and it is the claims that decide which records come back
    # marked obsolete. Keying on the directory alone let one project's
    # supersessions hide records from another's recall.
    project = index_shard.project_key(cwd)
    # Once, at entry, before any root can be skipped. Reaping inside the loop
    # never reached entries whose root had since become unavailable — those
    # `continue` earlier — or been unregistered, which drops it from the loop
    # entirely. Their dead threads and up to _MAX_FEDERATED_SCAN records each
    # then stayed for the life of the process.
    with _pending_lock:
        _reap_locked()
    for ref in federation.iter_roots():
        if ref.is_current():
            continue
        if ref.available_within(index_shard._AVAILABILITY_TIMEOUT) is not True:
            continue
        d = ref.memory_dir(cwd)
        slot = (ref.id, str(d), project)
        gate = _gate_key(ref)

        def scan(collected, d=d, ref=ref) -> None:
            try:
                # Say what was left out. A capped scan that stays quiet is
                # indistinguishable from a store with no matches, which is the
                # same silent-truncation trap the rendered block avoids.
                # Counted inside the worker, so a hung mount cannot make even
                # this cost more than the scan's own deadline.
                total = sum(1 for _ in d.glob("*.md")) if d.is_dir() else 0
                if total > _MAX_FEDERATED_SCAN:
                    config.log_event(
                        f"federation: reading only {_MAX_FEDERATED_SCAN} of "
                        f"{total} files in {ref.label}")
                for rec in iter_memories_in(d, max_files=_MAX_FEDERATED_SCAN):
                    if rec.status != "active":
                        continue
                    name = rec.source_path or rec.filename()
                    rec.origin_root = ref.label
                    rec.origin_root_id = ref.id
                    rec.origin_path = str(d / name)
                    # Deliberately not contested_by: these records are shared
                    # with whoever joins this scan, and each caller has its own
                    # claims. Baking in the starter's meant a caller that
                    # recorded a supersession *while* the scan ran got the old
                    # marking back and could be handed the memory it had just
                    # declared obsolete.
                    with _pending_lock:
                        collected.append(rec)
            except OSError:
                config.log_event(
                    f"federation: recall skipped {ref.label} (unreadable)")

        # Claim the slot and start the worker as one step: two concurrent
        # recalls checking first and starting after would each see no live
        # worker and start their own, which is the leak this prevents.
        #
        # A scan already in flight is *joined*, not skipped. Only one that has
        # already outlived its deadline is abandoned — treating every live
        # scan as stuck made a second concurrent recall silently return
        # nothing from a root whose scan was perfectly healthy.
        deadline = time.monotonic() + _FEDERATED_SCAN_TIMEOUT
        inflight = None
        while True:
            with _pending_lock:
                blocked = [t for t in _stuck_roots.get(gate, []) if t.is_alive()]
                if blocked:
                    _stuck_roots[gate] = blocked   # drop the ones that ended
                    config.log_event(
                        f"federation: skipping {ref.label}, {len(blocked)} "
                        "scan(s) of it are still blocked")
                    break
                _stuck_roots.pop(gate, None)
                inflight = _pending_scans.get(slot)
                if inflight is not None and not inflight["thread"].is_alive():
                    _pending_scans.pop(slot, None)
                    inflight = None
                if inflight is not None and inflight["timed_out"]:
                    config.log_event(
                        f"federation: skipping {ref.label}, its previous scan "
                        "is still blocked")
                    inflight = None
                    break
                if inflight is not None:
                    break                            # ours; join it below
                busy = _root_busy.get(gate)
                if busy is None or not busy.is_alive():
                    collected = []
                    inflight = {
                        "thread": threading.Thread(
                            target=scan, kwargs={"collected": collected},
                            daemon=True),
                        "results": collected,
                        "timed_out": False,
                    }
                    _pending_scans[slot] = inflight
                    _root_busy[gate] = inflight["thread"]
                    inflight["thread"].start()
                    break
            # Another *project* is scanning this root. Its results are not ours
            # to use — different project, different claims — so wait for it
            # rather than either add a second thread to a possibly hung mount
            # or drop the root from this recall's answer. Bounded: past the
            # deadline the root is left out, and the log says so.
            busy.join(max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                config.log_event(
                    f"federation: {ref.label} was busy with another project's "
                    "scan for too long; leaving it out of this recall")
                inflight = None
                break
        if inflight is None:
            continue
        worker = inflight["thread"]
        # The remaining budget, not a fresh one. Waiting for the root and then
        # scanning it are both part of what this root is allowed to cost; two
        # full timeouts would make a single root take twice the ceiling the
        # constant states, and four roots eight times.
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            # Partial results are honest — the block and the log both say a
            # root was cut short — and a hung mount stops being able to hold
            # up a recall that has other stores to answer from. Remember the
            # thread so the next recall does not start a second one.
            with _pending_lock:
                inflight["timed_out"] = True
                # Once per thread. Several callers can share one scan and
                # all reach the deadline together; appending blindly makes one
                # blocked thread look like many, both in the list and in what
                # the log reports.
                recorded = _stuck_roots.setdefault(gate, [])
                if worker not in recorded:
                    recorded.append(worker)
                if _root_busy.get(gate) is worker:
                    del _root_busy[gate]   # the stuck list owns it now
            config.log_event(
                f"federation: {ref.label} did not finish scanning in "
                f"{_FEDERATED_SCAN_TIMEOUT}s; using what it returned")
        else:
            with _pending_lock:
                if _pending_scans.get(slot) is inflight:
                    del _pending_scans[slot]
                if _root_busy.get(gate) is worker:
                    del _root_busy[gate]
        # Copy under the lock, yield outside it. A ``yield`` inside the ``with``
        # suspends the generator while still holding the lock: the scan thread
        # blocks on its next append for as long as the caller takes to consume,
        # and a caller that abandons the generator early never releases it.
        with _pending_lock:
            snapshot = list(inflight["results"])
        # Each caller applies its own claims, to its own copy: the records
        # belong to every joiner at once, so setting the field on the shared
        # object would race the next caller as surely as sharing the marking.
        for rec in snapshot:
            mine = copy.copy(rec)
            mine.contested_by = claims.get(
                f"{ref.id}:{rec.source_path or rec.filename()}")
            yield mine


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
        if is_store_artifact(path.name):
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
    if p is None or not p.is_file() or is_store_artifact(p.name):
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
    # The count goes with the memory, whether the file stayed or not. A
    # soft-deleted memory can be recalled again only by being restored, and
    # until then its count is just weight on a name nothing serves.
    recalls.forget(rec.id, cwd)
    rebuild_index(cwd)
    return action


def set_status(
    name: str, status: str, cwd: str | os.PathLike[str] | None = None,
    rebuild: bool = True,
) -> bool:
    """Move a memory between ``active`` and ``archived``. Rebuilds the index.

    Archiving is not deleting. The file keeps every word it had — this only
    stops it competing for attention: it leaves the index, recall, and this
    project's published shard, so other instances stop being shown it too. The
    memory can be brought back with the same call, which is the point:
    something that decayed out of relevance is not the same as something that
    was wrong, and only the second deserves to be unrecoverable.
    """
    if status not in ("active", "archived"):
        raise ValueError(f"not a status this moves between: {status}")
    rec = get(name, cwd)
    if rec is None:
        return False
    target = _resolve_in_store(name, cwd)
    if target is None:
        return False
    _refuse_if_foreign(rec, "archive")
    # Only between these two, and only in the direction asked for. Restoring
    # is the inverse of archiving, not a general revival: a memory that was
    # superseded or deleted did not decay out of relevance — something
    # replaced it, or someone removed it — and bringing those back here would
    # undo a decision this call knows nothing about. Undoing *those* is what
    # supersede and forget are for.
    allowed = "archived" if status == "active" else "active"
    if rec.status != allowed:
        return False
    rec.status = status
    rec.updated_at = datetime.now(timezone.utc)
    _write_text(target, rec.to_markdown())
    if status != "active":
        # Its recall history goes with it: while archived it answers nothing,
        # and the count would otherwise sit there weighting a memory that is
        # not in the running. Coming back, it starts earning again.
        recalls.forget(rec.id, cwd)
    # ``rebuild`` lets a caller moving several memories at once pay for the
    # index once instead of once per memory — a sweep over a large store would
    # otherwise rewrite it as many times as it archived.
    if rebuild:
        rebuild_index(cwd)
    return True


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
    recalls.forget(old.id, cwd)
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
    recalls.forget(old.id, cwd)
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
    include_contested: bool = False,
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

    A foreign memory this store has declared obsolete is left out unless
    ``include_contested``: recall feeds answers, and returning a record whose
    replacement is already recorded here is worse than returning nothing.

    With ``FOLDCRUMBS_SEMANTIC=1`` an optional embedding channel joins the
    lexical score (best-of, capped below a perfect word match — see
    ``_SEMANTIC_CAP``); without the flag, or when the embedding endpoint does
    not answer, results are purely lexical and identical to before.
    """
    import re

    q = query.lower()
    # \w+ (Unicode) instead of [a-z0-9]+: queries in accented languages must not
    # lose their words ("città" would otherwise tokenize to "citt" + nothing).
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    want_types = {t.lower() for t in types} if types else None
    want_tags = {t.lower() for t in tags} if tags else None
    scored: list[tuple[float, MemoryRecord]] = []
    # Read once and reused for the claims below. Scoring consumes the whole
    # local store anyway, so holding it costs nothing — while leaving it lazy
    # meant the federated pass parsed every local file a second time.
    local, complete = _read_local(cwd)
    # Foreign filenames are absent by construction: the sidecar is this
    # store's own observation of what it needed, and another instance's store
    # is never ours to write.
    recalled = recalls.counts(cwd)
    candidates: Iterator[MemoryRecord] = iter(local)
    if federated:
        candidates = itertools.chain(candidates, iter_federated(cwd, local=local))
    lexical: list[tuple[float, str, MemoryRecord]] = []
    for m in candidates:
        if m.status != "active":
            continue
        if m.contested_by and not include_contested:
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
        lexical.append((score, hay, m))

    # Optional second relevance channel (FOLDCRUMBS_SEMANTIC=1 + an embedding
    # endpoint that answers; either gate missing and this is a no-op). One
    # batched call covers the query and every candidate; haystacks are cached
    # machine-locally, so a warm store costs no network at all. embed() is
    # all-or-nothing: a partial answer would mix two scales, so any failure
    # returns None and recall stays purely lexical — silently, never blocking.
    sem_scores: list[float] | None = None
    if lexical and config.SEMANTIC:
        vectors = embeddings.embed([q] + [h for _, h, _ in lexical])
        if vectors is not None:
            qvec = vectors[0]
            sem_scores = [embeddings.cosine(qvec, v) for v in vectors[1:]]

    for i, (lex, _, m) in enumerate(lexical):
        score = lex
        if sem_scores is not None:
            # Relevance is the best of two independent evidence channels, but
            # the semantic one is capped below a perfect lexical match: no
            # vector similarity can outrank what the words already matched
            # exactly, and it rescues paraphrases the lexical pass could not
            # see at all (they would never have reached the threshold below).
            capped = max(0.0, sem_scores[i]) * _SEMANTIC_CAP
            if capped > score:
                score = capped
        if score >= _RECALL_THRESHOLD:
            scored.append((score, _tiebreak(m, recalled), m))
    # Relevance decides the order; recency and use only separate memories
    # that matched *equally well*. Folding either into the score itself let a
    # near match times its bonus overtake an exact one — 0.9613 x 1.1 beats
    # 1.0 — so they are a later key, never part of the number compared.
    # Comparable means equal to two decimals: finer than that is noise from a
    # fuzzy ratio, not a real difference in how well something matched.
    # Ties then break by locality (a local memory is the one this instance can
    # act on) and finally by filename, so the result never depends on
    # directory order.
    scored.sort(key=lambda t: (-round(t[0], _RANK_PRECISION), -t[1],
                               t[2].is_foreign,
                               t[2].source_path or t[2].filename()))
    top = [m for _, _, m in scored[:limit]]
    # None unless the listing is *complete*. A partial one — a directory that
    # exists but a file that would not open — names fewer memories than the
    # store holds, and reconciling against it would erase the counts of every
    # memory that happened to be unreadable at that moment.
    recalls.reinforce(_reinforceable(scored, top, limit), cwd,
                      known=_active_names(local) if complete else None)
    return top


def _tiebreak(rec: MemoryRecord, recalled: dict[str, int]) -> float:
    """How to order two memories the query matched equally well.

    Neither signal answers the question better than the other did — that was
    already decided. These say which of two equal answers to put first: the
    more recent one, and the one that has actually been needed. Both are
    confined to the tiebreak on purpose, so no amount of either can promote a
    worse match.
    """
    reinforcement = recalls.strength(
        0 if not _countable(rec) else recalled.get(rec.id, 0))
    return (_FRESHNESS_SHARE * recalls.freshness(rec)
            + _REINFORCEMENT_SHARE * reinforcement)


def _reinforceable(scored: list, top: list[MemoryRecord], limit: int) -> list[str]:
    """Which local memories this recall should count as used.

    What was returned — and anything that matched *just as well* but fell the
    wrong side of the limit. Without that second part the cut through a group
    of equally-relevant memories is decided by filename, and only the winner is
    ever reinforced: an arbitrary tiebreak would compound into a permanent lead
    that reflects nothing but having been first alphabetically.
    """
    names = [m.id for m in top if _countable(m)]
    if limit <= 0:
        return []       # nothing was returned, so nothing was used
    if len(scored) <= limit:
        return names
    cutoff = round(scored[limit - 1][0], _RANK_PRECISION)
    for score, _, m in scored[limit:]:
        if round(score, _RANK_PRECISION) != cutoff:
            break              # sorted, so nothing later can tie either
        if _countable(m):
            names.append(m.id)
    return names


def _read_local(
    cwd: str | os.PathLike[str] | None,
) -> tuple[list[MemoryRecord], bool]:
    """This store's memories, and whether that list is the whole store."""
    return scan_store(config.memory_dir(cwd))


def _countable(rec: MemoryRecord) -> bool:
    """Whether a recall of this record can be counted at all.

    Not a foreign one: reinforcing is a write, and another instance's store is
    never ours to write. And not one whose id was minted on load — a memory
    saved before ids were serialized gets a different uuid every time it is
    read, so a count would be filed under a key that never comes back. It
    would never accumulate, and each recall would add one entry and reconcile
    away the last, churning a file that may well be synced between machines.
    """
    return not rec.is_foreign and not rec.id_missing


def _active_names(local: list[MemoryRecord]) -> set[str]:
    return {m.id for m in local if m.status == "active" and _countable(m)}


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
