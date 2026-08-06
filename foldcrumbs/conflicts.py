"""The reconciliation queue — ambiguity made visible, never resolved silently.

The contradiction pass at distill time can settle a conflict ("the new memory
makes the old one obsolete": supersede) or reject one ("they coexist": do
nothing). But there is a third outcome the old true/false question had no room
for: *genuinely unsure*. Forcing an unsure answer into "false" is a silent
guess — the memory system keeps serving two statements about one subject and
nobody is told. This module is that third branch's home.

The queue is recomputed from live state every time it is read:

* **flagged pairs** — persisted here (machine-local, per project) when the
  LLM answers ``flag`` — or answers something that is neither true nor false,
  which is confusion and gets treated the same;
* **claims out** — this store's ``supersedes_external`` assertions against
  another instance's memories (their instance is the only one that can retire
  their file, so these wait on them);
* **contested here** — foreign claims against this store's own memories
  (visible here because this instance is the only one that can act on them).

Nothing in this module writes the store. It reads, lists, and suggests the
exact commands that would resolve each item; resolving is the user's act.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, index_shard, store


def _path(cwd: str | os.PathLike[str] | None = None) -> Path:
    # Machine-local, not the store: which pairs were ambiguous depends on what
    # *this* machine's LLM answered, and a synced queue would mix verdicts
    # from machines that may not even agree on the LLM in use.
    return (config.STATE_DIR / "projects"
            / index_shard.project_key(cwd) / "conflicts.json")


def _read(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    try:
        data = json.loads(_path(cwd).read_text(encoding="utf-8"))
        return data.get("pairs", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def _write(pairs: list[dict], cwd: str | os.PathLike[str] | None = None) -> None:
    try:
        _path(cwd).parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_path(cwd).parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"pairs": pairs}, fh, indent=1)
        os.replace(tmp, _path(cwd))
    except OSError:
        pass


def flag_pair(old_name: str, new_name: str, cwd: str | os.PathLike[str] | None = None,
              old_root: str | None = None, reason: str = "ambiguous") -> None:
    """Record that two same-subject memories could not be reconciled.

    Keyed on the pair, so re-distilling the same transcript cannot stack
    duplicates. Best-effort: a failed write only loses the flag, never the
    memories themselves.
    """
    pairs = _read(cwd)
    for p in pairs:
        if (p.get("old"), p.get("old_root"), p.get("new")) == (old_name, old_root, new_name):
            return
    pairs.append({
        "old": old_name,
        "old_root": old_root,
        "new": new_name,
        "reason": reason,
        "flagged_at": datetime.now(timezone.utc).isoformat(),
    })
    _write(pairs, cwd)


def _alive(name: str, root: str | None, cwd) -> bool:
    """Whether the named memory still exists and is active — a pair whose
    sides have been retired (or deleted outright) has been resolved by other
    means and drops out of the queue on the next read."""
    rec = store.get(name, cwd)
    if rec is not None:
        return rec.status == "active"
    if root:
        for m in store.iter_federated(cwd):
            if m.origin_root == root and (m.source_path or m.filename()) == name:
                return True
    return False


def flagged_pairs(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """Persisted ambiguous pairs, minus the ones no longer live on either side."""
    out = []
    for p in _read(cwd):
        if _alive(p.get("new", ""), None, cwd) and \
                _alive(p.get("old", ""), p.get("old_root"), cwd):
            out.append(p)
    return out


def claims_out(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """This store's standing assertions that a foreign memory is obsolete."""
    out: list[dict] = []
    for rec in store.iter_memories(cwd):
        if rec.status != "active":
            continue
        for claim in rec.supersedes_external or []:
            root_id, _, name = claim.partition(":")
            out.append({"claim": claim, "root_id": root_id, "foreign": name,
                        "by": rec.source_path or rec.filename()})
    return out


def contested_here(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """Claims by *other* instances that a memory of THIS store is obsolete.

    The claim lives on the claimant's own record (their store is the only one
    they can write), so finding ours means scanning the other registered
    instances' memories for ``supersedes_external`` entries whose root id is
    ours. A claim whose target file is gone (or no longer active) has been
    resolved already and is not listed.
    """
    from . import federation
    our = next((r for r in federation.iter_roots() if r.is_current()), None)
    if our is None:
        return []
    out: list[dict] = []
    for m in store.iter_federated(cwd):
        for claim in m.supersedes_external or []:
            root_id, _, name = claim.partition(":")
            if root_id != our.id:
                continue
            target = store.get(name, cwd)
            if target is None or target.status != "active":
                continue
            by = f"[{m.origin_root}] {m.source_path or m.filename()}"
            out.append({"name": name, "by": by})
    return out


def queue(cwd: str | os.PathLike[str] | None = None) -> dict[str, list]:
    return {
        "flagged": flagged_pairs(cwd),
        "claims_out": claims_out(cwd),
        "contested_here": contested_here(cwd),
    }


def _label(rec) -> str:
    if rec.is_foreign:
        return f"[{rec.origin_root}] {rec.source_path or rec.filename()}"
    return rec.source_path or rec.filename()


def format_queue(q: dict[str, list], cwd: str | os.PathLike[str] | None = None) -> str:
    """Human-readable queue with the exact command that resolves each item."""
    lines: list[str] = []
    flagged = q.get("flagged", [])
    if flagged:
        lines.append(f"Ambiguous pairs ({len(flagged)}) — the LLM could not tell "
                     "which statement holds:")
        for p in flagged:
            old = p.get("old", "?")
            if p.get("old_root"):
                old = f"[{p['old_root']}] {old}"
            lines.append(f"  - {old}  <->  {p.get('new', '?')}")
            # Only suggest the local action when both sides are local files;
            # a foreign side belongs to its own instance.
            if not p.get("old_root"):
                lines.append(f"      foldcrumbs supersede {p.get('old')} "
                             f"--by {p.get('new')}    (or `forget` the wrong one)")
            else:
                lines.append("      foreign side: its own instance must retire it")
    claims = q.get("claims_out", [])
    if claims:
        lines.append(f"Claims on other instances ({len(claims)}) — waiting on them:")
        for c in claims:
            lines.append(f"  - {c['foreign']} (root {c['root_id']}) asserted "
                         f"obsolete by {c['by']}")
    contested = q.get("contested_here", [])
    if contested:
        lines.append(f"Contested here ({len(contested)}) — another instance "
                     "says these are obsolete:")
        for c in contested:
            lines.append(f"  - {c['name']}  (claimed by: {c['by']})")
            lines.append(f"      foldcrumbs supersede {c['name']} --by <file>    "
                         "(or `forget`, or keep it — they only claimed)")
    if not lines:
        lines.append("no conflicts — nothing is ambiguous, contested or awaiting "
                     "another instance.")
    return "\n".join(lines)
