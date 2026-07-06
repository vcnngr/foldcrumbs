"""Store integrity audit + pruning.

Link integrity: the index links to real files on disk (``store.rebuild_index``
is path-based), so a dead link — or an active memory the index doesn't link —
just means the index is stale; ``heal_index`` rebuilds it. Pollution: a memory
whose title/content is a structural tooling artifact (markdown table, code
fence, status glyphs, the local-command caveat — distill's strict detector) is
never durable knowledge and can be pruned. The strict detector deliberately
excludes prose that merely mentions MEMORY.md so legitimate engram design notes
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
    on_disk = {_name(m) for m in mems}
    active_names = {_name(m) for m in active}
    return {
        "dead_links": sorted(t for t in linked if t not in on_disk),
        "orphans": sorted(n for n in active_names if n not in linked),
        "pollution": sorted(_name(m) for m in active
                            if _is_hard_artifact(m.title) or _is_hard_artifact(m.content)),
        "stale": sorted(_name(m) for m in active
                        if m.compute_confidence() < STALE_CONF),
        "active": len(active),
        "total": len(mems),
    }


def heal_index(cwd=None) -> bool:
    """Rebuild the index if it is stale (dead links or unlinked active memories).

    Cheap and idempotent; returns True iff it rebuilt. Callers that share a store
    across machines should gate this on ``config.distill_enabled()`` so only a
    writing machine repairs (avoids sync churn)."""
    a = audit(cwd)
    if a["dead_links"] or a["orphans"]:
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
        if m.status == "active" and (_is_hard_artifact(m.title) or _is_hard_artifact(m.content))
    ]
    removed = [n for n in removed if _delete(n, cwd)]
    if removed:
        store.rebuild_index(cwd)
    return removed


def prune(cwd=None, apply: bool = False, include_stale: bool = False) -> dict:
    """Find (and with ``apply``, delete) prune candidates.

    Candidates: superseded/deleted records (files left behind), active artifact
    pollution, and — only with ``include_stale`` — low-trust active memories.
    Dry-run by default; rebuilds the index when it deletes anything."""
    candidates: dict[str, str] = {}
    for m in store.iter_memories(cwd):
        name = _name(m)
        if m.status in ("deleted", "superseded"):
            candidates[name] = "superseded/deleted"
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
