"""How often each memory has actually been recalled.

A memory that keeps coming back for real questions is worth more than one that
has never been needed since it was written. That signal exists only at recall
time, and nothing was recording it: ``validation_count`` moves only when
something explicitly validates a memory, which is a different and much stronger
claim than "this got retrieved".

**Keyed by record id, never by filename.** Filenames are built from type and
title, so a different memory can take over an existing one's file — and a
count keyed that way would be inherited by whatever landed there next, handing
a brand-new memory a rank it never earned. Clearing the old count on every
takeover was tried and is not enough: it is a write that can fail, and one
lost lock silently transfers the rank. An id cannot be taken over, so the
inheritance stops being a race to win and becomes impossible to express.

**Kept beside the store, not inside it.** The obvious place is a field on the
record, and it is the wrong one. Recall runs on every hook, so writing the
count into the memory file would rewrite up to ``limit`` files per search —
changing their content, which republishes this project's index shard, which
every federated instance then re-reads. A read would cost a write amplified
across the federation. One small sidecar keeps the memories themselves
byte-stable and costs a single file per recall.

**Local only.** Reinforcement is a write, and a foreign store is never ours to
write. The sidecar lives in this instance's memory directory; foreign recalls
contribute nothing to it, which is correct — how often *we* needed a memory is
our observation, not theirs.

**Advisory.** Every failure here is swallowed. A store on a read-only mount, a
malformed sidecar, a lost race: recall still answers, just without the bonus.
Losing a count is a worse ranking, never a worse result.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from . import config

SIDECAR = ".recalls.json"

# Ten recalls is "well used"; beyond that the bonus stops growing, so a single
# heavily-queried memory cannot climb forever above things that match better.
_SATURATION = 10.0

# This runs on the *read* path. The default lock wait is five seconds, which
# is right for a registration but absurd for a count nobody needs: a contended
# sidecar would have held up every recall behind it. Barely wait at all — if
# another recall holds it, its increment is as good as ours.
_LOCK_WAIT = 0.05


def _path(cwd: str | os.PathLike[str] | None = None) -> Path:
    return config.memory_dir(cwd) / SIDECAR


def counts(cwd: str | os.PathLike[str] | None = None) -> dict[str, int]:
    """Recall count per memory id. Empty when unavailable."""
    try:
        data = json.loads(_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


def strength(count: int) -> float:
    """A recall count as a 0..1 weight."""
    return min(max(count, 0) / _SATURATION, 1.0)


# Half weight at two months old. Age is a weak signal — a decision from last
# year can be exactly the answer — so this decays gently and, like
# reinforcement, only separates memories that already matched equally well.
_HALF_LIFE_DAYS = 60.0


def freshness(rec) -> float:
    """How recent a memory is, as a 0..1 weight.

    Age already costs *confidence* for preferences and observations, which is
    about how much to trust a memory. This is a different question — which of
    two equally relevant memories to show first — so it applies to every type
    and never removes anything from the results.

    A memory with no recorded date is treated as neither fresh nor stale.
    Reading a missing date as the epoch would bury every memory written before
    dates were serialized, which is the opposite of not knowing.
    """
    if getattr(rec, "created_at_missing", False):
        return 0.5
    created = getattr(rec, "created_at", None)
    if created is None:
        return 0.5
    from datetime import datetime, timezone

    days = (datetime.now(timezone.utc) - created).days
    if days < 0:
        return 1.0          # a clock skew is not a reason to bury a memory
    return 1.0 / (1.0 + days / _HALF_LIFE_DAYS)


def reinforce(ids: list[str], cwd: str | os.PathLike[str] | None = None,
              known: set[str] | None = None) -> None:
    """Record that these memories were returned by a recall.

    Read-modify-write under the store's lock, because two recalls can land at
    once in an MCP server and a lost update here is a count silently reset.

    ``known`` is the store's current set of active memory ids, and
    anything outside it is dropped. A memory can leave recall through forget,
    supersede, the distillation's contradiction pass, or a prune — and cleaning
    up at each of those is a rule to remember at four call sites and forget at
    the fifth. Reconciling here instead means the sidecar cannot drift,
    whatever removed the memory, including paths that do not exist yet.
    """
    names = [i for i in ids if isinstance(i, str) and i]
    if not names and known is None:
        return          # nothing to add and nothing to reconcile against
    if not config.distill_enabled():
        # A read-only consumer: a machine sharing the store over Syncthing
        # while another one does the writing. Recall and index injection still
        # work there, and writing is exactly what is switched off — a count is
        # no exception, and every recall would otherwise churn a synced file.
        return
    from . import federation

    target = _path(cwd)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        with federation.file_lock(target.parent / ".lock-recalls",
                                  allow_unsupported=True,
                                  wait=_LOCK_WAIT) as locked:
            if not locked:
                return          # someone else is counting; theirs is as good
            data = counts(cwd)
            for name in names:
                data[name] = data.get(name, 0) + 1
            if known is not None:
                # An *empty* set is an answer: the store has no active
                # memories, so no count belongs to anything. Refusing to act
                # on it left the last retired memory's weight behind, growing
                # the sidecar with ids of things long gone. Callers that
                # cannot vouch for their listing pass None instead.
                data = {n: c for n, c in data.items() if n in known}
            _write(target, data)
    except OSError:
        return


def _write(target: Path, data: dict[str, int]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def forget(memory_id: str, cwd: str | os.PathLike[str] | None = None) -> None:
    """Drop a memory's count when the memory itself goes.

    Not required for correctness — ``reinforce`` reconciles against the store
    on every recall, and an id is never reused — but it keeps the sidecar from
    carrying weight for something already retired until the next search.
    """
    from . import federation

    target = _path(cwd)
    if not target.is_file():
        return
    try:
        # The ordinary wait, not the recall path's. This runs when a memory is
        # retired or its file is taken over by a different one, and giving up
        # here leaves the old count on a name the new memory now owns — an
        # sidecar carrying weight for a memory that no longer exists. A write
        # already waits; so does this.
        with federation.file_lock(target.parent / ".lock-recalls",
                                  allow_unsupported=True) as locked:
            if not locked:
                config.log_event(
                    f"foldcrumbs: could not drop the recall count of {memory_id}")
                return
            data = counts(cwd)
            if data.pop(memory_id, None) is not None:
                _write(target, data)
    except OSError:
        return

