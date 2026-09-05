"""FL-2 — the outcome loop: an explicit verdict on a memory.

`foldcrumbs outcome <memory> good|bad` records what actually happened when
a memory was used. The effects are declared against the REAL machinery
(design fleet-learning.md §F2, RT F5):

* good  → validation_count += 1 (the existing compute_confidence boost)
          plus outcome/outcome_at/outcome_note in the frontmatter;
* bad   → contradiction_detected = True (finally serialized — before FL-2
          the flag was lost on every round-trip) plus the outcome keys.
          A penalty never promotes: compute_confidence caps the
          contradicted value at the non-contradicted one.

The effects live on the effective-weight paths (compute_confidence →
answer/audit/trust_level). `store.search` ranking is relevance + freshness
+ locality and is NOT re-ordered by outcomes — the docs say exactly this,
no overclaim.

Trust boundary: outcome/outcome_at/outcome_note/contradiction_detected are
RESERVED keys. `import_store` and `migrate` strip them like `transit`, so
no foreign validation or dispute can be smuggled in. The note is flattened
to one line at serialization (FL-1 F1 lesson: raw multiline frontmatter
values forge keys).

Sequences: bad then good → outcome is good, contradiction_detected STAYS
true. Revalidation does not erase history; `supersede` does.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import config, store
from .schema import VALID_OUTCOMES


class OutcomeError(Exception):
    """A visible refusal — the loop never fails silently."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def set_outcome(name: str, verdict: str, note: str = "",
                cwd: str | os.PathLike[str] | None = None) -> dict:
    """Record good|bad on one memory. Returns {"ok": ..., "reason"/...}.

    Refusals are values (CLI/MCP report them uniformly): unknown memory,
    verdict outside the closed vocabulary, non-active memory.
    """
    v = (verdict or "").strip().lower()
    if v not in VALID_OUTCOMES:
        return {"ok": False,
                "reason": f"verdict must be one of {', '.join(VALID_OUTCOMES)} "
                          f"(got {verdict!r})"}
    rec = store.get(name, cwd)
    if rec is None:
        return {"ok": False, "reason": f"memory {name!r} not found"}
    if rec.status != "active":
        return {"ok": False,
                "reason": f"memory is not active (status={rec.status}); "
                          f"outcomes record what happened to LIVE knowledge"}
    if rec.is_expired:
        return {"ok": False,
                "reason": "memory is not active (expired); remove or move "
                          "the expiry first if it still holds"}
    if rec.is_foreign:
        return {"ok": False,
                "reason": "that memory belongs to another root — record the "
                          "outcome where you adopted it (or adopt it first)"}

    if v == "good":
        rec.validation_count += 1
        # A later `good` does NOT clear contradiction_detected: history is
        # not erased by revalidation (design §sequences).
    else:  # bad
        rec.contradiction_detected = True
    rec.outcome = v
    rec.outcome_at = _now()
    rec.outcome_note = note.strip() or None
    rec.updated_at = _now()
    store.write_memory(rec, cwd)
    return {"ok": True, "outcome": v, "filename": rec.filename(),
            "validation_count": rec.validation_count,
            "contradiction_detected": rec.contradiction_detected}


def list_outcomes(cwd: str | os.PathLike[str] | None = None) -> list[dict]:
    """Every memory with a recorded outcome, newest verdict first.

    Adoptions are annotated from the local ledger when present (the join is
    on the local memory id — RT F1: a forged `source: adopted:` frontmatter
    never produces a ledger entry, so it never shows up as an adoption
    here). A corrupt ledger does not break this listing: the verdicts are
    the primary data, the adoption tag is decoration.
    """
    ledger: dict = {}
    try:
        from . import adopt as adopt_mod
        ledger = adopt_mod.read_ledger(cwd)
    except Exception:
        ledger = {}
    rows: list[dict] = []
    for m in store.iter_memories(config.memory_dir(cwd)):
        if m.outcome not in VALID_OUTCOMES:
            continue
        row = {"id": m.id, "title": m.title, "filename": m.filename(),
               "outcome": m.outcome,
               "outcome_at": (m.outcome_at.isoformat()
                              if m.outcome_at else ""),
               "note": m.outcome_note or "",
               "contradiction_detected": bool(m.contradiction_detected)}
        entry = ledger.get(m.id)
        if entry:
            row["adopted_from"] = f"{entry.get('root_id', '')[:8]}:" \
                                  f"{entry.get('memory_id', '')[:8]}"
        rows.append(row)
    rows.sort(key=lambda r: (r["outcome_at"], r["filename"]), reverse=True)
    return rows
