# Design: `invalidated_by` — invalidation contracts between memories

Rev 1 — 2026-09-07. Status: PROPOSED (RT pending).
Source: arXiv 2609.00243 ("Invalidation Contracts") — a fact can die not
because IT was superseded or expired, but because the thing it depended
on was. Today foldcrumbs sees only direct death (status, expires_at);
dependent death is invisible, and the dependent memory keeps being
served as current.

## 1. The problem, concretely

Real store shapes this covers:

- "The staging URL is https://stg.example.com" depends on "We use the
  stg cluster" — when the cluster decision is superseded, the URL fact
  is suspect, but nothing marks it.
- "Trial license covers staging until December" depends on "Vendor X is
  our license provider" — vendor decision dies, the trial fact keeps
  being served as truth.
- An `authorization`'s `backed_by` already implements exactly this
  contract for grants (backing dies → UNBACKED on every served read).
  `invalidated_by` generalizes the same honesty to EVERY memory type —
  without touching the authorization design (grants keep their own
  typed field; this is not a replacement).

The paper's framing: invalidation must be a *contract declared at write
time* ("this memory holds only while that one holds"), not something a
reader has to infer. Our answer keeps foldcrumbs' posture: derived on
read, never a background daemon.

## 2. Design decisions

### D1 — a new relation predicate: `invalidated_by`

Added to `relations.PREDICATES` (G1 layer, 8 → 9 predicates). Semantics:

    A --invalidated_by--> B     "A holds only while B is alive"

- Written with the existing verbs: `foldcrumbs relate <A> invalidated_by
  --to-memory <B> --evidence "..."` and MCP `relate`. Same evidence
  rules, same per-memory lock, same dangling-target refusal (B must
  exist and be active AT WRITE TIME — an invalidation contract pointing
  at a dead memory is a mistake, not a contract; refuse visibly).
- Target kind: memory only (`{"k":"m"}`); external-entity targets
  (`{"k":"e"}`) refused for this predicate (an entity has no lifecycle
  to watch).
- Direction discipline: the contract lives on the DEPENDENT (A). B does
  not need to know its dependents at write time; they are found by
  scanning relations, exactly like the federated claims scan.

### D2 — derived, not written (the core decision)

Invalidation is a READ-TIME derivation, never a mutation:

    invalidated(A) = A has an invalidated_by edge whose target B is
                     dead: B.status in (superseded, deleted, archived)
                     OR B.is_expired OR B is hard-forgotten (dangling).

- No cascade writes when B is superseded. Reasons, in order of weight:
  1. A supersede is one atomic file write today; making it also rewrite
     N dependent files reintroduces exactly the race class RT found in
     authorization r1 (F2: concurrent writers resurrecting state) across
     a much larger surface.
  2. Derivation cannot go stale: it is computed from current truth on
     every read, same posture as `authz.derived_state` and `is_expired`.
  3. Revival is free: if B is restored (un-superseded by a human edit,
     un-expired by moving the date), A is valid again on the next read —
     no repair pass. A written cascade would need one.
- Cost bound: the derivation only runs for memories that CARRY the
  predicate (a parse-time flag on the relations line), and resolving
  one target is a single id lookup in the already-loaded store list.
  No extra file IO on the hot path: `search` loads the local store
  anyway; the check is a dict lookup per candidate that has the edge.

### D3 — served-read contract

An invalidated memory is treated exactly like an expired one — the
`_visible` predicate gains the condition, and everything downstream
(recall, index, federation, dedup, contradiction pass, answer context,
timeline) inherits it with zero per-surface work:

    _visible(rec) = rec.status == "active" AND NOT rec.is_expired
                    AND NOT rec.is_invalidated

