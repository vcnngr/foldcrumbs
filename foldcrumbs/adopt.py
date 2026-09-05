"""FL-1 — explicit adoption of memories from federated roots.

One memory at a time, by explicit command, fail-before-write (design
docs/design/fleet-learning.md rev 2). What makes this module different
from a sync is precisely what it refuses to do:

* no batch adoption, no background job, no propagation;
* NOTHING is ever written to the source root — the original is read-only;
* the declared provenance (``source: adopted:<root>:<id>`` in the copy's
  frontmatter) is inert documentation: any importer can forge it. The
  operational truth is the LOCAL LEDGER (``.adoptions.json``), which only
  this module writes, which never travels with memory files, and which
  import/migrate do not populate (RT F1).

Refusals are visible and ordered: every check runs before the first
write, and the decisive checks run AGAIN under the adoption lock, so two
concurrent adopts cannot both see a free destination (RT r2 obligation 1).

Identity (RT F2): an original whose id was minted at parse time
(``id_missing``), whose id violates the grammar below, or whose id is
duplicated inside its own root, has no stable identity to key the ledger
on — adoption refuses and tells the owner to re-save the memory.

Collision (RT F3): ``write_memory`` slugs by type+title and would
``os.replace`` over an unrelated local homonym. Adoption therefore writes
with create-only semantics: an occupied destination is ALWAYS refused,
even when the occupying file carries the same id as the foreign original.
There is no --force in FL-1.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, federation, redact, store
from .schema import MemoryRecord

LEDGER = ".adoptions.json"
_LOCK_WAIT_SECONDS = 10.0

# FL-1 defines the id grammar explicitly (Kimi N1: the schema itself does
# not validate record ids). Safe charset, no separators that could forge a
# `root:id` pair, no control characters, no newlines. uuid4 fits.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


class AdoptError(Exception):
    """A visible refusal. Adoption never fails silently."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_path(cwd=None) -> Path:
    return config.memory_dir(cwd) / LEDGER


def _adopt_lock_dir() -> Path:
    d = Path(config.STATE_DIR) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / "adopt"


def read_ledger(cwd=None) -> dict:
    """The local attestation ledger. FAIL-CLOSED (RT r2 obligation 3).

    Absent file == no adoptions yet (a fresh store is a legal state).
    Present but unreadable / not a dict / structurally invalid == refuse:
    unlike the recalls sidecar, this is attestation data — degrading it to
    {} would let a corrupt ledger authorize a second live copy.

    "Present" is checked with lexists (RT F2): a dangling symlink is a
    present-but-unreadable ledger, not an absent one.
    """
    path = _ledger_path(cwd)
    if not os.path.lexists(path):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdoptError(
            f"adoption ledger unreadable ({path.name}); refusing to adopt "
            f"until it is fixed or removed: {exc}") from exc
    if not isinstance(data, dict):
        raise AdoptError(
            f"adoption ledger structurally invalid ({path.name}): expected "
            f"an object, refusing to adopt")
    for key, entry in data.items():
        if not isinstance(key, str) or not _ID_RE.match(key):
            raise AdoptError(
                f"adoption ledger structurally invalid ({path.name}): "
                f"key {key!r} is not a memory id, refusing to adopt")
        # RT F2: an entry missing its required fields is corruption, not a
        # thin record — accepting it would free the dedup key and allow a
        # second live copy of the same original.
        if not isinstance(entry, dict):
            raise AdoptError(
                f"adoption ledger structurally invalid ({path.name}): "
                f"entry {key!r}, refusing to adopt")
        for field, check in (("root_id", federation.valid_id),
                             ("memory_id", lambda v: isinstance(v, str)
                              and bool(_ID_RE.match(v))),
                             ("filename", lambda v: isinstance(v, str) and bool(v)),
                             ("adopted_at", lambda v: isinstance(v, str) and bool(v))):
            if not check(entry.get(field)):
                raise AdoptError(
                    f"adoption ledger entry {key[:8]}… is corrupt ({field} "
                    f"missing or invalid); refusing to adopt until the "
                    f"ledger is fixed")
    return data


