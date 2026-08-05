"""Per-root index shards: the federated view, stored in pieces.

Every instance publishes what *its* store knows about a project into a shard of
its own:

    <state-dir>/projects/<project-key>/roots/<root-id>.json

Readers merge the shards; nothing is ever written to a single shared index.
That is the whole design in one line, and the reason for it is a lost update:
with one file, two instances scanning and rewriting concurrently would publish
a snapshot that silently drops whatever the other just added. ``os.replace``
prevents a torn file, not a stale one.

Sharding removes that race *between* roots — but a root is an instance, not a
process, and one instance runs several at once (a hook worker, a CLI call, the
MCP server). So the same lost update is possible within a single shard. Writes
hold a lock **scoped to that shard** across the scan, so there is no window between
reading the store and publishing it, and a shard already saying exactly what
was read is left alone rather than rewritten. Detecting the movement after the
fact was tried three ways — newest mtime, scan timestamps, a stat signature —
and each had its own blind spot, because the window was real. Removing it is
the only version without a detector to get wrong.

The local ``MEMORY.md`` is untouched. It stays byte-identical while only other
instances write, so the SessionStart-injected prefix keeps riding the agent's
prompt cache — the federated block is appended after it, in the region a
changing HANDOFF already invalidates every session.

**Ordering.** Merged entries are sorted by a *total* key:

    (type rank, created_at descending, root id, filename)

``rebuild_index`` can lean on ``filename()`` alone as a tiebreak because one
store cannot hold two files with one name. Across roots it can, so the root id
goes into the key — otherwise two instances would render the same set in
different orders. ``created_at`` is normalised to UTC, and a record whose file
carries no timestamp is pinned to its file's mtime rather than to the "now"
that parsing invents on every read.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from foldcrumbs import config, federation

SHARD_VERSION = 1

# With four instances the federated block can dwarf the local index, and a
# bloated prompt costs relevance. Entries past this are summarised as a count;
# the paths are announced either way, so nothing becomes unreachable.
_MAX_FEDERATED_ENTRIES = 40

# Past this, a shard's age is worth saying out loud.
_STALE_AFTER_DAYS = 30

# Probing a root that lives on a hung network mount must not stall a hook.
_AVAILABILITY_TIMEOUT = 0.25

# The entry cap bounds the count, not the size: one root with very long
# descriptions could still dominate the window.
_MAX_FEDERATED_CHARS = 12000


def project_key(cwd: str | os.PathLike[str] | None = None) -> str:
    """Directory name for a project inside the shared state dir."""
    return config.encode_cwd(cwd or os.getcwd())


def shards_dir(cwd: str | os.PathLike[str] | None = None) -> Path:
    return config.STATE_DIR / "projects" / project_key(cwd) / "roots"


def shard_path(root_id: str, cwd: str | os.PathLike[str] | None = None) -> Path | None:
    if not federation.valid_id(root_id):
        return None
    return shards_dir(cwd) / f"{root_id}.json"


def _stable_created_at(rec, path: Path) -> str:
    """A timestamp that means the same thing on every read.

    ``MemoryRecord`` invents ``created_at`` when the file has none, and invents
    a different one each time. Sorting a federated view on that would reshuffle
    entries between sessions, so fall back to the file's mtime, which at least
    is a fact about the file.
    """
    if getattr(rec, "created_at_missing", False):
        try:
            ts = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            ts = datetime.fromtimestamp(0, timezone.utc)
        return ts.isoformat()
    return rec.created_at.astimezone(timezone.utc).isoformat()


def _entry(rec, memory_dir: Path) -> dict:
    name = rec.source_path or rec.filename()
    path = memory_dir / name
    return {
        "filename": name,
        # Absolute: the agent greps and reads these directly, and a relative
        # link would resolve against whichever index rendered it.
        "path": str(path),
        "title": rec.title,
        "description": rec.description or "",
        "type": rec.type,
        "created_at": _stable_created_at(rec, path),
        "tentative": rec.compute_confidence() < 0.6,
    }


def write_shard(cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """Publish this instance's contribution to the project's federated view.

    Only ever writes this root's own shard. Returns None when the instance is
    not federated (no marker, or removed) — federation is opt-in and a
    non-participant must not start publishing because it happened to rebuild
    its index.
    """
    from foldcrumbs import store  # local: store imports config, not this module

    cur = federation.current_root_path()
    marker = federation.read_marker_data(cur) if cur is not None else None
    if not marker or federation.is_tombstoned(marker["id"]):
        return None
    target = shard_path(marker["id"], cwd)
    if target is None:
        return None

    memory_dir = config.memory_dir(cwd)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # One shard has one owning *root*, but a root is an instance, not a
        # process: a hook worker, a CLI call and the MCP server all publish it.
        # Two of them scanning and replacing concurrently is the same lost
        # update sharding removes between roots — the slower scan wins the
        # replace and the shard advertises a store that has moved on.
        #
        # So the scan happens *inside* the lock. Every earlier attempt kept it
        # outside and tried to detect afterwards whether the store had moved:
        # by newest mtime (which drops on delete), by timestamps (which need a
        # skew tolerance, and any tolerance is the same window again), by a
        # stat signature (blind to an edit that keeps size and mtime). Each
        # detector had its own blind spot because the window was real. Holding
        # the lock across the scan removes the window instead of watching it.
        # Scoped to *this* shard, not to the registry: the scan happens inside
        # the critical section, and a large store held the machine-wide lock
        # long enough to stall every other instance's SessionStart. The only
        # processes that race for this file are this instance's own, on this
        # project, so that is exactly what the lock covers.
        lock = shards_dir(cwd) / f".lock-{marker['id']}"
        with federation.file_lock(lock) as locked:
            if not locked:
                config.log_event(
                    "federation: shard not published (lock unavailable)"
                )
                return None
            entries = [
                _entry(m, memory_dir)
                for m in store.iter_memories(cwd)
                if m.status == "active"
            ]
            entries.sort(key=lambda e: (e["created_at"], e["filename"]))
            existing = _read_shard_file(target)
            # The directory counts as much as the entries. Readers refuse a
            # shard describing a layout the root has left, and comparing
            # entries alone made that permanent: a root that moved without
            # its memories changing kept re-deciding it had nothing to say,
            # so the stale directory was never rewritten and the root stayed
            # invisible until someone happened to edit a memory.
            if (existing is not None
                    and existing.get("entries") == entries
                    and existing.get("memory_dir") == str(memory_dir)):
                return target      # already says exactly this; don't churn it
            payload = {
                "version": SHARD_VERSION,
                "root_id": marker["id"],
                "label": federation._label_for(cur),
                "memory_dir": str(memory_dir),
                "written_at": datetime.now(timezone.utc).isoformat(),
                "entries": entries,
            }
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    except OSError:
        # A store that cannot publish is still a working store: the local
        # index and recall are unaffected, this instance is just invisible to
        # the others until the next rebuild.
        config.log_event(f"federation: could not write index shard {target}")
        return None
    return target


def ensure_shard(cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """Publish this root's shard if it is missing or behind the store.

    Shards are normally written by ``rebuild_index``, which only runs when
    something is remembered or distilled. An instance that federates an
    *existing* store would therefore appear registered but empty until its
    next write — its whole history invisible to everyone else. Called from the
    hooks so joining the federation shows what is already there, immediately.

    There is no cheap pre-check before delegating: a stat-based one would skip
    the republish for an edit that kept a file's size and mtime, leaving a
    wrong title published indefinitely. ``write_shard`` reads the store — the
    same work the index rebuild already does — and rewrites nothing when the
    entries it derives match what is published.
    """
    return write_shard(cwd)


def _read_shard_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class _Stalled(Exception):
    """A probe did not answer. Stop judging this root's shards for now.

    Raised rather than returned so it ends the whole cleanup instead of one
    shard's verdict: on a stalled mount every further probe costs another
    timeout and leaves another thread blocked in the kernel, so what was one
    lost second becomes one per shard.
    """


def _identity_of(path: str, memo: dict):
    """``_identity`` once per path per cleanup, or ``_Stalled``."""
    if path not in memo:
        memo[path] = federation._identity(path)
    got = memo[path]
    if got is federation._UNKNOWN:
        raise _Stalled(path)
    return got


def _left_behind(recorded: str, ref, mine: tuple, memo: dict) -> bool:
    """Whether a shard provably describes a layout this root no longer uses.

    Deletion needs proof, not merely the absence of a match. Readers already
    refuse a shard they cannot place, so keeping an ambiguous one costs a
    rejection until the next publication — while dropping a *fresh* shard
    published through a symlink, a bind mount, or an alternate spelling of the
    root loses it outright, and the writer has no reason to produce it again.

    So the cheap textual test only nominates a candidate; the filesystem
    confirms it. ``mine`` is this root's identity, read once by the caller.
    """
    if ref.mode == "explicit":
        if Path(recorded) == ref.path:
            return False
        theirs = _identity_of(recorded, memo)
        return theirs is federation._ABSENT or theirs != mine
    if ref.path in Path(recorded).parents:
        return False
    # A config root keeps its projects beneath itself, so the shard belongs
    # here if *any* ancestor is this root — compared by identity, since the
    # names can differ the whole way up.
    for ancestor in Path(recorded).parents:
        if _identity_of(str(ancestor), memo) == mine:
            return False
    return True


def drop_root_shards_in(registry: Path, root_id: str) -> int:
    """Remove a root's project shards from a registry it has left.

    Departure only removes ``roots/<id>.json``. The per-project shards stayed,
    and a root that later returns clears its own tombstone — at which point
    those pre-move shards read as valid again, by id and by memory path, and
    advertise whatever the store held before the move.
    """
    base = Path(registry) / "projects"
    if not base.is_dir():
        return 0
    registration = Path(registry) / "roots" / f"{root_id}.json"
    dropped = 0
    for shard in base.glob(f"*/roots/{root_id}.json"):
        # The same lock a publication takes, so this cannot delete a shard
        # written between the glob and the unlink.
        with federation.file_lock(shard.parent / f".lock-{root_id}") as locked:
            if not locked:
                continue
            # Re-read under the lock: the departure runs on a thread the
            # relocation stopped waiting for, so the root may have come back
            # to this registry meanwhile and republished here. Its shards are
            # then current, and a cleanup for a departure that is no longer
            # the state of things would delete fresh publications.
            if registration.is_file():
                config.log_event(
                    f"federation: {root_id} is registered in {registry} "
                    "again; keeping its project shards")
                return dropped
            try:
                shard.unlink()
                dropped += 1
            except FileNotFoundError:
                pass
            except OSError:
                config.log_event(
                    f"federation: could not drop departed shard {shard}")
    return dropped


def drop_stale_shards(ref) -> int:
    """Remove this root's shards that describe a layout it no longer uses.

    Changing a root's mode moves its memory directory, invalidating every
    shard it has published — across every project, not just the one in hand.
    Readers already refuse those, but refusal alone is permanent: a project
    that is never opened again never republishes, so the dead shard sits there
    being rejected and logged forever. Dropped at the moment that invalidates
    them, they are simply absent until each project next publishes.

    Decided from the shape of the recorded directory, which needs no cwd: an
    ``explicit`` root serves one fixed path, a ``config`` root always keeps a
    project's memory under itself.
    """
    base = config.STATE_DIR / "projects"
    if not base.is_dir():
        return 0
    # Read once for the whole cleanup, not once per shard: this is a bounded
    # probe, and paying its timeout for every project of a root on a stalled
    # mount turned one lost second into one per shard, each leaving another
    # thread blocked in the kernel.
    mine = federation._identity(str(ref.path))
    if not isinstance(mine, tuple):
        config.log_event(
            f"federation: {ref.label} did not describe itself; left its "
            "shards alone")
        return 0
    memo: dict[str, object] = {str(ref.path): mine}
    dropped = 0
    for shard in base.glob(f"*/roots/{ref.id}.json"):
        # Under the same lock ``write_shard`` takes for this shard. Reading
        # then unlinking is a check-then-act on a file another process of this
        # instance can replace: a project publishing a *fresh* shard in that
        # window would have had it deleted as though it were the old one.
        with federation.file_lock(shard.parent / f".lock-{ref.id}") as locked:
            if not locked:
                continue          # readers refuse it meanwhile; try again later
            data = _read_shard_file(shard)
            recorded = data.get("memory_dir") if data else None
            if not isinstance(recorded, str):
                continue
            try:
                if not _left_behind(recorded, ref, mine, memo):
                    continue
            except _Stalled as stall:
                config.log_event(
                    f"federation: {stall} did not answer; stopped dropping "
                    f"shards of {ref.label}")
                break
            try:
                shard.unlink()
                dropped += 1
            except OSError:
                config.log_event(
                    f"federation: could not drop stale shard {shard}")
    if dropped:
        config.log_event(
            f"federation: dropped {dropped} shard(s) of {ref.label} left by an "
            "earlier layout")
    return dropped


def drop_shard(root_id: str, cwd: str | os.PathLike[str] | None = None) -> bool:
    """Remove one root's shard for this project (used when it unregisters)."""
    p = shard_path(root_id, cwd)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def read_shards(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """Every registered foreign root's shard for this project.

    Skips the current root — its memories are already in the local index — and
    skips roots that are not registered, so a removal takes effect even if the
    shard file is still on disk. An unreadable shard is dropped rather than
    raising: a corrupt file must not cost the session its whole federated view.
    """
    out: list[dict] = []
    d = shards_dir(cwd)
    if not d.is_dir():
        return out
    known = {r.id: r for r in federation.iter_roots()}
    for p in sorted(d.glob("*.json")):
        rid = p.stem
        ref = known.get(rid)
        if ref is None or ref.is_current():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config.log_event(f"federation: ignoring unreadable index shard {p}")
            continue
        if not isinstance(data, dict) or data.get("root_id") != rid:
            config.log_event(f"federation: ignoring mismatched index shard {p}")
            continue
        # A shard written by a newer foldcrumbs may mean something this reader
        # would misinterpret; skipping is visibly incomplete, guessing is not.
        version = data.get("version")
        if not isinstance(version, int) or version > SHARD_VERSION:
            config.log_event(
                f"federation: ignoring shard {p} written for format v{version}"
            )
            continue
        if not isinstance(data.get("entries"), list):
            config.log_event(f"federation: ignoring shard {p} with no entry list")
            continue
        # The root's layout can change under a shard: switching mode moves its
        # whole memory directory, and shards already published for other
        # projects keep the old one — with absolute paths to match. Accepting
        # them on root id alone served those stale paths to every instance
        # until each project happened to republish, which for an old project
        # may be never. Checked here so it self-heals rather than needing every
        # shard hunted down at the moment of the change.
        expected = str(ref.memory_dir(cwd))
        if not _same_memory_dir(data.get("memory_dir"), expected):
            config.log_event(
                f"federation: ignoring shard {p} — it describes "
                f"{data.get('memory_dir')}, but {ref.label} now keeps this "
                f"project's memory in {expected}")
            continue
        data["label"] = ref.label
        # A root we cannot reach keeps its last published entries, flagged:
        # dropping them would read as "those memories were deleted". The probe
        # is time-bounded because this runs inside a hook; None means it did
        # not answer, which for the reader is the same as unreachable.
        data["available"] = ref.available_within(_AVAILABILITY_TIMEOUT)
        out.append(data)
    return out


def _clean_entry(entry: object) -> dict | None:
    """Coerce one shard entry to known-safe types, or reject it.

    Shards are written by other installations — possibly other versions — and
    are edited by nobody, in theory. In practice a single entry whose
    ``created_at`` is a list would raise inside the sort, and the hook's
    ``except`` would swallow it: one malformed record would cost the session
    its *entire* federated view. Rejecting that one record is the difference
    between degraded and blank.
    """
    if not isinstance(entry, dict):
        return None
    filename = entry.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None
    out = {
        "filename": filename,
        "path": entry.get("path") if isinstance(entry.get("path"), str) else "",
        "title": entry.get("title") if isinstance(entry.get("title"), str) else filename,
        "description": (entry.get("description")
                        if isinstance(entry.get("description"), str) else ""),
        "type": entry.get("type") if isinstance(entry.get("type"), str) else "fact",
        "created_at": (entry.get("created_at")
                       if isinstance(entry.get("created_at"), str) else ""),
        "tentative": bool(entry.get("tentative")),
    }
    return out


def _dedup_rank(entry: dict) -> tuple[str, str]:
    """Order two entries claiming the same (root, filename).

    Newest wins, and the serialised entry breaks a tie — a total order over
    the content itself, so the survivor is the same whichever shard was read
    first. Anything weaker leaves the merged view depending on directory
    iteration order.
    """
    return (
        str(entry.get("created_at", "")),
        json.dumps(entry, sort_keys=True, default=str),
    )


def _age_note(shard: dict) -> str:
    """How stale a shard is, in words, or empty when it is fresh enough.

    Only shown past a threshold: a timestamp on every root would be noise, but
    a view that silently presents month-old entries as current is worse.
    """
    written = shard.get("written_at")
    if not isinstance(written, str) or not written:
        return ", never reported when it was published"
    try:
        when = datetime.fromisoformat(written.replace("Z", "+00:00"))
    except ValueError:
        return ", publication date unreadable"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - when).days
    if days < 0:
        # Clocks disagree across machines; saying so beats "published -3d ago".
        return ", published with a clock ahead of this one"
    return f", last published {days}d ago" if days >= _STALE_AFTER_DAYS else ""


