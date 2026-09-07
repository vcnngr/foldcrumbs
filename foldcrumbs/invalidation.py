"""INV — invalidation contracts between memories (docs/design/invalidated-by.md rev 2).

A memory can declare "I hold only while B holds" via the `invalidated_by`
relation predicate. When B's BASE lifecycle dies (superseded, deleted,
archived, expired), A leaves the served context — derived on read, never
written back (no cascade, no sweeper).

Resolution is owner-scoped and three-valued (design §D2):

* INVALIDATED — the target resolved in a COMPLETE scan and is base-dead.
  The contract fulfilled: A is excluded from the served context.
* DANGLING    — the target does not resolve in a complete scan. We do NOT
  claim to know why (hard-forget vs typo vs corruption are the same
  observation); A is excluded fail-closed and doctor surfaces the edge.
* UNRESOLVED  — the scan was incomplete (cap/deadline) or the target id
  is ambiguous (duplicate ids, one alive one dead). "Could not verify" —
  never "B was deleted". A is excluded fail-closed.

Contracts are single-hop by BASE-alive (design §D1): A depends on B's
base lifecycle (status active + not expired), NOT on B's own derived
invalidation. If B is merely invalidated (base-alive), A stays served.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PREDICATE = "invalidated_by"

# The three resolution outcomes (design §D2).
INVALIDATED = "invalidated"
DANGLING = "dangling"
UNRESOLVED = "unresolved"
VALID = "valid"          # target base-alive — contract holds


class ReadContext:
    """Ephemeral owner-scoped resolution context for ONE store.

    Holds every on-disk record state (visibility-INDEPENDENT: a dead B
    must stay resolvable), the evaluation instant, and whether the scan
    was complete. Built once per operation and reused across candidates
    — the per-edge check is a dict lookup (design §D2, T12 pins O(N+E)
    with a read spy).
    """

    def __init__(self, records: list[Any], complete: bool = True) -> None:
        self.complete = complete
        self.evaluated_at = datetime.now(timezone.utc)
        self._by_id: dict[str, list[Any]] = {}
        for rec in records:
            self._by_id.setdefault(rec.id, []).append(rec)

    @classmethod
    def for_store(cls, cwd: Any = None) -> "ReadContext":
        """Full local scan — all states, completeness from the scanner."""
        from . import store
        records, complete = store._read_local(cwd)  # noqa: SLF001
        return cls(records, complete=complete)

    def resolve(self, target_id: str) -> tuple[str, str]:
        """(outcome, detail) for one contract target id.

        outcome ∈ {VALID, INVALIDATED, DANGLING, UNRESOLVED}; detail is a
        human-readable reason for diagnostics (never asserts more than
        the observation supports).
        """
        matches = self._by_id.get(target_id)
        if matches is None:
            if not self.complete:
                return (UNRESOLVED,
                        "contract target could not be verified "
                        "(incomplete scan)")
            return (DANGLING, "contract target does not resolve in this store")
        if len(matches) > 1:
            # duplicate ids (one alive, one dead): ambiguous — fail closed,
            # never last-wins (design §D2).
            return (UNRESOLVED,
                    f"contract target id is ambiguous ({len(matches)} "
                    "records share it)")
        rec = matches[0]
        if base_alive(rec):
            return (VALID, "")
        return (INVALIDATED, f"contract target is {rec.status}"
                + (" (expired)" if rec.is_expired and rec.status == "active"
                   else ""))


def base_alive(rec: Any) -> bool:
    """B's BASE lifecycle: active on disk and not past expiry.

    Deliberately independent of B's own derived invalidation — contracts
    do not chain recursively (design §D1, T8).
    """
    return rec.status == "active" and not rec.is_expired


def contract_targets(rec: Any) -> list[str]:
    """Target ids of rec's invalidated_by edges (parse-time, cheap)."""
    from . import relations
    out: list[str] = []
    for rel in relations.parse(rec.relations_json):
        if rel.get("p") != PREDICATE:
            continue
        t = rel.get("t") or {}
        if t.get("k") == "m" and t.get("id"):
            out.append(t["id"])
    return out


def carries_contract(rec: Any) -> bool:
    """True when the record has any invalidated_by edge (any state).

    Design §D4: contract-carrying records get write protection
    (create-only at destination) REGARDLESS of the derived outcome —
    the protection is about identity, not visibility.
    """
    return bool(contract_targets(rec))


def derive(rec: Any, ctx: ReadContext) -> tuple[str, str]:
    """Derived invalidation state of one record against a context.

    Returns (VALID, "") when the record carries no contract or every
    contract target is base-alive; otherwise the WORST outcome across
    its edges with the first matching detail. Worst = most informative
    failure: INVALIDATED (we know why) > DANGLING > UNRESOLVED.
    Single-hop: the target's own derived state is irrelevant (base_alive
    only).
    """
    targets = contract_targets(rec)
    if not targets:
        return (VALID, "")
    worst = VALID
    detail = ""
    order = {VALID: 0, UNRESOLVED: 1, DANGLING: 2, INVALIDATED: 3}
    for tid in targets:
        outcome, why = ctx.resolve(tid)
        if outcome != VALID and order[outcome] > order[worst]:
            worst, detail = outcome, why
    return (worst, detail)


def is_served(rec: Any, ctx: ReadContext) -> bool:
    """Whether rec belongs in the served context (all outcomes but VALID
    are excluded fail-closed)."""
    outcome, _ = derive(rec, ctx)
    return outcome == VALID


def diagnostic_line(rec: Any, outcome: str, detail: str) -> str:
    """One honest diagnostic line for the recall tail (design §D3).

    Says what was observed, never more: DANGLING does not claim deletion,
    UNRESOLVED does not claim death.
    """
    label = {
        INVALIDATED: "invalidated",
        DANGLING: "contract target unresolved",
        UNRESOLVED: "contract unverified",
    }.get(outcome, outcome)
    name = rec.title or rec.filename()
    suffix = f" — {detail}" if detail else ""
    return f"matched but not served: {name} ({label}{suffix}); verify before use"


def doctor_report(cwd: Any = None) -> list[str]:
    """Health view of every invalidation contract in the local store
    (design §D3 doctor row). NOT a context filter — it lists all three
    failure outcomes plus mutual pairs, so a human can repair them.
    """
    from . import store
    records = list(store.iter_memories_including_retired(cwd))
    ctx = ReadContext(records)
    lines: list[str] = []
    contracted = [m for m in records if carries_contract(m)]
    # mutual pairs (design §D1/T7): A inv_by B AND B inv_by A
    edges: dict[str, set[str]] = {}
    for m in contracted:
        edges[m.id] = set(contract_targets(m))
    for m in contracted:
        for tid in contract_targets(m):
            if m.id in edges.get(tid, ()):
                pair = tuple(sorted((m.id, tid)))
                tag = f"mutual pair {pair[0][:8]}<->{pair[1][:8]}"
                if not any(tag in ln for ln in lines):
                    lines.append(f"mutual invalidation pair: "
                                 f"{m.filename()} <-> (target {tid[:8]}) — "
                                 "either base death invalidates both")
    for m in contracted:
        outcome, detail = derive(m, ctx)
        if outcome == VALID:
            continue
        name = m.source_path or m.filename()
        if outcome == INVALIDATED:
            lines.append(f"invalidated: {name} — {detail}")
        elif outcome == DANGLING:
            lines.append(f"dangling contract: {name} — target does not "
                         "resolve in this store (re-point or retire)")
        else:
            lines.append(f"unverified contract: {name} — {detail}")
    return lines