def _write_ledger(data: dict, cwd=None) -> None:
    path = _ledger_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _split_ref(ref: str) -> tuple[str, str]:
    root_id, sep, mem_ref = ref.partition(":")
    if not sep or not root_id.strip() or not mem_ref.strip():
        raise AdoptError(
            "expected <root_id>:<memory-file>; run `foldcrumbs roots` for ids")
    return root_id.strip(), mem_ref.strip()


def _resolve_in_root(root: federation.RootRef, mem_ref: str,
                     cwd=None) -> MemoryRecord:
    """Resolve filename-or-title inside the SOURCE root, fail-closed.

    A title matching several memories is ambiguous: refuse rather than
    guess (RT r2, editorial note on title resolution).
    """
    memdir = root.memory_dir(cwd)
    if not memdir.is_dir():
        raise AdoptError(
            f"root {root.label} ({root.id[:8]}) is unavailable: "
            f"{memdir} is not a directory")
    matches: list[MemoryRecord] = []
    for rec in store.iter_memories_in(memdir):
        if rec.filename() == mem_ref:
            matches = [rec]  # exact filename wins outright
            break
        if rec.title == mem_ref:
            matches.append(rec)
    if not matches:
        raise AdoptError(f"memory {mem_ref!r} not found in root {root.label}")
    if len(matches) > 1:
        raise AdoptError(
            f"memory ref {mem_ref!r} is ambiguous in root {root.label}: "
            f"{len(matches)} memories share that title — use the filename")
    return matches[0]


def _check_identity(src: MemoryRecord, root: federation.RootRef,
                    cwd=None) -> None:
    """RT F2: stable, grammatical, unique identity or refuse."""
    if src.id_missing:
        raise AdoptError(
            "original has no stable id (legacy record, id re-minted on every "
            "read) — ask its owner to re-save it in their store")
    if not _ID_RE.match(src.id):
        raise AdoptError(
            "original id fails the identity grammar (unsafe characters or "
            "length); refusing to key an adoption on it")
    dupes = 0
    report: dict = {}
    for rec in store.iter_memories_in(root.memory_dir(cwd), report=report):
        if rec.id == src.id:
            dupes += 1
            if dupes > 1:
                raise AdoptError(
                    f"ambiguous id in source root: {dupes} memories in "
                    f"{root.label} declare id {src.id[:8]}…")
    # RT round-2 F3: an incomplete scan (unreadable files) cannot PROVE
    # uniqueness. Refuse rather than adopt on a negative that was never
    # fully established.
    if not report.get("complete", False):
        raise AdoptError(
            f"source root {root.label} could not be scanned completely "
            f"(unreadable files?); refusing to claim id uniqueness — "
            f"fix readability and retry")


def _check_live(src: MemoryRecord) -> None:
    """RT F4: only live knowledge is adoptable.

    status == active AND not expired. Adopting an expired-but-active record
    would revive, in this store, a memory the source already retired from
    recall. Dead history is consulted where it lives (graph path/transit),
    never copied.
    """
    if src.status != "active":
        raise AdoptError(
            f"original is not live (status={src.status}); adoption copies "
            f"live knowledge, history stays in its own root")
    if src.is_expired:
        raise AdoptError(
            "original is not live (expired); adopting it would revive a "
            "retired memory")


