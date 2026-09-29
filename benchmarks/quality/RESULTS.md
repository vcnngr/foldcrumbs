# Quality benchmark — semantic channel gate (0.12.0)

Numbers produced on 2026-09-29 by `bench_semantic.py` against the owner's
real store (read-only), on an Intel Xeon W-2140B (macOS, Python 3.12,
onnxruntime 1.23.2, bundled Xenova/all-MiniLM-L6-v2 quantized).

## Results (top-5, served-title match)

| Set | Lexical (0.11.0 baseline) | + bundled semantic (RRF fusion) | FP lexical / semantic |
|---|---|---|---|
| `golden.json` (queries derived from titles — **favours lexical**, declared) | 40/40 (100%) | 40/40 (100%) | 0 / 0 on 10 negatives |
| `paraphrase_golden.json` (zero term overlap — the lexical blind spot) | 6/10 (60%) | **7/10 (70%)** | 3 / **4** on 10 negatives |

**Honest reading:** the semantic channel recovers +1 paraphrase hit at the
cost of +1 false positive in top-5. This does NOT support any "better
recall" marketing claim — and per the Phase-0 protocol no such claim is
made anywhere in README/CHANGELOG. The channel stays opt-in; the trade-off
is now *measured*, not told.

## Method

- `build_golden.py` derives the golden set deterministically from the real
  store: positives = memories `store.search()` actually serves for a query
  derived from their title; negatives = memories not served with ≤1 term
  overlap. Labels come from objective signals, not committer judgment.
- `paraphrase_golden.json` is hand-written (declared): zero-overlap
  paraphrases of real memories, plus clearly-unrelated negatives. Two
  initially-dishonest pairs (semantically relevant pairs labelled
  irrelevant) were corrected **before** running the benchmark.
- `bench_semantic.py` toggles `config.SEMANTIC` per pair — faithful because
  `store.search` reads the flag at runtime (store.py, RRF fusion block).
  The server endpoint is pointed at a dead port on purpose so channel 3
  (bundled, local) serves every semantic call. No network involved.

## Known limits (declared, not hidden)

- n=10 paraphrase positives: directional evidence, not a statistic.
- The Phase-0 "lexical 30%" number referred to a different, now-lost
  paraphrase set (/tmp cleanup). Like-for-like on THIS set: 60% → 70%.
- MiniLM is English-only; no IT set is included (low diagnostic value for
  an EN-only model). A multilingual bundle would need its own set.
- Golden positives favour lexical by construction (title-derived queries);
  the paraphrase set is the one that discriminates.

## Reproduce

```bash
pip install 'foldcrumbs[semantic]' && foldcrumbs embeddings setup
FOLDCRUMBS_DIR=<your store> python benchmarks/quality/bench_semantic.py
```

`golden.json` is store-specific (built from the owner's store); the
paraphrase set references its titles, so both files are committed as the
frozen 2026-09-29 snapshot the numbers above refer to.
