# Design: RRF fusion for the semantic channel

Status: rev 3 — absorbs RT r2 (GPT t_66617c73 F2 residual + P1 float language; Kimi t_a4805ad2 GREEN)
Scope: `foldcrumbs/store.py` `search()` fusion stage only (+ tests, docs).
No schema change, no new surface, no new dependency, no persisted state.

## 1. Problem

Recall has two independent evidence channels:

- **lexical** — exact/word-overlap/fuzzy-ratio score in `[0, 1]`
  (store.py:1095-1101);
- **semantic** — cosine similarity against an embedding endpoint,
  opt-in (`FOLDCRUMBS_SEMANTIC=1`), all-or-nothing batched call,
  cached machine-locally (store.py:1104-1115).

Today they fuse by **capped max**:

```python
capped = max(0.0, sem_scores[i]) * _SEMANTIC_CAP   # 0.8
score = max(lex, capped)                            # store.py:1117-1127
```

with admission `score >= _RECALL_THRESHOLD` (0.22).

What capped-max cannot express: **agreement as evidence**. A memory
ranked well by *both* independent channels and a memory ranked well by
*one* channel can end up ordered purely by which single number is
bigger; and the cap (0.8) is a hand-picked calibration constant that
every embedding-model change silently rescales. RRF (Cormack, Clarke &
Buettcher, SIGIR 2009) replaces the cross-scale score comparison with
ordinal evidence — which is what two incommensurable channels actually
give us.

> **Honesty note (RT r1 F4/F2):** rank fusion does NOT guarantee that
> agreement always beats single-channel strength — candidates with
> crossed ranks tie exactly ((rank_lex 1, rank_sem 2) and (rank_lex 2,
> rank_sem 1) both give 1/61+1/62 to the last float bit), and ties
> fall to the deterministic tiebreak chain (freschezza/reinforcement).
> What RRF buys is: no magic cap, no scale mixing, agreement as a
> factor, and degeneration to today's behavior when one channel is
> absent. Rev 1 oversold this as a universal inversion property; rev 2
> does not claim it. (Ties are NOT limited to crossed pairs: an
> exhaustive probe over ranks 1..500 finds hundreds of non-crossed
> float ties, e.g. (3,24) and (12,12) both sum to the bit-identical
> 0.027777… — all of them settle on the tiebreak chain.)

## 2. Proposal: rank-based fusion (RRF)

### D1 — Admission

The candidate set and the lexical pass stay exactly as today
(visibility, authorization/contested/type/tag filters, contract
partition, relevance-gated diagnostics — all upstream of fusion, none
touched). A candidate is **admitted** when:

```
lex >= _RECALL_THRESHOLD  OR  sem >= _SEMANTIC_FLOOR
```

`_SEMANTIC_FLOOR = 0.275` — equals today's effective semantic rescue
bar (`0.22 / 0.8`): a paraphrase capped-max would have surfaced still
surfaces; one it would not still does not. Admission stays
score-based (magnitude matters at the gate); *ranking* is ordinal.

Membership equivalence vs today (RT r1 GPT-F1 attack point): under
capped-max a record is served iff `max(lex, sem·0.8) ≥ 0.22` iff
`lex ≥ 0.22 OR sem ≥ 0.275` — the admitted SET is identical by
construction; only the ORDER within it changes (and only when the
semantic channel is on).

### D2 — Ranked lists and their membership (RT r1 Kimi-F2: made explicit)

Every admitted candidate gets a rank in **every available channel**
(sparse ranks, no gaps):

