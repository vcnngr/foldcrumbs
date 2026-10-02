# Quality benchmark — semantic channel gate (0.12.0)

Numbers produced on 2026-09-29 by `bench_semantic.py` against the owner's
real store (read-only — guaranteed AND verified byte-for-byte at the end of
every run), on an Intel Xeon W-2140B (macOS, Python 3.12, onnxruntime
1.23.2, bundled Xenova/all-MiniLM-L6-v2 quantized).

## Results (top-5, served-title match)

| Set | Lexical (0.11.0 baseline) | + bundled semantic (RRF fusion) | FP lexical / semantic |
|---|---|---|---|
| `golden.json` (title-derived queries — **favours lexical**, declared) | 40/40 (100%) | 40/40 (100%) | 0 / 0 on 10 negatives (10 distinct queries) |
| `paraphrase_golden.json` (low-overlap paraphrases, verified pair-by-pair) | 7/10 (70%) | **10/10 (100%)** | 0 / 0 on 10 negatives |
| `hard_negatives_golden.json` (near-miss memories, empirically selected — see below) | — | — | **6 / 6 on 7 negatives** |

## Hard negatives (added 2026-10-02 — closes the r2-P1 backlog item)

The 0/0 FP rows above are bounded by EASY negatives (clearly-unrelated
memories). `hard_negatives_golden.json` measures the false-positive rate
NEAR the decision frontier: each negative is a memory that shares the
topic/vocabulary of the query but answers a DIFFERENT question, selected
**empirically** — `probe_hard_negatives.py` ran both channels over 12
queries against the real store and only memories actually ranked high
(lexical rank 1-7 in top-10) were admitted as candidates; labels were then
audited pair-by-pair against the full memory content (same discipline as
the paraphrase set).

**Result: 6/7 hard negatives are served in top-5 by BOTH channels**
(per-pair ranks in `detail_hard_negatives.py` output: lexical
1,1,3,3,5,5; semantic 1,1,1,3,3,5). The 7th falls out of top-5 for both.

**Honest reading — this is the benchmark doing its job:**
- The semantic channel's value is RECALL on paraphrases (70%→100%), not
  precision at the frontier: on near-misses it is no better than lexical
  (and promoted one wrong memory from rank 3 to rank 1). Both channels
  confuse topically-adjacent memories ("context budget" vs "context
  MONITORING"; store shared "between projects" vs "between INSTANCES").
- No claim of "zero false positives" may be made outside the easy-negative
  sets. Near the frontier the measured FP rate is ~86% (6/7) for both.
- Mitigations are structural, not ranking: served memories carry their
  title+type so the agent can see WHAT answered, and the AGENTS.md loop
  tells it to verify against source. A relevance-threshold or a
  cross-encoder reranker would be the ranking-level fix — both out of
  scope for the stdlib core / opt-in bundle and left as declared backlog.
- Borderline labels declared: "Per-instance memory, shared ~/.engram
  backend" gives an agent PARTIAL information for "is the store shared
  between projects" (it answers the instance axis, not the project axis).
  Labelled irrelevant because the specific question (cwd keying) is
  answered by a different memory that both channels also serve.

The 3 semantic rescues were inspected one by one (counter-proof script):
"remember across sessions"→*Memory Persistence*, "organized into
layers"→*Dual-layer architecture*, "model identifiers named"→*Model ID
Naming Convention*. All three are genuine semantic matches with no lexical
path (synonyms/morphology only) — not label artifacts.

**Honest reading:** on a small, hand-verified paraphrase set the bundled
channel recovers every miss with zero false positives. This is *strong
directional evidence*, not a statistic: n=10 positives, one store, one
model, one host. The earlier run (60%→70%, +1 FP) measured a set that
contained two mislabelled pairs — corrected here (see Honesty notes). No
"better recall" marketing claim is made anywhere in README/CHANGELOG: the
channel stays opt-in and these numbers bound what we say about it.

## Method

- `build_golden.py` derives the golden set deterministically from the real
  store: records are **sorted by title before selection** (filesystem walk
  order differs between hosts — RT r2 P0 — so the committed snapshot is
  reproducible on any machine, not just this one), positives = memories
  `store.search()` actually serves for a query derived from their title;
  negatives = **at most one per positive query** (10 distinct queries),
  never served by it, ≤1 title-term overlap. Labels come from objective
  signals, not committer judgment. BOTH recall-stats touch-points are
  neutralized before the first search: `reinforce` (the write → read-only)
  AND `counts` (the host-local read → reproducible on any machine
  regardless of its `.recalls.json` sidecar).
- `hard_negatives_golden.json` is empirically selected: `probe_hard_negatives.py`
  dumps both channels' top-10 for 12 targeted queries (read-only, same
  neutralization); candidates were admitted only if a channel ranked them
  high, then each label was audited against the full memory content. The
  file records the lexical rank at selection time and the `why` per pair.
  `detail_hard_negatives.py` re-runs the per-pair ranks (regression tool).
