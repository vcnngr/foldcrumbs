"""G1 — explicit relations between memories, stored in the memory itself.

Design REV-2 (docs/design/graph-layer.md) governs everything here:

* relations live in ONE frontmatter line, ``relations_json`` — canonical JSON
  (sorted keys, no spaces) so diffs stay deterministic (bloccante GPT-F6:
  nested YAML would be dropped by the flat frontmatter parser and silently
  deleted on the first rewrite);
* strong edges point at the immutable ``Memory.id``, never at a filename —
  a retitle renames the file and would dangle the reference (Kimi-F2b);
* writes are read-modify-write under a per-memory lock: two agents adding
  relations to the same memory must both survive or fail VISIBLY, never lose
  one edge silently (bloccante GPT-F5);
* every predicate outside the closed vocabulary of eight is rejected
  explicitly — a forced fit would poison causal queries (Kimi-F1).

No LLM is involved anywhere in this module. Reading is deterministic.
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import config, federation, store
from .schema import MemoryRecord

# The closed vocabulary (design REV-2, §Vocabolario). Eight predicates —
# narrow and unambiguous beats wide and vague. Each one's semantics:
#
#   caused_by   A happened/exists because of B          (causal, backward)
#   depends_on  A cannot work without B                  (structural)
#   supersedes  A replaces B (B was true, A is now)      (replacement)
#   contradicts A and B cannot both hold                 (conflict marker)
#   supports    A is evidence for B                      (justification)
#   refines     A sharpens/narrows B                     (elaboration)
#   blocks      A prevents B until resolved              (obstacle)
#   precedes    A happened before B — temporal order only, NOT causation;
#               forcing caused_by on a mere sequence would be semantically
#               false and would poison every causal query that walks it.
#
# Declared near-inverses (approximate, documented): supersedes ~ the existing
# superseded_by field; blocks ~ depends_on seen from the other side.
PREDICATES = frozenset({
    "caused_by", "depends_on", "supersedes", "contradicts",
    "supports", "refines", "blocks", "precedes",
})

_LOCK_WAIT_SECONDS = 10.0
DEFAULT_DEPTH = 3      # design: default 3, hard max 4
MAX_DEPTH = 4
DEFAULT_MAX_NODES = 500


class InvalidRelation(ValueError):
    """A relation that violates the design: unknown predicate, malformed or
    dangling target. Always explicit — nothing here is accepted silently."""


class RelationLockBusy(RuntimeError):
    """The memory lock stayed held past the wait. Fail closed and visibly:
    the caller gets an error, never a lost edge."""


# --- canonical form --------------------------------------------------------

def canonical(rels: list[dict]) -> str:
    """One-line canonical JSON: sorted keys, no whitespace. Same logical
    relations → same string, whatever order the fields arrived in."""
    return json.dumps(list(rels), sort_keys=True, separators=(",", ":"))


def parse(relations_json: str | None) -> list[dict]:
    """Tolerant read: empty or malformed → []. One corrupt line must not
    blind the rest of the store — same posture as iter_memories_in."""
    if not relations_json or not relations_json.strip():
        return []
    try:
        data = json.loads(relations_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data
            if isinstance(r, dict) and r.get("p") and r.get("t")]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_target_shape(target: dict) -> dict:
    """Shape-validate and normalize a target WITHOUT store access. Raises
    InvalidRelation with the reason — rejection is always explicit (design
    §Target tipizzati). The existence of a memory target is checked later,
    under the lock: checking before would leave a check-then-write window.
    """
    t = target or {}
    kind = t.get("k")
    if kind == "m":
        mem_id = str(t.get("id") or "").strip()
        if not mem_id:
            raise InvalidRelation("memory target requires a non-empty id")
        return {"k": "m", "id": mem_id}
    if kind == "x":
        ns = str(t.get("ns") or "").strip() or "general"
        # Mandatory normalization: lowercase, trim, collapse whitespace —
        # "Moonshot AI" / "  moonshot   ai " are one entity.
        label = " ".join(str(t.get("l") or "").split()).lower()
        if not label:
            raise InvalidRelation("external target requires a non-empty label")
        return {"k": "x", "ns": ns, "l": label}
    raise InvalidRelation("target kind must be 'm' (memory) or 'x' (external)")


def _known_ids() -> set[str]:
    return {m.id for m in store.load_all()}


def _dedup_key(predicate: str, norm_target: dict) -> str:
    return canonical([{"p": predicate, "t": norm_target}])


# --- writing ---------------------------------------------------------------

def add_relation(mem_id: str, predicate: str, target: dict,
                 evidence: str = "", confidence: float = 0.8,
                 prov: str | None = None, proposal_id: str | None = None,
                 cwd: str | Path | None = None) -> bool:
    """Attach one relation to memory ``mem_id``. Returns True when the edge
    was written, False when it already existed (same predicate + target).

    Raises InvalidRelation for any violation; RelationLockBusy when the
    per-memory lock cannot be acquired — both are VISIBLE refusals, per the
    fail-closed rule.

    Provenance (design g2-extraction.md D1/E5): callers that represent a
    write path pass ``prov`` explicitly — ``manual`` (human CLI), ``agent``
    (MCP), ``inferred`` (distill/promotion of a proposal). Non-human
    provenance caps confidence at 0.5. A write without ``prov`` keeps the
    legacy evidence-rule shape (prov absent = later migrated as ``legacy``,
    never silently manual). ``proposal_id`` tags arcs materialized by the
    proposal queue (E4-bis crash-recovery marker).

    Evidence rule (design §Trattamento dell'evidence): a direct write without
    evidence is accepted but records its own uncertainty — confidence capped
    at 0.5 and provenance marked ``inferred``. The uncertainty is stored,
    never hidden.
    """
    if predicate not in PREDICATES:
        raise InvalidRelation(
            f"unknown predicate {predicate!r}; valid: "
            + ", ".join(sorted(PREDICATES)))
    if prov is not None and prov not in ("manual", "agent", "inferred"):
        raise InvalidRelation(f"unknown provenance {prov!r}")
    norm_t = _norm_target_shape(target)

    evidence = (evidence or "").strip()
    rel: dict = {"p": predicate, "t": norm_t,
                 "c": round(float(confidence), 2), "d": _now_iso()}
    if prov is not None:
        rel["prov"] = prov
        if prov != "manual":
            rel["c"] = min(rel["c"], 0.5)
    if not evidence:
        rel["c"] = min(rel["c"], 0.5)
        if prov is None:
            rel["prov"] = "inferred"
    else:
        rel["e"] = evidence
    if proposal_id:
        rel["proposal_id"] = proposal_id

    lock_dir = Path(config.STATE_DIR) / "locks" / f"memory-{mem_id}"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    with federation.file_lock(lock_dir, wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise RelationLockBusy(
                f"memory {mem_id} is locked by another writer; "
                "the relation was NOT added (refusing to race)")
        # Existence checks belong UNDER the lock: a memory target that does
        # not exist right now is rejected outright (no dangling references on
        # the way in), and checking outside would reopen a check-then-write
        # window.
        if norm_t["k"] == "m" and norm_t["id"] not in _known_ids():
            raise InvalidRelation(f"no memory with id {norm_t['id']!r}")
        rec = _find_by_id(mem_id, cwd)
        rels = parse(rec.relations_json)
        key = _dedup_key(predicate, norm_t)
        if any(_dedup_key(r["p"], r["t"]) == key for r in rels):
            return False
        rels.append(rel)
        rec.relations_json = canonical(rels)
        store.write_memory(rec, cwd)
    return True


def _find_by_id(mem_id: str,
                cwd: str | Path | None = None) -> MemoryRecord:
    for rec in store.load_all(cwd):
        if rec.id == mem_id:
            if rec.is_foreign:
                raise InvalidRelation(
                    "cannot attach relations to a foreign memory")
            return rec
    raise InvalidRelation(f"no memory with id {mem_id!r}")


# --- path queries (tri-state BFS) ------------------------------------------

def find_path(src_id: str, dst_id: str, depth: int = DEFAULT_DEPTH,
              max_nodes: int = DEFAULT_MAX_NODES,
              include_inferred: bool = False,
              cwd: str | Path | None = None) -> dict:
    """Walk strong edges (target kind 'm') from src to dst. TRI-STATE result,
    never ambiguous (design §BFS):

      FOUND                 — "path": one step per node, edge info included
      NOT_FOUND_EXHAUSTIVE  — search completed; the connection does not exist;
                              "reached" says how many nodes were visited
      TRUNCATED:<reason>    — budget ran out; NOT proof of absence, and the
                              output says so

    Provenance containment (g2-extraction.md D1, approved amendment):
    by default only ``prov=manual`` arcs are walked — full trust is reserved
    for what a human attested. ``include_inferred=True`` is an explicit
    per-query choice to also walk agent/inferred/legacy arcs and the pending
    proposal overlay. There is deliberately NO environment variable for it:
    opting in must stay a conscious act every time (R3 decision 3).

    Node universe (D3/E3): every ACTIVE, non-expired memory is a node no
    matter which arcs are walked — filters restrict EDGES, never NODES, so
    the tri-state stays honest. An id absent from the store raises
    InvalidRelation (MISSING); an id present but superseded/deleted/
    provisional/expired yields NOT_FOUND_EXHAUSTIVE with an explanatory note
    — it exists, it is just not traversable.

    Neighbors are visited in id order → same store, same path, every time.
    Weak edges (external entities, tags) are never walked here.
    """
    depth = max(1, min(int(depth), MAX_DEPTH))
    max_nodes = max(1, int(max_nodes))
    mems = store.load_all(cwd)
    by_id = {m.id: m for m in mems}
    if src_id not in by_id:
        raise InvalidRelation(f"no memory with id {src_id!r}")
    if dst_id not in by_id:
        raise InvalidRelation(f"no memory with id {dst_id!r}")

    # E3: endpoints that exist but are not traversable get an explicit
    # NOT_FOUND_EXHAUSTIVE — never MISSING (they exist) and never an
    # InvalidRelation (that would break the tri-state for callers).
    for node_id in (src_id, dst_id):
        m = by_id[node_id]
        if m.status != "active" or m.is_expired:
            why = (f"status={m.status}" if m.status != "active"
                   else "expired")
            return {"status": "NOT_FOUND_EXHAUSTIVE", "reached": 0,
                    "note": f"node {node_id} exists but is not traversable "
                            f"({why}); not MISSING — present, excluded from "
                            "paths by design"}

    universe = {m.id for m in mems
                if m.status == "active" and not m.is_expired}

    def _walkable(rel: dict) -> bool:
        prov = rel.get("prov")
        if prov == "manual":
            return True
        # include_inferred walks everything: agent, inferred, legacy
        # (prov absent — E5), and overlay pending proposals. Default walks
        # manual ONLY: legacy/inferred must be opted into, per query.
        return include_inferred

    # Adjacency: strong, existing, traversable targets only, walked BOTH
    # ways — a path query answers "how are these connected", not "was the
    # edge stored in this direction". Direction is never lost: every step
    # carries whether it follows the edge as stored or against it, and the
    # CLI says so. A dangling target is skipped here and surfaced by
    # `graph doctor`, never followed in silence.
    adj: dict[str, list[tuple[str, dict, bool]]] = {}
    for m in mems:
        for r in parse(m.relations_json):
            t = r.get("t")
            if isinstance(t, dict) and t.get("k") == "m" \
                    and t.get("id") in universe and _walkable(r):
                adj.setdefault(m.id, []).append((t["id"], r, True))
                adj.setdefault(t["id"], []).append((m.id, r, False))
    # E4 overlay: pending proposals, read-only, only behind the explicit
    # flag. The store is never touched.
    if include_inferred:
        from . import proposals as proposals_mod
        for e in proposals_mod.overlay_edges(cwd):
            adj.setdefault(e["_subject"], []).append((e["t"]["id"], e, True))
            adj.setdefault(e["t"]["id"], []).append((e["_subject"], e, False))
    for node in adj:
        adj[node].sort(key=lambda e: (e[0], not e[2]))

    parent: dict[str, tuple[str, dict, bool]] = {}
    visited = {src_id}
    frontier = deque([src_id])
    level = 0
    truncated = None
    while frontier and truncated is None:
        if level >= depth:
            truncated = "depth"
            break
        next_frontier: list[str] = []
        for node in frontier:
            for tgt, rel, forward in adj.get(node, ()):
                if tgt in visited:
                    continue
                visited.add(tgt)
                parent[tgt] = (node, rel, forward)
                if tgt == dst_id:
                    return _assemble(src_id, dst_id, parent, by_id)
                next_frontier.append(tgt)
                if len(visited) >= max_nodes:
                    truncated = "max-nodes"
                    break
            if truncated:
                break
        frontier = deque(sorted(next_frontier))
        level += 1

    if truncated:
        return {"status": f"TRUNCATED:{truncated}",
                "reached": len(visited),
                "note": "budget exhausted — this is NOT proof the path "
                        "does not exist; raise --depth/--max-nodes"}
    return {"status": "NOT_FOUND_EXHAUSTIVE",
            "reached": len(visited),
            "note": "search completed; no connection between these memories"}


def _assemble(src_id: str, dst_id: str, parent: dict,
              by_id: dict[str, MemoryRecord]) -> dict:
    # Backward walk dst -> src. Each node carries the edge that leads INTO it
    # from its predecessor; the source carries None. Load parent[cur] BEFORE
    # appending so the arriving edge is attached to the right step (the
    # previous off-by-one rendered edges on the wrong node — caught by the
    # FOUND fixtures, where a 2-node path showed no edge at all).
    steps: list[dict] = []
    cur = dst_id
    while cur != src_id:
        pred, edge, forward = parent[cur]
        m = by_id[cur]
        steps.append({"id": cur, "title": m.title, "file": m.filename(),
                      "edge": edge, "forward": forward})
        cur = pred
    m = by_id[src_id]
    steps.append({"id": src_id, "title": m.title, "file": m.filename(),
                  "edge": None, "forward": True})
    steps.reverse()
    return {"status": "FOUND", "path": steps}


# --- maintenance views -----------------------------------------------------

def legacy_arcs(cwd: str | Path | None = None) -> list[dict]:
    """E5: arcs written before the provenance taxonomy existed — no ``prov``
    field. They are NOT mapped to manual automatically (that would attest an
    author we never observed); doctor counts them and a human attests each
    one explicitly via ``promote_legacy_arc``. Sorted for determinism."""
    out: list[dict] = []
    for m in store.load_all(cwd):
        for r in parse(m.relations_json):
            if "prov" in r:
                continue
            t = r.get("t")
            out.append({
                "memory_id": m.id, "memory_title": m.title,
                "predicate": r.get("p"),
                "target_id": t.get("id") if isinstance(t, dict)
                and t.get("k") == "m" else None,
                "target": t,
            })
    out.sort(key=lambda a: (a["memory_id"], str(a["predicate"]),
                            str(a["target_id"])))
    return out


def promote_legacy_arc(mem_id: str, predicate: str, target: dict,
                       cwd: str | Path | None = None) -> bool:
    """E5: a human attests one legacy arc as their own. Sets prov=manual in
    place (read-modify-write under the memory lock, like every relation
    write). Returns True when attested, False when nothing matched."""
    lock_dir = Path(config.STATE_DIR) / "locks" / f"memory-{mem_id}"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    norm_t = _norm_target_shape(target)
    key = _dedup_key(predicate, norm_t)
    with federation.file_lock(lock_dir, wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise RelationLockBusy(
                f"memory {mem_id} is locked by another writer")
        rec = _find_by_id(mem_id, cwd)
        rels = parse(rec.relations_json)
        changed = False
        for r in rels:
            if "prov" in r or r.get("p") != predicate:
                continue
            try:
                rkey = _dedup_key(r.get("p", ""), _norm_target_shape(r.get("t", {})))
            except InvalidRelation:
                continue          # malformed target: doctor territory, not ours
            if rkey == key:
                r["prov"] = "manual"
                changed = True
        if changed:
            rec.relations_json = canonical(rels)
            store.write_memory(rec, cwd)
        return changed


def doctor(cwd: str | Path | None = None) -> dict:
    """Surface graph rot instead of hiding it: dangling memory targets
    (memory removed after the edge was written) and unknown predicates.
    Read-only."""
    ids = _known_ids()
    dangling: list[dict] = []
    bad_predicates: list[dict] = []
    for m in store.load_all(cwd):
        for r in parse(m.relations_json):
            t = r.get("t")
            if r.get("p") not in PREDICATES:
                bad_predicates.append({"memory": m.title, "id": m.id,
                                       "predicate": r.get("p")})
            if isinstance(t, dict) and t.get("k") == "m" \
                    and t.get("id") not in ids:
                dangling.append({"memory": m.title, "id": m.id,
                                 "missing_target": t.get("id"),
                                 "predicate": r.get("p")})
    return {"dangling": dangling, "bad_predicates": bad_predicates}


def external_entities(cwd: str | Path | None = None) -> list[dict]:
    """Every external entity ('x' target), with where it appears. Sorted for
    determinism."""
    seen: dict[tuple, dict] = {}
    for m in store.load_all(cwd):
        for r in parse(m.relations_json):
            t = r.get("t")
            if not (isinstance(t, dict) and t.get("k") == "x"):
                continue
            key = (t.get("ns", "general"), t.get("l", ""))
            entry = seen.setdefault(
                key, {"ns": key[0], "label": key[1], "count": 0,
                      "memories": []})
            entry["count"] += 1
            if m.title not in entry["memories"]:
                entry["memories"].append(m.title)
    out = sorted(seen.values(), key=lambda e: (e["ns"], e["label"]))
    for e in out:
        e["memories"].sort()
    return out


def similar_entities(cwd: str | Path | None = None) -> list[tuple[str, str]]:
    """SUGGESTED duplicate entities — containment on normalized labels.
    Suggestion only (design principle 4: no automatic merging, a human
    decides)."""
    ents = external_entities(cwd)
    labels = sorted({e["label"] for e in ents})
    pairs: list[tuple[str, str]] = []
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            if a in b or b in a:
                pairs.append((a, b))
    return pairs