def render_block(
    cwd: str | os.PathLike[str] | None = None,
    type_order: list[str] | None = None,
    max_entries: int = _MAX_FEDERATED_ENTRIES,
) -> str:
    """The federated view, as a block to append after the local index.

    Deliberately a *separate* block, never merged into ``MEMORY.md``: the
    local index stays byte-identical while only other instances write, so the
    injected prefix keeps its cache. Entries carry absolute paths and the
    owning instance's name, because the agent has to be able to read and grep
    files that live outside its own store.

    Returns "" when there is nothing from anyone else — an empty section would
    just spend context saying so.
    """
    shards = read_shards(cwd)
    if not shards:
        return ""
    order = type_order or []
    rows = merge_entries(shards, order)
    if not rows:
        return ""
    # Resolve this instance's assertions about foreign memories. Their files
    # cannot be edited from here, so the contradiction only becomes visible at
    # the moment both sides are rendered together — which is here.
    contested = _external_supersessions(cwd)
    for row in rows:
        # Matched on the root id, not the label: labels are directory names
        # and two instances can share one, which would pin the claim on the
        # wrong instance's memory.
        claim = f"{row.get('root_id')}:{row.get('filename')}"
        if claim in contested:
            row["contested_by"] = contested[claim]

    # Two ceilings, because the count alone doesn't bound the cost: one root
    # with very long descriptions could still swamp the window.
    shown: list[dict] = []
    budget = _MAX_FEDERATED_CHARS
    for row in rows[:max_entries]:
        cost = len(str(row.get("title", ""))) + len(str(row.get("description", ""))) \
            + len(str(row.get("path", "")))
        if shown and cost > budget:
            break
        if cost > budget:
            # First entry and already over: include it, but truncated. Letting
            # it through whole would make the ceiling advisory, and dropping it
            # would render an empty view for a store that has content. Title
            # and path are not negotiable — the path is how the reader gets the
            # rest — so the description absorbs the whole cut.
            fixed = len(str(row.get("title", ""))) + len(str(row.get("path", "")))
            row = dict(row,
                       description=str(row.get("description", ""))[
                           :max(0, budget - fixed)])
            cost = min(cost, max(fixed, budget))
        budget -= cost
        shown.append(row)
    dropped = len(rows) - len(shown)
    lines = [
        "<foldcrumbs-federated>",
        "Memory from this project's other agent instances. Same project, "
        "separate stores: read or grep the paths below to see the full text. "
        "These files are READ-ONLY from here — record your own conclusions in "
        "your own store instead of editing them.",
        "",
    ]
    for shard in sorted(shards, key=lambda s: s.get("label", "")):
        avail = shard.get("available", True)
        note = "" if avail else " — UNREACHABLE, entries may be out of date"
        if avail is None:
            note = " — did not respond in time, entries may be out of date"
        lines.append(f"- {shard.get('label', '?')}: {shard.get('memory_dir', '?')}"
                     f"{note}{_age_note(shard)}")
    lines.append("")
    for e in shown:
        tag = " *(tentative)*" if e.get("tentative") else ""
        if e.get("contested_by"):
            tag += (f" *(your store records this as obsolete: "
                    f"{e['contested_by']})*")
        hook = e.get("description") or e.get("title") or ""
        lines.append(
            f"- [{e.get('root_label')}] {e.get('title')} — {hook}{tag}\n"
            f"  {e.get('path')}"
        )
    if dropped:
        # Say what was left out. A silently truncated view reads as complete.
        lines.append("")
        lines.append(
            f"({dropped} further entries not shown — grep the paths above)")
    lines.append("</foldcrumbs-federated>")
    return "\n".join(lines)