- `paraphrase_golden.json` is hand-written (declared) and was audited
  pair-by-pair against the real memory descriptions: every positive is a
  genuine answer to its query; every negative is genuinely unrelated.
  Overlap of positives vs the indexed text (title+description) is ≤3
  common tokens; negatives have 0.
- `bench_semantic.py` toggles `config.SEMANTIC` per pair — faithful because
  `store.search` reads the flag at runtime (RRF fusion block). The server
  endpoint points at a dead port on purpose so channel 3 (bundled, local)
  serves every semantic call. The RT reviewer instrumented this and
  confirmed: bundle channel really served, not a silent lexical fallback.
  Run ends with a sha256 snapshot comparison of the whole store; any
  mutation exits non-zero.

## Honesty notes (RT PR #82 findings across rounds 1–3, all closed)

- P0-1: the first version of these scripts called `store.search()` without
  neutralizing recall-statistics reinforcement → `.recalls.json` was
  rewritten. Fixed: reinforcement neutralized in BOTH scripts, plus the
  end-of-run byte-equality proof.
- P0-2: the first paraphrase set contained two indefensible labels
  ("index organized"→"Dual-layer architecture" labelled irrelevant while
  the memory describes exactly that; "push to production"→"project
  status" labelled relevant on a stale memory) and the "zero overlap"
  claim was measured on titles only. Fixed: set rewritten and audited
  against real descriptions; the claim above states the real overlap.
- P0-3: all 10 golden negatives hung off a single query (greedy loop).
  Fixed: max one negative per query, regenerated, diversity asserted.
- r2 P0 (RT round 2): the committed golden.json was NOT reproducible on
  the reviewer's host — iter_memories() walks in filesystem order, which
  differs per machine. Fixed: sort-by-title before selection; two
  consecutive runs are byte-identical (sha256 verified) and the benchmark
  numbers on the regenerated file are unchanged (40/40; 7/10→10/10; FP
  0/0).
- r3 P0 (RT round 3): sort-by-title was NOT enough — store.search() also
  READS the host-local `.recalls.json` counts into its tiebreak, so the
  reviewer's regeneration still differed (one negative pair flipped: a
  memory served in their top-10 but not in ours). Fixed: neutralize
  `recalls.counts` too, in builder AND benchmark. Proof:
  `verify_reproducible.py` runs the builder 3× on store copies with
  absent / full / hostile sidecars — all three sha256-identical to the
  committed blob (cdfe32e7…). The builder now depends only on the
  store's markdown content.
- r2 P1 (declared, backlog — reviewer's own framing, not a veto): the
  paraphrase negatives are all clearly-unrelated (overlap 0), a good smoke
  test but a WEAK measure of the false-positive rate near the decision
  frontier. The 0/0 FP claim is therefore bounded by easy negatives.
  Follow-up: hard negatives (near-miss memories the lexical channel ranks
  high but that answer a different question). — **CLOSED 2026-10-02**:
  `hard_negatives_golden.json` added; frontier FP measured 6/7 for both
  channels; see the Hard negatives section above.

## Known limits (declared, not hidden)

- n=10 paraphrase positives: directional evidence, not a statistic.
- The Phase-0 "lexical 30%" number referred to a different, lost set
  (/tmp cleanup); it is NOT comparable to the 70% here.
- MiniLM is English-only; no IT set is included (low diagnostic value for
  an EN-only model). A multilingual bundle would need its own set.
- Golden positives favour lexical by construction (title-derived queries);
  the paraphrase set is the discriminating one.
- The store is the owner's real one (~70 memories, EN-dominant): results
  may differ on larger/multilingual stores.

## Reproduce

```bash
pip install 'foldcrumbs[semantic]' && foldcrumbs embeddings setup
FOLDCRUMBS_DIR=<your store> python benchmarks/quality/bench_semantic.py
```

`golden.json` is store-specific; the paraphrase set references its
titles. Both files are committed as the frozen 2026-09-29 snapshot these
numbers refer to. `build_golden.py` regenerates `golden.json`
byte-identically from that store on ANY host: selection is sorted by
title and both recall-stats touch-points are neutralized, so the output
depends only on the store's markdown. Run
`python benchmarks/quality/verify_reproducible.py` to prove it
(3 sidecar scenarios, all sha256-identical).
