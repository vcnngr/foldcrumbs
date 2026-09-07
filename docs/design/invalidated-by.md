# Design: `invalidated_by` — invalidation contracts between memories

Rev 2 — 2026-09-07. Status: PROPOSED (RT r2 pending).
Rev 1 was double-RED: GPT F1-F3 (dedup exclusion is DESTRUCTIVE on
upsert-collision; timeline+snapshot consumers don't inherit _visible;
the derivation lacks an owner-scoped resolution context) and Kimi F1-F4
(no-IO claim false outside search; dangling indistinguishable from
corrupt; recall-line mechanism unspecified and self-contradictory;
"edges ride along" FALSE for adopt — _copy_of drops relations_json by
design). All absorbed below; GPT P1s F4-F6 folded in as explicit
semantics rather than left implicit.

Source: arXiv 2609.00243 ("Invalidation Contracts") — a fact can die
not because IT was superseded or expired, but because the thing it
depended on was.

## 1. The problem, concretely

- "The staging URL is https://stg.example.com" holds only while "We use
  the stg cluster" holds — when the cluster decision dies, the URL fact
  is suspect, but nothing marks it today.
- An `authorization`'s `backed_by` already implements exactly this
  contract for grants (backing dies → UNBACKED on every served read).
  `invalidated_by` generalizes the same honesty to EVERY memory type,
  without touching the authorization design.

The paper's framing: invalidation is a contract declared at write time,
not something a reader infers. Our posture stays: derived on read,
never a daemon.

## 2. Design decisions

### D1 — a new relation predicate: `invalidated_by`

Added to `relations.PREDICATES` (8 → 9):

    A --invalidated_by--> B     "A holds only while B is BASE-alive"

