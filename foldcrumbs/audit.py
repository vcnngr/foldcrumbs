"""Store integrity audit + pruning.

Link integrity: the index links to real files on disk (``store.rebuild_index``
is path-based), so a dead link — or an active memory the index doesn't link —
just means the index is stale; ``heal_index`` rebuilds it. Pollution: a memory
whose title/content is a structural tooling artifact (markdown table, code
fence, status glyphs, the local-command caveat — distill's strict detector) is
never durable knowledge and can be pruned. The strict detector deliberately
excludes prose that merely mentions MEMORY.md so legitimate foldcrumbs design notes
are never deleted. Superseded/deleted records keep their
files but drop out of the index/recall; ``prune`` clears those too.
"""

from __future__ import annotations

import re

from . import config, store
from .distill import _is_hard_artifact

_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
# compute_confidence below this is low-trust (stale/contradicted); prune only on
# explicit request, never automatically.
STALE_CONF = 0.3


def _name(m) -> str:
    return m.source_path or m.filename()


def _index_links(cwd=None) -> set[str]:
    p = config.index_path(cwd)
    if not p.exists():
        return set()
    try:
        return set(_LINK_RE.findall(p.read_text(encoding="utf-8")))
    except OSError:
        return set()


def audit(cwd=None) -> dict:
    """Read-only report: dead index links, orphaned active memories (on disk but
    unlinked), artifact pollution, and low-trust/stale memories."""
    linked = _index_links(cwd)
    mems = list(store.iter_memories(cwd))
    active = [m for m in mems if m.status == "active"]
    visible = [m for m in active if not m.is_expired]
    on_disk = {_name(m) for m in mems}
    active_names = {_name(m) for m in visible}
    return {
        "dead_links": sorted(t for t in linked if t not in on_disk),
        # Linked, and the file is right there — but the memory behind it has
        # been retired. Archived, superseded and deleted records keep their
        # files, so this is invisible to the dead-link check, and an index
        # left in that state stays wrong for good: the operation that retired
        # them has already run, and nothing later notices.
        "retired_links": sorted(t for t in linked
                                if t in on_disk and t not in active_names),
        "orphans": sorted(n for n in active_names if n not in linked),
        "pollution": sorted(_name(m) for m in active
                            if _is_hard_artifact(m.title) or _is_hard_artifact(m.content)),
        "stale": sorted(_name(m) for m in active
                        if m.compute_confidence() < STALE_CONF),
        # Active on disk but past their expires_at: invisible everywhere an
        # archived memory is, file untouched. The sweep archives them; until
        # then this list is the user's view of what has lapsed.
        "expired": sorted(_name(m) for m in active if m.is_expired),
        "active": len(active),
        "visible": len(visible),
        "total": len(mems),
    }


def heal_index(cwd=None) -> bool:
    """Rebuild the index if it is stale (dead links or unlinked active memories).

    Cheap and idempotent; returns True iff it rebuilt. Callers that share a store
    across machines should gate this on ``config.distill_enabled()`` so only a
    writing machine repairs (avoids sync churn)."""
    a = audit(cwd)
    if a["dead_links"] or a["orphans"] or a["retired_links"]:
        store.rebuild_index(cwd)
        return True
    return False


def _delete(name: str, cwd=None) -> bool:
    try:
        (config.memory_dir(cwd) / name).unlink()
        return True
    except OSError:
        return False


def prune_artifacts(cwd=None) -> list[str]:
    """Delete active memories whose text is a clear tooling artifact, then rebuild
    the index. Conservative — only unambiguous artifacts. Returns deleted names."""
    removed = [
        _name(m)
        for m in list(store.iter_memories(cwd))
        if store._visible(m) and (_is_hard_artifact(m.title) or _is_hard_artifact(m.content))
    ]
    removed = [n for n in removed if _delete(n, cwd)]
    if removed:
        store.rebuild_index(cwd)
    return removed


# A memory has to be given a chance before it can be judged for having had
# one. Distillation writes `inferred` records at modest confidence, so a
# freshly written memory can already sit below the threshold — archiving it on
# the next run would mean it never got used because it was never offered.
DECAY_GRACE_DAYS = 30


