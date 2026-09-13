# Design: RRF fusion for the semantic channel

Status: rev 1 — for red-team
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

What capped-max cannot express: **agreement**. A memory that is
*good-but-not-great in both channels* (lex 0.55, sem 0.85 → 0.68) loses
to a memory that is *strong in one only* (lex 0.75, sem 0.10 → 0.75),
even though two independent channels concurring is better evidence than
one channel alone. The cap (0.8) was the fix for the inverse pathology
(vectors outranking exact word matches); it is a calibration constant
picked by hand, and every embedding model rescales it silently.

## 2. Proposal: rank-based fusion (RRF)

Reciprocal Rank Fusion (Cormack, Clarke & Buettcher, SIGIR 2009) fuses
ranked lists without comparing scores across scales — exactly our
constraint (lexical ratios and cosine similarities are different
animals; the cap is the scar tissue of mixing them).

### D1 — Admission (unchanged in spirit, explicit in code)

The candidate set and the lexical pass stay exactly as today
(visibility, authorization/contested/type/tag filters, contract
partition, relevance-gated diagnostics — all upstream of fusion, none
touched). A candidate is **admitted** to fusion when:

```
lex >= _RECALL_THRESHOLD  OR  sem >= _SEMANTIC_FLOOR
```

`_SEMANTIC_FLOOR = 0.275` — chosen so the semantic-only admission bar
equals today's effective rescue bar (`0.22 / 0.8 = 0.275`): a paraphrase
that capped-max would have surfaced still surfaces, one that it would
not still does not. Admission is evidence-based; *ranking* is RRF.

### D2 — Fusion

Each admitted candidate gets a rank in each channel (1-based; the
semantic rank exists only when the semantic channel answered). Ties
within a channel before ranking are broken by the existing
deterministic key (filename), so ranks are total and stable:

```
rrf(m) = 1/(K + rank_lex(m)) + 1/(K + rank_sem(m))     K = 60
```

Standard K=60 (the paper's value, robust across domains; no tuning
knob exposed). A candidate absent from a channel's ranked list simply
has no term for it.

### D3 — Ordering

Final sort key:

```
(-round(rrf, _RANK_PRECISION_RRF), -tiebreak(m), m.is_foreign, filename)
```

- `_RANK_PRECISION_RRF = 5` — RRF values live in ~[0.003, 0.033]; two
  decimals would collapse everything into ties. Five decimals separate
  adjacent ranks while still treating float noise as equal.
- `tiebreak` (freshness/reinforcement) keeps its exact current role:
  separates memories that matched *equally well*, never promotes a
  worse match (store.py:1130-1141 comment stays true, mutatis
  mutandis).
- Locality then filename: unchanged.

### D4 — Single-channel degeneration (the compatibility contract)

When the semantic channel is OFF or failed (`sem_scores is None`), RRF
must produce **exactly today's lexical ordering**. It does by
construction: with one channel, `rrf = 1/(K + rank_lex)` is a strictly
decreasing function of `rank_lex`, and `rank_lex` is derived from the
lexical score with deterministic tie-breaks — so the order is identical
to sorting by `(-round(lex, 2), -tiebreak, …)`, which is today's. The
one subtlety: today ties at two decimals are broken by tiebreak *within*
the score sort; under RRF the lexical rank already consumed those ties
by filename. To preserve the reinforcement/freshness behavior exactly,
`rank_lex` is computed by sorting on `(-round(lex, _RANK_PRECISION),
-tiebreak(m), is_foreign, filename)` — i.e. **today's full sort key
defines the lexical rank**. Then single-channel RRF ordering ≡ today's
ordering, provably, and a golden test pins it on a fixture store with
the semantic channel disabled.

### D5 — Invariants (each pinned by a test)

| # | invariant |
|---|---|
| I1 | semantic OFF ⇒ byte-identical ordering vs pre-RRF (golden fixture) |
| I2 | exact lexical match (lex = 1.0) outranks any semantic-only admission (the old cap guarantee, re-expressed: rank_lex 1 ⇒ rrf ≥ 1/61 > 1/62 ≥ any single-channel semantic term… **see §4 objection O2** — the guarantee holds against *single-channel* rivals, not against a rival that is also lexically ranked) |
| I3 | agreement beats single-channel strength: (lex 0.55, sem 0.85) outranks (lex 0.75, sem 0.10) when both admitted — *this is the whole point; if a red-team probe shows a realistic corpus where this inversion hurts, the design is wrong* |
| I4 | admission floor: sem < 0.275 and lex < 0.22 ⇒ not served (garbage queries stay empty) |
| I5 | determinism: same store + same channel outputs ⇒ same order, run to run (no dict-iteration dependence) |
| I6 | embedding failure mid-flight ⇒ all-or-nothing fallback to lexical-only ordering (existing embed() contract, unchanged) |
| I7 | diagnostics tail (collect_invalidated) unchanged: fusion happens strictly after the contract partition |