- **BASE-alive, precisely** (rev1 F4): B's base lifecycle — `status ==
  "active"` and not past `expires_at` — independent of any DERIVED
  invalidation B itself carries. Contracts do not chain recursively:
  A depends on B's base state, not on B's served truth. If B is itself
  invalidated (base-alive, contract dead), A stays visible. Mutual
  A↔B pairs invalidate together on either one's BASE death; `doctor`
  reports the pair.
- Written with the existing verbs (`relate` CLI/MCP), same evidence
  rules, same per-memory lock, same canonical serialization.
- Target: local memory only (`{"k":"m"}`); entity targets refused; the
  target must be BASE-alive AT WRITE TIME (an edge to a dead memory is
  a mistake, refused visibly). Self-edge refused.
- **Concurrency, stated honestly** (rev1 F4): `relate` validates B
  under A's lock; B's death is not serialized by it. A contract can be
  invalidated microseconds after being written — that is the
  observational contract, same as authz minting. No linearizability
  claim; every complete read re-derives.

### D2 — derivation-on-read with an OWNER-SCOPED RESOLUTION CONTEXT

Invalidation is never written; it is derived. But `is_invalidated` is
NOT a property of one record (rev1 GPT-F3): resolving B needs the
owner store's state, so the derivation runs against an explicit,
ephemeral **read context**:

    ReadContext = { id -> record state, for ALL on-disk records of ONE
                    store (visibility-independent: superseded/expired/
                    archived included — a dead B must stay resolvable),
                    evaluation instant, completeness flag }

- Built once per store per operation from the full local scan the
  caller already performs where one exists (`search` loads `local`;
  `rebuild_index` iterates everything). Where no scan exists (`get`,
  `fetch`), building the context is an explicit cost — DECLARED, not
  hidden: the rev1 "no extra IO" claim is withdrawn. Cost model: one
  O(N+E) snapshot per operation, reused across all candidates; the
  per-edge check is a dict lookup. T12 pins this with a read spy.
- **Resolution outcomes are three, not two** (rev1 GPT-F3, Kimi-F2):
  - B found, base-dead (superseded/deleted/archived/expired) → A is
    INVALIDATED (contract fulfilled).
  - B not found in a COMPLETE scan → A is DANGLING. Dangling does not
    claim to distinguish hard-forget from typo/corruption — it says
    "target does not resolve"; A is excluded from the served context
    (fail-closed) and rendered as suspect, never silently dropped.
    `doctor` surfaces every dangling edge for repair.
  - Scan incomplete (federated partial, cap/deadline hit) or duplicate
    ids for B (one alive, one dead) → UNRESOLVED. Fail-closed: A is
    NOT served as current, and the diagnostic says "could not verify",
    NEVER "B was deleted". Incompleteness is not evidence of death
    (store.iter_memories_in's own documented rule).
- Federation: a foreign record's edges are resolved ONLY in the foreign
  owner's context (the contract is local to the owner store — the
  reader in another root does not resolve B against its own dict).
  Unresolvable cross-root → UNRESOLVED → not served as current.

### D3 — served-read contract: an EXPLICIT consumer table

`_visible(rec)` stays a pure per-record predicate — it CANNOT carry
invalidation (no context parameter). Instead, every served surface
gains the derived check explicitly. Rev1's "zero per-surface work" is
withdrawn; this is the table, verified against the real consumers
(rev1 GPT-F2, Kimi-F1):

| consumer | mechanism | rev1 error fixed |
|---|---|---|
| `store.search` / recall / answer context | context built from the already-loaded `local` list; candidates with edges checked before scoring | partition, see below |
| `find_duplicate` (dedup) | **NOT excluded — PROTECTED** (see D4) | rev1 T10 was destructive |
| `find_conflict_candidates` | invalidated A excluded from candidates (coherent: current-context only) | — |
| `rebuild_index` (MEMORY.md) | excluded via the derived check on the full scan | — |
| hooks SessionStart/PostCompact | the snapshot EXCLUDES contract-carrying memories from the "honour it" block; a pointer line replaces them: "N memories carry invalidation contracts — served live via recall" (same posture as the authz index exclusion). A stale MEMORY.md (built while B lived) therefore never asserts A as current after B dies | rev1 probe: PostCompact re-injected A |
| `index_shard` (federated publication) | contract-carrying records published WITHOUT current-validity claim, or excluded — chosen: excluded from shard blocks, counted in a pointer line (shard is a snapshot; snapshots don't derive) | rev1: shard has no state |
| MCP/CLI `timeline` | rows AND anchor get the derived check against a context built from the timeline's own store scan; invalidated anchor → explicit refusal with reason (mirrors `_anchor_exclusion`) | rev1 probe: timeline served A |
| `fetch` (CLI+MCP) | raw file served (historical document) + one-line envelope when invalidated/dangling: `[invalidated: contract target <file> is <state>]` | — |
| recall honesty line | see below | rev1 D3/T3 contradiction |
| authz ledger section | unchanged feeder (`iter_memories_including_retired`); a grant with BOTH conditions renders BOTH labels: `UNBACKED` (its own state) and `INVALIDATED:<target>` — neither suppressed | — |
| `graph view` / `graph path` | UNTOUCHED — invalidation is a context-serving rule, not a graph rule; invalidated nodes stay traversable while active-on-disk | per rev1 D5, confirmed |
| `doctor` | health view, not a context filter: lists invalidated (with contract + target state), dangling (for repair), UNRESOLVED, mutual pairs; counts distinguish active-on-disk from served-visible | rev1: counters conflated |

**Recall honesty line — mechanism, specified** (rev1 Kimi-F3, GPT-F5):
`search` partitions its scored candidates into `valid` and
`invalidated` in ONE pass over ONE snapshot (same filters, same
relevance scoring — no second disk read). The context block renders
`valid` as today; the diagnostics tail (NOT the LLM answer context)
renders up to 3 lines: `matched but invalidated by <target> (<state>) —
verify before use`, plus `showing 3 of N` when N > 3. `answer`'s LLM
context receives ONLY the valid partition — diagnostics never feed the
model as evidence. Ordering deterministic (created_at, filename).

### D4 — write-path protection: invalidated ≠ deduplicable

Rev1's worst hole (GPT-F1, probe-reproduced): excluding invalidated
from `find_duplicate` meant an upsert of identical content found no
duplicate, hit `write_memory`, and **overwrote A's file by destination
collision** — new id, no contract, immediately served while B was
still dead. The fix separates context eligibility from identity
protection:

- A record that carries an `invalidated_by` edge (whatever its derived
  state) is **create-only at its destination**, like grants and like
  the authz minting rule: `write_memory`/`upsert` refuse to replace an
  existing file whose record carries a contract. Visible refusal:
  "existing memory carries an invalidation contract — retire it
  explicitly (supersede/forget) or fix the contract; no silent
  overwrite".
- Fuzzy dedup does NOT "validate" onto a contract-carrying record
  either (validation would bump trust on a memory whose contract is
  dead — wrong signal): near-duplicate of an invalidated A → refused
  with the same visible message, bytes/id/edges of A unchanged.
- Applies through EVERY upsert caller: CLI remember, MCP remember,
  distill.persist, import_store(apply=True). T10-bis pins each.

### D5 — import/migrate/adopt: what actually rides (rev1 Kimi-F4, GPT-F6)

Verified against code, not assumed:

- `import_store` and `migrate` do NOT strip `relations_json` — edges
  genuinely ride. An imported edge whose target id does not resolve
  locally → DANGLING → the memory arrives suspect and is excluded from
  served context until the user re-points or retires it. Honest,
  visible, repairable. A target id that DOES resolve to a different
  local memory with the same id: resolves to that memory — documented
  choice (ids are uuid4; collision is a store-corruption case doctor
  reports, not an import concern).
- `adopt` EXPLICITLY DROPS `relations_json` (`_copy_of`, by design:
  relations are the source root's graph, not portable facts) and
  re-mints the id. Rev1's "edges ride along" was false. Decision:
  **keep the drop.** An adopted memory arrives WITHOUT contracts —
  conservative and coherent: the adopter sees the content, not the
  source's dependency web; if the dependency matters, the adopter
  writes a local contract against a local target. The adoption ledger
  already records source root+id for provenance.
- "Importing doubt is safe" — narrowed (GPT-F6): import is an explicit
  user action and edges carry no authority (unlike transit/outcome/
  authz, which stay stripped). The real import risk was the F1
  overwrite, now closed by D4; no new attestation system for edges.

### D6 — non-goals

- No sweeper/daemon/auto-archive (derived visibility only; the file is
  untouched; decay/archive stay user verbs).
- No chained/transitive invalidation (D1 base-alive rule).
- No probabilistic/semantic staleness.
- No tombstones for hard-forgotten targets: DANGLING is honest
  ("does not resolve"), it does not claim to know B was deleted.
- No change to authorization's `backed_by`; grants may additionally
  carry edges (D3 renders both states).
- No global store lock; eventual consistency between file writes is
  declared (D1), not fixed.

## 3. Test obligations (acceptance matrix)

T1. Base-death matrix: B superseded / deleted / archived / expired /
    hard-forgotten → A excluded from search+index; one test per mode.
T2. Revival free: B restored by hand (status flip + rebuild) → A
    served again; no repair pass ran.
T3. Recall partition: honest diagnostic lines for invalidated hits
    (max 3 + "showing 3 of N"); answer's LLM context gets ONLY valid;
    deterministic ordering.
T4. Fetch envelope (CLI+MCP parity) on invalidated AND dangling;
    valid memory fetch byte-identical to before (backward compat).
T5. No cascade writes: superseding B writes exactly ONE memory file
    (B); A's bytes unchanged; index/shard/sidecar rebuilds are their
    own documented writes, not counted as memory writes.
T6. Write-time refusals: dead B, foreign B, entity target, self-edge,
    dangling at write — visible, nothing persisted.
T7. Mutual pair: allowed; both invalidate on either BASE death;
    doctor reports the pair.
T8. Single-hop by base-alive: A inv_by B, B inv_by C, C base-dead →
    B invalidated, A STILL SERVED (B is base-alive); documented as
    contract, pinned.
T9. Import: edge rides; unresolvable target → DANGLING → excluded +
    doctor-visible; migrate branches same; adopt DROPS edges (pins
    the _copy_of behavior) and adoption still works.
T10. Consumer-table sweep: one test per row (search, index, hooks
    snapshot exclusion incl. SessionStart with distill DISABLED and
    PostCompact with a pre-death index, shard publication, timeline
    rows+anchor refusal, conflict candidates, dedup protection).
T10-bis. D4 destruction closed: A invalidated; upsert of identical
    title/content (CLI, MCP, distill, import apply=True) → visible
    refusal; A's bytes/id/edges unchanged; B revived → original A
    served; no duplicate copies created by the attempts.
T11. Graph untouched: path traverses an invalidated node; view renders
    the edge; no _visible added to traversal.
T12. Cost: read spy asserts ONE snapshot per operation (O(N+E)),
    per-edge lookups are dict hits; `get`/`fetch` context build is the
    declared extra cost. Duplicate-id target (alive+dead, both orders)
    → UNRESOLVED, not served; incomplete scan → UNRESOLVED, never
    mislabeled hard-forgotten.
T13. Expiry-as-death: B expired invalidates; B with future expiry
    doesn't.
T14. Authz precedence: grant UNBACKED + invalidated → ledger shows
    BOTH labels; grant without edges unchanged (byte-level).
T15. Bench S7: invalidated dependent absent from the served block +
    diagnostic line present; meta-test mutant (contract ignored →
    served) grades SUPERSEDED.

## 4. Docs obligations

README x3 (EN source of truth): predicate table + "contracts do not
chain — write the second edge" + "no sweeper: invalidation is computed
on read, never written" + the consumer table's user-visible parts
(hooks pointer line, fetch envelope, recall diagnostics). CHANGELOG
[Unreleased]. MCP `relate` description gains the predicate. fleet-
learning.md cross-reference: adopt drops edges (unchanged), import
rides them (new).

## 5. Sequencing

One PR: relations predicate + ReadContext + consumer table + D4
protection + doctor + tests + docs. Size estimate REVISED (rev1 ~780
was low — Kimi-F1): relations (~60), context+derivation (~150),
consumer wiring incl. hooks/shard/timeline (~180), dedup protection
(~60), recall partition+diagnostics (~80), fetch envelope (~40),
doctor (~70), tests (~600), docs (~140), bench S7+meta (~80). Still
one atomic PR: a half-landed contract table is a lying surface.