def _copy_of(src: MemoryRecord, root_id: str, note: str = "",
             as_type: str | None = None) -> MemoryRecord:
    """Build the local copy per the design's field contract (RT F4/F5)."""
    copy = MemoryRecord(
        title=src.title,
        content=redact.scrub(src.content),   # scrub BEFORE the write
        type=(as_type or src.type),
        description=src.description,
        # id: fresh, persisted — the copy is THIS store's memory
        id=str(uuid.uuid4()),
        confidence=min(src.confidence, 0.8),
        provenance="imported",
        status="active",
        tags=list(src.tags),
        # declaration, inert: built from the registry root id and the
        # VERIFIED original id — never inherited from src.source
        source=f"adopted:{root_id}:{src.id}",
        validation_count=0,                  # no inherited validations
        # created_at/updated_at default to now (adoption time)
        expires_at=src.expires_at,           # future expiry preserved
    )
    copy.contradiction_detected = False      # no inherited disputes
    # relations_json, superseded_by, transit, outcome*, source_path and
    # operational extra_meta are NOT copied: a fresh MemoryRecord carries
    # none of them by construction.
    # RT round-2 F1: the note lives ONLY in the ledger (json-encoded there,
    # so newlines are data). Never in extra_meta: the frontmatter
    # serializer interpolates values raw, so a multiline note would forge
    # keys (id/source/validation_count) on the written file.
    return copy


def _dedup_hit(ledger: dict, root_id: str, src_id: str,
               cwd=None) -> str | None:
    """Ledger-keyed dedup (RT F1): returns the live local filename or None.

    Only ledger entries attest an adoption; a forged `source: adopted:…`
    arriving via import has no entry and blocks nothing. An entry whose
    local copy is gone or no longer visible does not occupy the key: the
    join is on the LOCAL memory id, verified in this store, filename is a
    locator only (RT r2 obligation 3).
    """
    stale: list[str] = []
    for local_id, entry in list(ledger.items()):
        if entry.get("root_id") != root_id or entry.get("memory_id") != src_id:
            continue
        # is the attested copy still live in this store?
        report: dict = {}
        found = None
        for rec in store.iter_memories_in(config.memory_dir(cwd), report=report):
            if rec.id == local_id:
                found = rec
                break
        if found is not None:
            if found.status == "active" and not found.is_expired:
                return found.filename()
            # dead (superseded/expired/deleted): free the key
            stale.append(local_id)
            continue
        # Not found. RT round-2 F3: a MISSING copy frees the key only when
        # the scan was complete; an incomplete scan (unreadable file) cannot
        # prove the copy is gone, so keep the attestation and let the
        # collision/identity checks downstream refuse — never silently drop
        # the entry and authorize a second adoption.
        if not report.get("complete", False):
            raise AdoptError(
                "this store could not be scanned completely (unreadable "
                "files?); refusing to decide whether an adopted copy is "
                "still present — fix readability and retry")
        stale.append(local_id)
    for local_id in stale:
        ledger.pop(local_id, None)
    return None