def _external_supersessions(cwd: str | os.PathLike[str] | None = None,
                            records=None) -> dict[str, str]:
    """Local claims of the form "<root label>:<filename> is obsolete".

    Written by the distillation contradiction pass when the memory it
    obsoletes belongs to another instance. Kept on our own record because
    theirs is not ours to edit; surfaced here so the reader is not left
    believing a superseded entry still holds.
    """
    from foldcrumbs import store

    # ``records`` lets a caller that has already read the local store hand it
    # over. Re-reading it here doubled the Markdown parsed by every federated
    # recall, and the foreign-scan timeout does not bound this pass.
    if records is None:
        records = store.iter_memories(cwd)
    out: dict[str, str] = {}
    for rec in records:
        if rec.status != "active":
            continue
        for claim in getattr(rec, "supersedes_external", None) or []:
            if ":" in claim:
                out.setdefault(claim, rec.title)
    return out


def _same_memory_dir(recorded: object, expected: str) -> bool:
    """Whether a shard describes the directory this root actually uses.

    String equality first, then filesystem identity. Paths are stored
    unresolved on purpose, so a root reached through a symlink or an alternate
    spelling yields a different string for the same directory — and comparing
    text alone rejected its shards for good, since publishing through the alias
    kept producing the same rejected value.

    Anything that cannot be established counts as different: serving a shard
    from a layout the root no longer uses is worse than hiding one until the
    next publication.
    """
    if not isinstance(recorded, str) or not recorded:
        return False
    if recorded == expected:
        return True
    same = federation._bounded(
        lambda: os.path.samefile(recorded, expected),
        federation._REGISTRY_PROBE_TIMEOUT)
    return same is True