- The FILE is untouched (same philosophy as expiry: visibility is a
  read decision; decay/archive remain the user's explicit verbs).
- `fetch` still serves it — the raw file is a historical document — but
  MCP/CLI fetch appends a one-line envelope when invalidated, mirroring
  the authorization envelope:
  `[invalidated: depends on <B filename> which is <status>; the
  contract no longer holds]`.
- The memory is NOT silently dropped from recall when it is the ONLY
  hit: the recall block shows a single honest line instead of an empty
  "(no matching memories)" — "1 memory matched but is invalidated by
  <B> (<status>)" — because an agent that asked deserves to know the
  answer exists but is suspect, not that nothing exists. (Same honesty
  move as the authz ledger section; bounded to one line per invalidated
  hit, max 3 lines.)

### D4 — write-path rules

- `relate A invalidated_by B`: B must be a LIVE local active memory
  (dangling/dead/foreign/entity refused at write — the existing
  relations gate plus the alive check).
- Self-edge refused (A invalidated_by A is a paradox).
- A↔B mutual invalidation contracts are ALLOWED but flagged: if both
  directions exist, invalidation of either kills both — that is a
  coherent (if fragile) contract; `doctor` reports the pair so a human
  can decide. No cycle detection beyond depth: the derivation is
  single-hop by construction (see D5).
- **Single hop, deliberately.** invalidation does NOT chain
  (A inv_by B, B inv_by C, C dies → B invalidated, A NOT automatically).
  Chained invalidation is transitive closure over a mutable graph:
  order-dependent, cycle-prone, and impossible to explain in one line.
  If B's death should kill A too, the user writes the second edge. The
  one exception: a DANGLING target (hard-forgotten) invalidates its
  direct dependents — that is still single-hop.
- import/migrate: `invalidated_by` edges are ordinary relations_json —
  they ride along (unlike transit/outcome/authz, an invalidation
  contract carries no authority, only doubt; importing doubt is safe).
  BUT: a foreign target id in an imported edge resolves to nothing
  locally → dangling → invalidates. Honest failure mode: the imported
  memory arrives suspect and the user re-points the edge. Documented.
- adopt: same as import (the ledger records provenance; edges ride; a
  dangling target invalidates visibly).

### D5 — visibility surfaces

- `doctor`: new section — invalidated memories with the contract and
  the target's state; dangling targets; mutual pairs. Counters only,
  no fixes suggested beyond "re-point or retire".
- `graph view`: the predicate renders like any other G1 edge (label
  `invalidated_by`); invalidated nodes keep rendering (the graph shows
  history, `_visible` gates the served CONTEXT, not the graph).
- `graph path`: **semantics untouched.** An invalidated memory stays a
  valid path node while active-on-disk — invalidation is a context
  serving rule, not a graph rule. (Same line as authorization rev2 F3:
  we do not quietly change traversal to fix a presentation problem.)
- MEMORY.md index: invalidated memories drop out (they fail
  `_visible`), consistent with expiry.

### D6 — non-goals

- No background sweeper, no daemon, no auto-archive of invalidated
  memories (the user's verb stays the user's).
- No probabilistic/semantic invalidation ("B changed a lot, so A is
  maybe stale") — contracts are explicit edges or nothing.
- No chained/transitive invalidation (D4).
- No invalidation-by-foreign-memory across federation (local targets
  only; imported edges with unresolvable targets fail to dangling,
  which IS supported).
- No change to authorization's `backed_by` (typed field stays; a grant
  MAY additionally carry invalidated_by edges, nothing special).

## 3. Test obligations (acceptance matrix)

T1. relate A invalidated_by B (live) → edge stored canonical; B dead
    (each of: superseded, deleted, archived, expired, hard-forgotten)
    → A fails _visible; matrix per death mode.
T2. B restored (status flipped back by hand + rebuild) → A visible
    again on next read; no repair pass was needed.
T3. recall with A as only hit → honest "invalidated by" line, not
    empty result; >3 invalidated hits → capped at 3 lines.
T4. fetch envelope on invalidated memory (CLI + MCP parity); fetch of
    a VALID memory unchanged byte-for-byte (backward compat).
T5. no cascade writes: superseding B performs exactly ONE file write
    (B itself) — dependent files untouched (byte-compare before/after).
T6. write-time refusals: dead B, foreign B, entity target, self-edge,
    dangling — each visible, nothing written.
T7. mutual pair allowed; doctor reports it.
T8. single-hop: A inv_by B, B inv_by C, C superseded → B invalidated,
    A still visible (contract, not accident).
T9. import/migrate: edge rides; unresolvable target → dangling →
    invalidated on arrival; visible in doctor.
T10. index/federation/dedup/contradiction/timeline all exclude
    invalidated (inherited via _visible) — one test per surface.
T11. graph path: invalidated node still traversable (semantics
    untouched); graph view renders the edge.
T12. hot-path cost: search on a store with N memories where k carry
    edges does not re-read files (assert via iter count / monkeypatched
    read spy).
T13. expired-B counts as dead for the contract; B with future expiry
    does not invalidate.
T14. authorization unaffected: grants keep backed_by behavior; a grant
    with an additional invalidated_by edge behaves as both contracts
    say (derived_state UNBACKED vs _visible invalidated — precedence
    documented: authz ledger section shows the grant regardless, with
    BOTH states).

## 4. Docs obligations

README x3 (EN source of truth): `invalidated_by` in the relations
predicate table; the single-hop rule stated plainly ("contracts do not
chain — write the second edge"); the read-derivation posture ("no
sweeper: invalidation is computed, never written"). CHANGELOG
[Unreleased]. MCP `relate` description gains the predicate.

## 5. Sequencing

One PR: relations predicate + derivation + _visible + recall line +
fetch envelope + doctor + tests + docs. Size estimate: relations (~60),
store derivation (~80), profile/recall (~40), fetch envelope (~30),
doctor (~50), tests (~450), docs x3 (~120). Bench scenarios: ADD one
(S7: invalidated dependent absent from served block; contract pinned in
the meta-tests' known_ops + validate_suite if the op set grows).