def _create_only(memdir: Path, rec: MemoryRecord) -> Path:
    """Atomic create-ONLY write: refuses an occupied destination.

    ``write_memory`` would ``os.replace`` over an unrelated homonym
    (RT F3). Here the final step is ``os.link``: it fails FileExistsError
    when the target exists. RT round-2 F4: any OTHER os.link failure
    (EOPNOTSUPP, EACCES, EIO...) is a visible refusal — the former
    exists()+os.replace fallback could clobber a writer that occupied the
    destination between the check and the replace. No fallback: a create-only
    guarantee is only as strong as its weakest path. The tmp file is removed
    on every path.
    """
    memdir.mkdir(parents=True, exist_ok=True)
    target = memdir / rec.filename()
    fd, tmp = tempfile.mkstemp(dir=str(memdir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rec.to_markdown())
        try:
            os.link(tmp, target)   # fails FileExistsError if occupied
        except FileExistsError:
            raise AdoptError(
                f"destination collision: {target.name} already exists in "
                f"this store — rename or supersede the local memory first "
                f"(adoption never overwrites)") from None
        except OSError as exc:
            raise AdoptError(
                f"cannot create {target.name} atomically without "
                f"hard-link support ({exc}); refusing rather than risking "
                f"an overwrite") from None
        return target
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def adopt(ref: str, cwd=None, note: str = "",
          as_type: str | None = None) -> dict:
    """Adopt ONE memory from a federated root. Returns a result dict:
    ``{"ok": True, "filename", "id", "source"}`` or ``{"ok": False,
    "reason": …}``. Refusals are values, not exceptions, so the CLI and
    MCP can report them uniformly; the ledger fail-closed path raises
    AdoptError which callers turn into a refusal too.
    """
    try:
        return _adopt(ref, cwd=cwd, note=note, as_type=as_type)
    except AdoptError as exc:
        return {"ok": False, "reason": str(exc)}


def _adopt(ref: str, cwd=None, note: str = "",
           as_type: str | None = None) -> dict:
    root_id, mem_ref = _split_ref(ref)
    if not federation.valid_id(root_id):
        raise AdoptError(f"root id {root_id!r} is not a valid registry id")
    root = federation.get_root(root_id)
    if root is None:
        raise AdoptError(
            f"unknown root {root_id[:8]}…; run `foldcrumbs roots` "
            f"(is it registered and not unregistered?)")

    my_dir = config.memory_dir(cwd)
    if Path(os.path.realpath(root.memory_dir(cwd))) == \
            Path(os.path.realpath(my_dir)):
        raise AdoptError("that root IS this store — nothing to adopt")

    # --- fail-before-write: every check runs before any byte is written ---
    src = _resolve_in_root(root, mem_ref, cwd)
    _check_identity(src, root, cwd)
    _check_live(src)
    ledger = read_ledger(cwd)          # fail-closed on corrupt ledger
    hit = _dedup_hit(ledger, root.id, src.id, cwd)
    if hit:
        raise AdoptError(f"already adopted as {hit}")
    copy = _copy_of(src, root.id, note=note, as_type=as_type)
    if (my_dir / copy.filename()).exists():
        raise AdoptError(
            f"destination collision: {copy.filename()} already exists in "
            f"this store — rename or supersede the local memory first "
            f"(adoption never overwrites)")

    # --- decisive section, under the adoption lock (RT r2 obligation 1):
    # checks re-run, then memory file, then ledger. ---
    with federation.file_lock(_adopt_lock_dir(), wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise AdoptError(
                "another adoption holds the lock; refusing rather than "
                "racing it (retry in a moment)")
        ledger = read_ledger(cwd)      # re-read under lock, fail-closed
        hit = _dedup_hit(ledger, root.id, src.id, cwd)
        if hit:
            raise AdoptError(f"already adopted as {hit}")
        path = _create_only(my_dir, copy)   # create-only: collision = refuse
        ledger[copy.id] = {
            "root_id": root.id,
            "memory_id": src.id,
            "filename": copy.filename(),
            "adopted_at": _now_iso(),
        }
        if note:
            ledger[copy.id]["note"] = note
        try:
            _write_ledger(ledger, cwd)
        except OSError as exc:
            # The memory file exists WITHOUT attestation (design point 6).
            # Visible error; a retry of the same adoption will refuse on the
            # occupied destination — no silent corruption, no invented entry.
            raise AdoptError(
                f"memory written to {path.name} but the ledger write failed "
                f"({exc}); the copy exists unattested — inspect "
                f"{LEDGER} before retrying") from exc
    return {"ok": True, "filename": copy.filename(), "id": copy.id,
            "source": copy.source}


def search_candidates(query: str, root_id: str, limit: int = 10,
                      cwd=None) -> list[dict]:
    """List live candidates in a federated root — read-only, adopts nothing.

    The reputation hint (previous bad outcomes from this root) joins in
    FL-2, where the outcome data exists.
    """
    if not federation.valid_id(root_id):
        raise AdoptError(f"root id {root_id!r} is not a valid registry id")
    root = federation.get_root(root_id)
    if root is None:
        raise AdoptError(f"unknown root {root_id[:8]}…")
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    out: list[dict] = []
    for rec in store.iter_memories_in(root.memory_dir(cwd)):
        if rec.status != "active" or rec.is_expired:
            continue
        hay = f"{rec.title}\n{rec.content}".lower()
        score = sum(1 for w in words if w in hay)
        if not words or score:
            out.append({"score": score, "id": rec.id, "title": rec.title,
                        "filename": rec.filename(), "type": rec.type,
                        "root": root.label})
    out.sort(key=lambda d: (-d["score"], d["filename"]))
    return out[:limit]
