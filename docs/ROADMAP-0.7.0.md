# Roadmap 0.7.0 — memory engineering

0.6.x made the federated view **correct**. 0.7.0 makes the memory itself
**selective**: what surfaces, what fades, and who it belongs to.

The frame comes from the five-stage pipeline (capture, consolidate, retrieve,
reconcile, decay). foldcrumbs already ships four of them. This release closes
the gap on the two that are partial and adds the two things federation made
possible but never exposed: profiles and a way to *look* at the store.

## Where we actually stand

| stage | today | 0.7.0 |
|---|---|---|
| Capture | `distill.py`, LLM-judged | unchanged |
| Consolidate | `_DEDUP_THRESHOLD`, `SequenceMatcher` | unchanged |
| Retrieve | substring + overlap + fuzzy | **+ freshness, + reinforcement** |
| Reconcile | supersede, contradiction, cross-instance claims | unchanged |
| Decay | `compute_confidence()` at read | **+ a pass that acts** |

## Stages

Each stage ships only when the review gate is clean. No stage starts before
the previous one is green.

### A — Recall reinforces

A memory that keeps being recalled is a memory that matters. `validation_count`
exists but only moves when something explicitly calls `validate()`, so use
never feeds back into ranking.

- count a recall as weak reinforcement, separately from explicit validation —
  they are not the same evidence and must not share a field
- reinforcement must not let a stale memory outrank a relevant one: it is a
  tiebreaker among matches, not a substitute for matching
- **foreign records are read-only**: reinforcement is a write, so it applies to
  the local store only, and the federated path must not attempt it

### B — Freshness in the ranking

Two memories that match a query equally are not equally useful if one was
written today and one a year ago. Age already penalises *confidence* for
`preference`/`observation`; ranking ignores it entirely.

- a decreasing weight on age, small enough that relevance still dominates
- `created_at_missing` records must not be treated as infinitely old

### C — Decay that acts

Confidence decays on read but nothing ever leaves. `audit.py` already knows
what stale looks like (`STALE_CONF`); it just reports it.

- a maintenance pass that archives below the threshold — **archived, not
  deleted**, and recoverable
- explicit and scheduled, never a side effect of recall
- an archived memory leaves the index and the shards, so other instances stop
  being shown it

### D — Profiles *(done)*

`foldcrumbs profile add|list|env|remove`. A profile is a registered root with
a name and a shape: **dedicated** (one memory directory, every project — what
a long-running agent wants) or **shared** (per project under a config dir —
how an interactive assistant works). Both shapes already existed; this gave
them a vocabulary.

There is no `profile use`. Which store a process reads is decided by its
environment before it starts, and a CLI cannot reach back into the shell that
launched it — so `profile env` prints the one line that does work, and a test
proves that line selects that store.

### E — Dashboard

A store you cannot see is a store you cannot trust. Everything below is
already computable from the store and the shards.

- store health: counts by type, confidence distribution, what is decaying
- federation: which roots, reachable or not, who wrote what, what is contested
- generated as one self-contained HTML file — no server, no external assets

### Deferred — semantic retrieval

Embeddings would improve recall the most and cost the most: foldcrumbs is
pure-stdlib today, which is why it installs anywhere without a dependency
chain. If it lands, it lands as an *optional* path — fuzzy by default,
semantic when the library is present — and not in 0.7.0 unless A–E finish
early.

## Not in scope

- Cross-machine federation. Foreign stores are read from the local filesystem;
  hosts do not see each other. Syncthing already carries the store between
  machines, so this is a question about trust in that sync, not about code, and
  it deserves its own release.
