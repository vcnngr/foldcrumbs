"""G2 — the relation proposal queue.

Design: docs/design/g2-extraction.md (REV-2 + amendments E1-E6 + E4-bis).
Every clause below cites the design decision it implements.

Model-generated relations NEVER land in the store directly. They enter this
queue as ``pending`` proposals; only a human ``promote`` materializes an arc
(prov=manual). Reading may walk pending proposals only behind the explicit
per-query ``--include-inferred`` flag (amendment to REV-2, approved by the
owner) — never by default, never silently.

Posture rules that shape the code:

* E1 — dedup is total: a proposal is refused when the store ALREADY has the
  same triple (any prov) or when the queue has it in ANY status (pending,
  promoted, rejected). A reject is persistent suppression; only an explicit
  human ``reopen`` revives a triple.
* E2 — every read-check-write on the queue happens under one
  ``federation.file_lock``. The conflicts.py pattern was checked during R3
  and does NOT lock — this module must not inherit that gap.
* SERIALIZATION (GPT code-RT P0-1) — the queue lock is the ONE serialization
  point for EVERY triple mutation: submit reads the store and rewrites the
  queue under it, and ``relations.add_relation`` acquires it FIRST (before
  the per-memory lock). Lock order is always queue → memory; no path takes
  them in reverse. This is what makes "store triple OR queued triple blocks
  the other" hold against concurrent writers, not just against crashes.
* E4-bis — promote is a recoverable protocol, not a transaction. The arc is
  written into the store FIRST, tagged with the proposal_id; only then is the
  queue row marked promoted. A crash in between converges on retry; promote
  never declares success unless the exact tagged arc is observable (GPT
  code-RT P0-1: an untagged pre-existing triple is attested under the lock,
  never papered over).
* PER-STORE QUEUE (GPT code-RT P0-3) — the queue is namespaced by the store
  it serves (``memory_dir(cwd)``), not machine-wide: two projects under one
  STATE_DIR never see each other's proposals.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, federation, store

PROPOSAL_STATUSES = ("pending", "promoted", "rejected")
MAX_PER_SESSION = 10          # D2: cap per distill session
_LOCK_WAIT_SECONDS = 10.0


class ProposalError(ValueError):
    """A proposal operation that violates the protocol. Always explicit."""


class ProposalLockBusy(RuntimeError):
    """The queue lock stayed held past the wait. Fail closed and visibly."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_key(cwd=None) -> str:
    """Stable identity of the store this queue serves: the resolved absolute
    memory dir. Two cwd values resolving to the same store share a queue;
    different stores get different queues (GPT code-RT P0-3)."""
    root = Path(config.memory_dir(cwd)).resolve()
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def queue_path(cwd=None) -> Path:
    return Path(config.STATE_DIR) / "proposals" / _store_key(cwd) \
        / "relation_proposals.jsonl"


def _lock_path(cwd=None) -> Path:
    return Path(config.STATE_DIR) / "proposals" / _store_key(cwd) / "lock"


# --- reading ----------------------------------------------------------------

def load_all(cwd=None) -> list[dict]:
    """Tolerant read: one corrupt line must not blind the rest of the queue."""
    path = queue_path(cwd)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("proposal_id"):
            rows.append(row)
    return rows


def get(proposal_id: str, cwd=None) -> dict | None:
    for row in load_all(cwd):
        if row.get("proposal_id") == proposal_id:
            return row
    return None


def counts(cwd=None) -> dict:
    out = {"pending": 0, "promoted": 0, "rejected": 0}
    for row in load_all(cwd):
        st = row.get("status")
        if st in out:
            out[st] += 1
    return out


def _rewrite(rows: list[dict], cwd=None) -> None:
    """Replace the whole queue file. Caller MUST hold the queue lock.
    tmp + os.replace so a crash never leaves a half-written queue."""
    path = queue_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
                   encoding="utf-8")
    os.replace(tmp, path)


def _dedup_key(row: dict) -> tuple:
    t = row.get("target") or {}
    return (row.get("subject_id"), row.get("predicate"), t.get("id"))


def _store_has_triple(subject_id: str, predicate: str, target_id: str,
                      cwd=None) -> bool:
    """E1.1 — the store already decided this triple (any prov)."""
    from . import relations
    for rec in store.load_all(cwd):
        if rec.id != subject_id:
            continue
        for rel in relations.parse(rec.relations_json):
            t = rel.get("t")
            if rel.get("p") == predicate and isinstance(t, dict) \
                    and t.get("k") == "m" and t.get("id") == target_id:
                return True
    return False


def _store_has_proposal_tag(proposal_id: str, cwd=None) -> bool:
    """E4-bis recovery: an arc tagged with this proposal_id already exists."""
    from . import relations
    for rec in store.load_all(cwd):
        for rel in relations.parse(rec.relations_json):
            if rel.get("proposal_id") == proposal_id:
                return True
    return False


