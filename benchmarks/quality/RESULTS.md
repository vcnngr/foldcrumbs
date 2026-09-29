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
  store: positives = memories `store.search()` actually serves for a query
  derived from their title; negatives = **at most one per positive query**
  (10 distinct queries), never served by it, ≤1 title-term overlap. Labels
  come from objective signals, not committer judgment. Reinforcement is
  neutralized before the first search (read-only).
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

## Honesty notes (RT PR #82 round 1 findings, all closed)

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
deterministically from that store (verified: two runs byte-identical,
store untouched).