def _has_decayed(m) -> bool:
    """Whether a memory has both fallen below the threshold and had its turn.

    Trust is only half the question. The other half is time: something written
    or re-validated recently has not decayed, it simply has not proved itself
    yet — and ``validate`` moves ``updated_at``, so a memory that was just
    confirmed gets its grace back.

    A memory with no usable date keeps its place. An unknown date is not
    evidence of age, and this step deletes nothing that can be re-earned only
    by being recalled — which cannot happen once it is out of recall.
    """
    if m.compute_confidence() >= STALE_CONF:
        return False
    from datetime import datetime, timezone

    # The most recent date the file actually carries — the newest, not the
    # first one that happens to be there. Both attributes default to "now"
    # when absent, so asking for the value is not enough: a legacy memory
    # holding only created_at would look untouched a second ago and never
    # decay. And preferring updated_at outright is wrong too, because an
    # imported or hand-edited file can carry one older than its creation date,
    # which would archive a memory that was just written.
    dates = []
    if not getattr(m, "updated_at_missing", False) and m.updated_at is not None:
        dates.append(m.updated_at)
    if not getattr(m, "created_at_missing", False) and m.created_at is not None:
        dates.append(m.created_at)
    # Every date here has a zone: the parser reads a naive timestamp as UTC
    # precisely so comparisons cannot raise. Re-normalising would be dead
    # code, and a guard that never fires is a guard nobody maintains — the
    # test holds the parser to it instead.
    if not dates:
        return False
    return (datetime.now(timezone.utc) - max(dates)).days >= DECAY_GRACE_DAYS


def decay(cwd=None, apply: bool = False) -> dict:
    """Archive active memories whose trust has decayed below the threshold.

    The decay itself is not new — ``compute_confidence`` has always applied
    provenance, contradiction and age. What was missing is a step that acts on
    it: a store where nothing ever leaves ends up competing with itself, every
    stale entry taking up the retrieval space of something current.

    Also archives memories past their ``expires_at``. Expiry already hides
    them from index and recall the moment the date passes; the sweep is what
    turns that into a durable state, and it is the only automatic writer here —
    still explicit, still dry-run by default.

    Archived, never deleted. A memory that decayed out of relevance is not a
    memory that was wrong, and ``foldcrumbs restore`` brings it back whole.
    ``prune --apply`` is still the way to remove files for good, and it is
    still a separate, explicit act.

    Explicit and scheduled, never a side effect of recall: reading must not
    silently change what the store contains. Dry-run unless ``apply``.
    """
    candidates = {
        _name(m): round(m.compute_confidence(), 2)
        for m in store.iter_memories(cwd)
        if m.status == "active" and (m.is_expired or _has_decayed(m))
    }
    # Which of the candidates lapsed on their date rather than on trust — the
    # CLI says so, so a user knows which ones to re-date instead of re-trust.
    expired = [
        _name(m)
        for m in store.iter_memories(cwd)
        if m.status == "active" and m.is_expired and _name(m) in candidates
    ]
    archived: list[str] = []
    if apply:
        try:
            for n in candidates:
                if store.set_status(n, "archived", cwd, rebuild=False):
                    archived.append(n)
        finally:
            # Whatever happened above, the index must describe what the store
            # now holds. Skipping it on the way out through an exception would
            # leave archived memories advertised — and heal_index would not
            # notice, since their files are still there. It does now, which is
            # the real backstop for a sweep interrupted outright.
            if archived:
                store.rebuild_index(cwd)
    return {"candidates": candidates, "archived": archived, "applied": apply,
            "expired": sorted(expired)}


def prune(cwd=None, apply: bool = False, include_stale: bool = False) -> dict:
    """Find (and with ``apply``, delete) prune candidates.

    Candidates: superseded/deleted records (files left behind), active artifact
    pollution, and — only with ``include_stale`` — low-trust active memories.
    Dry-run by default; rebuilds the index when it deletes anything."""
    candidates: dict[str, str] = {}
    for m in store.iter_memories(cwd):
        name = _name(m)
        if m.status in ("deleted", "superseded", "archived"):
            candidates[name] = f"{m.status} (file kept until pruned)"
        elif m.status == "active" and (_is_hard_artifact(m.title) or _is_hard_artifact(m.content)):
            candidates[name] = "artifact"
        elif (include_stale and m.status == "active"
              and m.compute_confidence() < STALE_CONF):
            candidates[name] = "stale"
    removed: list[str] = []
    if apply and candidates:
        removed = [n for n in candidates if _delete(n, cwd)]
        if removed:
            store.rebuild_index(cwd)
    return {"candidates": candidates, "removed": removed, "applied": apply}