# --- writing (always under the queue lock, E2 + P0-1 serialization) ---------

def _envelope_valid(predicate: str | None, evidence: str | None) -> bool:
    """ONE validator for the proposal envelope, used on the way in (submit)
    and on the way out (overlay_edges) alike (GPT code-RT re-review P1-1):
    a known predicate and a non-empty evidence quote. A queue row that is
    malformed on disk (hand-edited, truncated) is dropped from the overlay —
    never surfaced. A direct write without evidence is a different shape
    (relations.py records its own uncertainty, prov=inferred) and does not
    go through this validator."""
    from . import relations
    return bool(predicate) and predicate in relations.PREDICATES \
        and bool(str(evidence or "").strip())


def submit(raw: list[dict], prov: str = "inferred",
           cap: int = MAX_PER_SESSION, cwd=None) -> dict:
    """Validate + enqueue model proposals. Returns measurable counts (E6).

    ``raw`` items: {subject_id, predicate, target_id, evidence, confidence}.
    Invalid items are dropped at parse (D4) — never written, never raised:
    the queue is fail-soft on the way in, fail-closed on the way out.

    The store is re-read for the dedup check INSIDE the queue lock: writers
    that add arcs to the store take the same lock first (relations.add_
    relation), so the "store triple blocks the proposal" check and the
    enqueue are one indivisible step against concurrent writers
    (GPT code-RT P0-1).
    """
    from . import relations

    stats = {"written": 0, "invalid": 0, "dup_store": 0, "dup_queue": 0,
             "capped": 0}
    valid: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            stats["invalid"] += 1
            continue
        subject = str(item.get("subject_id") or "").strip()
        target = str(item.get("target_id") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if predicate not in relations.PREDICATES \
                or not subject or not target or subject == target:
            stats["invalid"] += 1
            continue
        # P1-1 (GPT code-RT): a model proposal without an evidence quote is
        # not a supported claim — the envelope validator is ONE function,
        # shared with overlay_edges on the way out.
        if not _envelope_valid(predicate, evidence):
            stats["invalid"] += 1
            continue
        try:
            confidence = min(max(float(item.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.5
        if prov != "manual":            # D1: non-human writes cap at 0.5
            confidence = min(confidence, 0.5)
        valid.append({
            "subject_id": subject, "predicate": predicate, "target_id": target,
            "evidence": evidence[:2000], "confidence": round(confidence, 2),
        })

    with federation.file_lock(_lock_path(cwd), wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise ProposalLockBusy(
                "proposal queue is locked by another writer; nothing written")
        # Store and queue ids read under the lock: one consistent view.
        known = {m.id for m in store.load_all(cwd)}
        rows = load_all(cwd)
        queued_keys = {_dedup_key(r) for r in rows}
        for item in valid:
            key = (item["subject_id"], item["predicate"], item["target_id"])
            if key[0] not in known or key[2] not in known:
                stats["invalid"] += 1
                continue
            if stats["written"] >= cap:
                stats["capped"] += 1
                continue
            if _store_has_triple(*key, cwd=cwd):
                stats["dup_store"] += 1
                continue
            if key in queued_keys:
                stats["dup_queue"] += 1
                continue
            row = {
                "proposal_id": uuid.uuid4().hex,
                "subject_id": item["subject_id"],
                "predicate": item["predicate"],
                "target": {"k": "m", "id": item["target_id"]},
                "evidence": item["evidence"],
                "confidence": item["confidence"],
                "prov": prov,
                "status": "pending",
                "created_at": _now_iso(),
                "decided_at": None,
            }
            rows.append(row)
            queued_keys.add(key)
            stats["written"] += 1
        _rewrite(rows, cwd)
    return stats


# --- decisions ---------------------------------------------------------------

def _decide(proposal_id: str, new_status: str, cwd=None) -> dict:
    from . import relations

    with federation.file_lock(_lock_path(cwd), wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise ProposalLockBusy(
                "proposal queue is locked by another writer; nothing changed")
        rows = load_all(cwd)
        row = next((r for r in rows if r.get("proposal_id") == proposal_id),
                   None)
        if row is None:
            raise ProposalError(f"no proposal with id {proposal_id!r}")
        if row.get("status") == new_status:
            return {"action": "noop", "status": new_status,
                    "proposal_id": proposal_id}
        if new_status == "promoted":
            if row.get("status") != "pending":
                return {"action": "noop", "status": row.get("status"),
                        "proposal_id": proposal_id,
                        "note": "only pending proposals can be promoted"}
            # E4-bis step 3: the arc FIRST, tagged with the proposal_id.
            # The queue lock is already held here; add_relation is told so
            # it takes only the memory lock (queue → memory order).
            added = relations.add_relation(
                row["subject_id"], row["predicate"],
                {"k": "m", "id": row["target"]["id"]},
                evidence=row.get("evidence") or "",
                confidence=row.get("confidence", 0.5),
                prov="manual", proposal_id=proposal_id, cwd=cwd,
                _queue_lock_held=True)
            if not added:
                # The triple already exists in the store. Convergence is only
                # real if the tagged arc is now observable — otherwise a human
                # would be told "promoted" while no arc carries this proposal
                # (GPT code-RT P0-1).
                if not _store_has_proposal_tag(proposal_id, cwd=cwd):
                    raise ProposalError(
                        f"promote refused: the triple exists in the store "
                        f"without this proposal's tag and could not be "
                        f"attested (proposal {proposal_id}); inspect with "
                        f"`foldcrumbs graph doctor` — nothing was marked "
                        f"promoted")
            # E4-bis step 4: only now mark the queue row.
        if new_status not in PROPOSAL_STATUSES:
            raise ProposalError(f"unknown status {new_status!r}")
        if new_status == "pending" and row.get("status") != "rejected":
            return {"action": "noop", "status": row.get("status"),
                    "proposal_id": proposal_id,
                    "note": "only rejected proposals can be reopened"}
        row["status"] = new_status
        row["decided_at"] = None if new_status == "pending" else _now_iso()
        _rewrite(rows, cwd)
    return {"action": "ok", "status": new_status, "proposal_id": proposal_id}


def promote(proposal_id: str, cwd=None) -> dict:
    """Human promotion: pending -> store arc (prov=manual) + promoted.
    Idempotent; crash-safe per E4-bis (arc first, tagged; status second).
    Success is declared ONLY when the exact tagged arc is observable."""
    return _decide(proposal_id, "promoted", cwd)


def reject(proposal_id: str, cwd=None) -> dict:
    """Persistent suppression (E1): the triple is not re-proposed until a
    human reopens it."""
    return _decide(proposal_id, "rejected", cwd)


def reopen(proposal_id: str, cwd=None) -> dict:
    """Human action only (E1): rejected -> pending again."""
    return _decide(proposal_id, "pending", cwd)


# --- read-side overlay for graph_path (E4) -----------------------------------

def overlay_edges(cwd=None) -> list[dict]:
    """Pending proposals eligible for the --include-inferred overlay.

    Only rows that are pending, valid, and whose endpoints are alive enter;
    rejected/promoted/malformed never do. A pending row whose proposal_id is
    already materialized in the store (the E4-bis crash window) is treated as
    promoted — the store copy is authoritative, the overlay stays silent so a
    path never shows the same edge twice.
    """
    from . import relations

    mems = store.load_all(cwd)
    alive = {m.id for m in mems if m.status == "active" and not m.is_expired}
    tagged = set()
    store_triples = set()
    for m in mems:
        for rel in relations.parse(m.relations_json):
            if rel.get("proposal_id"):
                tagged.add(rel["proposal_id"])
            t = rel.get("t")
            if isinstance(t, dict) and t.get("k") == "m":
                store_triples.add((m.id, rel.get("p"), t.get("id")))
    edges: list[dict] = []
    for row in load_all(cwd):
        if row.get("status") != "pending":
            continue
        if row.get("proposal_id") in tagged:
            continue                      # already materialized: store wins
        subject = row.get("subject_id")
        t = row.get("target") or {}
        target = t.get("id") if t.get("k") == "m" else None
        if subject not in alive or target not in alive:
            continue
        if row.get("predicate") not in relations.PREDICATES:
            continue
        # P1-1 (GPT code-RT re-review): the SAME envelope validator as submit
        # — a malformed queue row (hand-edited evidence blanked out) never
        # surfaces through the overlay.
        if not _envelope_valid(row.get("predicate"), row.get("evidence")):
            continue
        # GPT code-RT P0-1: if the triple already exists in the store (any
        # prov — e.g. a direct write that raced the proposal), the store copy
        # is authoritative; the overlay stays silent so a path never shows
        # the same edge twice.
        if (subject, row.get("predicate"), target) in store_triples:
            continue
        edges.append({
            "p": row["predicate"],
            "t": {"k": "m", "id": target},
            "c": row.get("confidence", 0.5),
            "prov": row.get("prov", "inferred"),
            "e": row.get("evidence") or "",
            "proposal_id": row.get("proposal_id"),
            "_subject": subject,
            "_overlay": True,
        })
    return edges


# --- maintenance view ---------------------------------------------------------

def doctor(cwd=None) -> dict:
    """Queue health, read-only. Includes the E4-bis invariant check: a
    promoted proposal must have its arc in the store (impossible by
    construction — if it is ever observed, report it loudly, never fix it
    silently)."""
    promoted_missing_arc: list[str] = []
    for row in load_all(cwd):
        if row.get("status") == "promoted" \
                and not _store_has_proposal_tag(row["proposal_id"], cwd=cwd):
            promoted_missing_arc.append(row["proposal_id"])
    return {"counts": counts(cwd),
            "promoted_missing_arc": promoted_missing_arc}