def _type_rank(type_name: str, order: list[str]) -> int:
    try:
        return order.index(type_name)
    except ValueError:
        return len(order)


def merge_entries(shards: list[dict], type_order: list[str]) -> list[dict]:
    """Flatten shards into one deterministically ordered list.

    The key is total — (type, created_at desc, root id, filename) — so every
    instance derives the same order from the same data without a shared file
    to agree through. Determinism is what makes the shared artefact
    unnecessary.
    """
    # One root cannot hold two files with one name, so a repeat is a malformed
    # shard. Keeping both would make the order depend on which copy arrived
    # first — but so would keeping *whichever came first*, since the two copies
    # can carry different titles and timestamps and would land in different
    # places. The winner is therefore chosen by content, not by arrival.
    by_key: dict[tuple[str, str], dict] = {}
    for shard in shards:
        rid = shard.get("root_id", "")
        for e in shard.get("entries", []):
            row = _clean_entry(e)
            if row is None:
                continue
            row = {
                **row,
                "root_id": rid,
                "root_label": str(shard.get("label", rid)),
                "available": shard.get("available", True),
            }
            key = (rid, row["filename"])
            current = by_key.get(key)
            if current is None or _dedup_rank(row) > _dedup_rank(current):
                by_key[key] = row
    rows = list(by_key.values())
    # Stable sort, least significant key first. A string can't be negated for
    # a descending pass, so time gets its own reversed sort in between.
    # (root_id, filename) is unique after the dedup above, which is what makes
    # the composite key total rather than merely usually-distinguishing.
    rows.sort(key=lambda e: (e.get("root_id", ""), e.get("filename", "")))
    rows.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    rows.sort(key=lambda e: _type_rank(e.get("type", ""), type_order))
    return rows