### D6 — What does NOT change

- The cap constant disappears (`_SEMANTIC_CAP` deleted) — it exists only
  to mix scales; RRF never mixes them. The *guarantee* it encoded moves
  to I2 + the admission floor.
- `_RECALL_THRESHOLD` stays (lexical admission), gains the sibling
  `_SEMANTIC_FLOOR`.
- Embedding batching, caching, opt-in flag, timeout: untouched.
- `recalls.reinforce(...)` bookkeeping: untouched (it consumes the
  final ordering, whatever produced it).
- Foreign records, federation, snapshots, authorization ledger,
  invalidation contracts: untouched (all upstream or orthogonal).

## 3. Test matrix

| # | test | invariant |
|---|---|---|
| T1 | golden ordering, semantic off, fixture store with near-ties | I1 |
| T2 | exact lexical beats semantic-only admission | I2 |
| T3 | agreement inversion (the §1 example, stubbed embedder) | I3 |
| T4 | below-floor paraphrase not admitted; above-floor admitted | I4 |
| T5 | double run equality + filename tie determinism | I5 |
| T6 | embedder returns None ⇒ ordering ≡ T1 golden | I6 |
| T7 | invalidated/foreign/grant partition untouched by fusion (existing test_recall_layers + test_invalidation suites green) | I7 |
| T8 | single admitted candidate ⇒ served (no rank math edge case) | — |
| T9 | all candidates same lexical score, semantic ranks differ ⇒ semantic order decides | — |
| T10 | reinforcement still breaks RRF ties (two candidates, equal rrf, different recall counts) | D3 |

Stubbed embedder: monkeypatch `embeddings.embed` with deterministic
vectors (the suite already does this in test_semantic*; no network in
tests, none added).

## 4. Anticipated objections (red-team bait, answered up front)

- **O1 "RRF discards score magnitude — a 0.99 lexical match and a 0.23
  one both become rank 1 and rank N."** True, and intended: admission
  keeps magnitude where it matters (the floor), ranking uses only
  ordinal evidence. The golden test I1 pins that lexical-only behavior
  is unchanged, which is where magnitude was doing real work.
- **O2 "I2 is weaker than the old cap guarantee."** The old guarantee
  was *numeric* (sem·0.8 < 1.0 always). The new one is *ordinal*:
  an exact match is rank_lex 1, so it beats anything not also ranked
  high lexically; a rival with lex 0.95 AND sem 0.95 *should* beat a
  lone exact match on a different query term — that is agreement
  working, not a regression. Stated honestly: this is a behavior
  change under semantic-on, and it is the point of the feature.
- **O3 "K=60 is arbitrary."** It is the published default, robust in
  the source paper across TREC collections; we expose no knob because
  a knob nobody can calibrate is worse than a constant everybody can
  read.
- **O4 "Two-channel RRF with tiny candidate sets is noise."** With n≤3
  candidates the ranks are nearly flat (1/61 vs 1/62); ordering then
  falls to the tiebreak chain — exactly today's behavior for near-equal
  scores. T9/T10 pin this.

## 5. Size / rollout

- store.py: ~40 lines net (fusion block replaced, two constants added,
  one deleted); a `_rrf_fuse()` helper for testability.
- tests: ~250 lines (T1-T10 + golden fixture).
- docs: README x3 semantic-recall paragraph (one sentence: fusion is
  rank-based, admission floors documented), CHANGELOG.
- No migration, no state, no config surface change. Semantic channel
  remains opt-in; users with it off see a provably identical recall.

Estimated total: ~300 lines, 1 module touched + tests + docs.

## 6. Non-goals

- No learned/weighted fusion (weights would need calibration data we
  refuse to collect).
- No third channel (BM25 etc.) — the lexical pass already owns that
  space; if it is ever replaced, RRF extends to n lists unchanged.
- No persisted ranks/indexes (violates derive-on-read).