- `rank_lex` — position (1-based) in the admitted set sorted by
  **today's full sort key** `(-round(lex, 2), -tiebreak, is_foreign,
  filename)`. Deliberately the legacy key, including its 2-decimal
  quantization: that quantization is *part of the behavior we promise
  to preserve* (two scores rounding equal are tied, and the tiebreak
  chain orders them — see D4).
- `rank_sem` — exists only when the semantic channel answered
  (`sem_scores is not None`); position in the admitted set sorted by
  `(-sem, filename)` (deterministic total order).

A candidate below a channel's floor is NOT "absent" from that channel —
it still holds a (low) rank in it, computed from its actual score.
There is no second, hidden admission inside the ranking: the floor
does its one job at D1 and never again. (Rev 1 left membership
ambiguous; both readings are now excluded in favor of this one.)

### D3 — Fusion and ordering

```
rrf(m) = 1/(K + rank_lex(m)) [+ 1/(K + rank_sem(m)) when available]
K = 60 (paper default, no knob)
```

Final sort key:

```
(-rrf_full_float, -tiebreak(m), m.is_foreign, filename)
```

**No rounding of rrf** (RT r1 GPT-F1/Kimi-F4: rev 1's `round(rrf, 5)`
collapses adjacent ranks for rank ≳ 250 and could invert distinct
lexical ranks — the constant is deleted). RRF is computed from integer
ranks, so its values are deterministic run-to-run (D5b: deterministic
IEEE-754 approximations, not rational arithmetic); genuine ties
(crossed ranks, D5b) fall to the tiebreak chain, exactly as today's
near-equal scores do.

`tiebreak` (freshness/reinforcement) keeps its exact current role:
separates memories whose fused evidence is equal, never promotes a
worse match. Locality then filename close the chain: unchanged.

### D4 — Semantic-off is the legacy path, not a degenerate case (RT r1 GPT-F1)

When the semantic channel is OFF or failed (`sem_scores is None`), the
fusion stage is **bypassed entirely**: served order =
`(-round(lex, _RANK_PRECISION), -tiebreak, is_foreign, filename)` —
today's code path, byte-identical, no RRF arithmetic involved. I1 is
then true by construction, not by numerical argument; the golden test
asserts ordering equality between old and new code on a fixture with
deliberate near-ties (scores differing only beyond 2 decimals, plus a
reinforcement-differentiated tie group).

### D5 — Reinforcement bookkeeping (RT r1 GPT-F2 + r2 residual, closed)

`_reinforceable(scored, top, limit)` does NOT only consume the served
order: it recognizes the tie group at the cutoff by comparing
`round(score, _RANK_PRECISION)` (store.py:1181-1183). Under rev 2's
RRF values (all ≈ 0.03) that comparison rounds EVERY candidate to the
same 2-decimal bucket — RT r2 PoC: three distinct RRF scores, limit=1,
all three reinforced. Rev 2's "contract unchanged" claim was false at
this line.

Fix (design-level, pinned by T11):

- the tuple carried in `scored` gains its **comparison key**: legacy
  path keeps `round(lex, _RANK_PRECISION)`; RRF path carries the
  full-float rrf value;
- `_reinforceable` compares that key for equality instead of rounding
  it itself — legacy: identical behavior to today (same 2-decimal
  buckets, same tie-group semantics); RRF: only genuine full-float
  ties join the cutoff group;
- semantic-off (D4 bypass) never reaches the RRF key, so I1 covers
  reinforcement too: byte-identical reinforced set on the golden
  fixture.

Under semantic-on the served list may differ from the lexical ranking —
reinforcement following the *served* list is the intended semantics
(count what was actually used), identical in spirit to today where the
served list already mixes both channels via capped max. No second
definition of "score" leaks into the bookkeeping; the comparison
precision is a property of the channel that produced the ordering.

### D5b — Float language correction (RT r2 P1, absorbed)

"Exact float because ranks are integers" was wrong as stated: division
and addition are deterministic IEEE-754 approximations, not rational
arithmetic. Two facts are true and sufficient:

- **deterministic**: same integer ranks ⇒ same float values, run to
  run, on the same platform — no dict-iteration or ordering dependence
  (I5 stands on determinism, not exactness);
- **crossed-rank pairs tie bit-for-bit**: 1/(K+a)+1/(K+b) and
  1/(K+b)+1/(K+a) are the same two float operations in a different
  order — IEEE-754 addition is commutative, so the sums are identical
  bit patterns. This is an *example* of a guaranteed tie, NOT a
  classification of all ties: rational coincidences like (3,174) vs
  (5,150) (both 11/546) may or may not collide in float, and the
  design does not depend on either answer — any residual near-tie is
  settled by the total-order tiebreak chain.

No `Fraction` machinery: full-float ordering plus total tie-breaks is
sufficient for this stage.

### D6 — Invariants (each pinned by a test)

| # | invariant |
|---|---|
| I1 | semantic OFF/failure ⇒ served ordering identical to the legacy path (D4 bypass; golden fixture with 2-decimal near-ties + reinforcement tie group) |
| I2 | an exact lexical match (rank_lex 1) is never outranked by a candidate with strictly lower evidence in BOTH channels; against crossed-rank rivals ((1,2) vs (2,1)) the rrf ties exactly and the tiebreak chain decides. **Strictly weaker than the old numeric cap guarantee** (rev 1 proved falsely strong — Kimi F1: a semantic-only rival at rank_sem 1 TIES the lone exact match, tiebreak decides). Declared weakening; CHANGELOG entry required when implemented |
| I3 | agreement factors into rank: a fixture (explicit rank table, not a universal property) where a both-channel candidate overtakes a single-channel one; companion fixture pinning that crossed-rank pairs tie exactly and the tiebreak decides |
| I4 | admission floor: sem < 0.275 and lex < 0.22 ⇒ not served; the admitted set equals capped-max's admitted set (D1 equivalence) |
| I5 | determinism: same store + same channel outputs ⇒ same order (integer ranks, deterministic full-float sums per D5b, total-order tie-breaks; no dict-iteration dependence) |
| I6 | embedding failure mid-flight ⇒ all-or-nothing fallback to the legacy ordering (existing embed() contract, unchanged) |
| I7 | diagnostics tail (collect_invalidated) unchanged: fusion happens strictly after the contract partition |

### D7 — What does NOT change

- `_SEMANTIC_CAP` is deleted (its scale-mixing job disappears; its
  *guarantee* is re-expressed — weaker, ordinal — in I2 and declared).
- `_RECALL_THRESHOLD` stays; gains the sibling `_SEMANTIC_FLOOR`.
- Embedding batching, caching, opt-in flag, timeout: untouched.
- Foreign records: they participate exactly as today — the semantic
  channel embeds the same candidate list the lexical pass built
  (foreign included), fusion changes ordering only. No federation
  surface is touched (closes the rev-1 silence GPT r1 was asked about).
- `recalls.reinforce(...)` bookkeeping: see D5.
- Snapshots, authorization ledger, invalidation contracts: untouched
  (upstream or orthogonal).

## 3. Test matrix (oracles corrected per RT r1)

| # | test | invariant |
|---|---|---|
| T1 | golden ordering, semantic off: legacy vs new code on fixture with 2-decimal near-ties AND a reinforcement tie group | I1 |
| T2 | exact match (rank_lex 1) vs semantic-only rival (rank_lex ≥ 2): served first — AND the mirrored-rank tie case (exact match + rival rank_lex 2/rank_sem 1 ⇒ rrf tie ⇒ tiebreak decides): both outcomes pinned, no overclaim | I2 |
| T3 | agreement fixture with EXPLICIT rank table: A ranks (lex 2, sem 1), B ranks (lex 1, sem 3) ⇒ rrf A = 1/62+1/61 = 0.032522 > rrf B = 1/61+1/63 = 0.032266 ⇒ A (agreement) beats B (single-channel strength) — verified numerically | I3 |
| T3b | crossed-rank tie fixture: A (lex 1, sem 2) vs B (lex 2, sem 1) ⇒ rrf equal to the last float bit (both 1/61+1/62), tiebreak chain decides, filename closes | I3/I5 |
| T4 | below-floor paraphrase not admitted; above-floor admitted; admitted SET identical to capped-max on a mixed fixture | I4 |
| T5 | double run equality; filename tie determinism | I5 |
| T6 | embedder returns None ⇒ ordering ≡ T1 golden | I6 |
| T7 | partition untouched: existing test_recall_layers + test_invalidation suites green | I7 |
| T8 | single admitted candidate ⇒ served (no rank-math edge case) | — |
| T9 | all candidates equal lexical score, semantic ranks differ ⇒ **declared outcome**: equal rank_lex group is ordered by today's tiebreak inside rank_lex assignment; semantic rank then re-orders the fused sum — pinned by explicit expected list, not by a vague "semantic decides" | D2/D3 |
| T10 | reinforcement breaks rrf ties (two candidates, equal rrf, different recall counts) | D3 |
| T11 | reinforcement cutoff under RRF: three distinct rrf scores, limit=1 ⇒ exactly ONE reinforced (the RT r2 PoC, run against the real helper); legacy path re-pinned: 2-decimal tie group still reinforced together | D5 |

Stubbed embedder: monkeypatch `embeddings.embed` with deterministic
vectors (the suite already does this; no network in tests, none added).

## 4. Anticipated objections (updated post-r1)

- **O1 "RRF discards score magnitude."** Admission keeps magnitude
  where it matters (the floor); ranking uses ordinal evidence only.
  D4 pins that lexical-only users see zero change.
- **O2 "I2 is weaker than the old cap guarantee."** Yes — and rev 2
  says so in the invariant itself, with the tie case spelled out
  (Kimi r1 F1: semantic-only at rank_sem 1 ties the lone exact match;
  the tiebreak decides). The weakening is the price of removing a
  cross-scale comparison that was never semantically meaningful; it is
  a CHANGELOG-declared behavior change under semantic-on.
- **O3 "K=60 is arbitrary."** Published default, robust across TREC
  collections in the source paper; no knob because a knob nobody can
  calibrate is worse than a constant everybody can read.
- **O4 "Tiny candidate sets are noise."** Corrected per GPT r1: small
  lists do not *automatically* fall to the tiebreak — only actual
  (mirrored or same-rank) ties do; adjacent-rank differences are exact
  float values (1/61 ≠ 1/62), unrounded (D3), so they order normally.

## 5. Size / rollout

- store.py: ~40 lines net (fusion block replaced by a `_rrf_fuse()`
  helper + D4 bypass branch; two constants added, one deleted).
- tests: ~280 lines (T1-T10 + golden fixture).
- docs: README x3 semantic-recall paragraph (rank-based fusion;
  admission floors; **exact-match guarantee weakening declared**),
  CHANGELOG (behavior change under `FOLDCRUMBS_SEMANTIC=1`).
- No migration, no state, no config surface change. Semantic-off users
  get a provably identical recall (D4 bypass + I1 golden test).

Estimated total: ~330 lines, 1 module touched + tests + docs.

## 6. Non-goals

- No learned/weighted fusion (weights would need calibration data we
  refuse to collect).
- No third channel (BM25 etc.); RRF extends to n lists unchanged if
  ever needed.
- No persisted ranks/indexes (violates derive-on-read).
