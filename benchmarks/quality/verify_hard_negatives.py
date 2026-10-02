"""Divergence guard for the hard-negative set (RT t_316f39c7 P0).

r1 went RED because RESULTS.md + hard_negatives_golden.json drifted from the
live benchmark output (stale ranks, 6/6 vs the real 7/6). This script
recomputes the per-pair FP ranks and the FP counts from the REAL store and
asserts that both committed artefacts still match — so any future change to
the store, the ranking, or the fusion makes the docs fail loudly instead of
silently lying.

Hermetic: reinforce + counts + federation all neutralized (same as
bench_semantic.py), so the numbers depend only on the store's markdown.

Exit 0 = artefacts agree with live output. Exit 1 = divergence (printed).
Read-only on the store (sha256 snapshot checked at exit).
"""
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ["FOLDCRUMBS_SEMANTIC"] = "1"
os.environ["FOLDCRUMBS_EMBEDDING_MODEL"] = "minilm-bundled"
os.environ["FOLDCRUMBS_EMBEDDING_ENDPOINT"] = "http://127.0.0.1:1"  # dead → forces bundled
os.environ.setdefault("FOLDCRUMBS_STATE_DIR", os.path.expanduser("~/.foldcrumbs"))

REPO = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, REPO)
from foldcrumbs import config, embeddings, embeddings_local, recalls, store  # noqa: E402

assert embeddings_local.available(), embeddings_local.status()
recalls.reinforce = lambda *a, **k: None  # noqa: E731
recalls.counts = lambda *a, **k: {}  # noqa: E731
store.iter_federated = lambda *a, **k: iter(())  # noqa: E731
# Fourth neutralization (RT t_2ce01083 P0-1): the semantic embedding CACHE
# lives in the state dir (embeddings._cache_path) and stale vectors from an
# older basis changed one frontier rank (host S-rank 2 vs clean-cache 3).
# Neutralizing load/save forces fresh computation on every run — the ranks
# then depend only on the store's markdown + the pinned model, on ANY host.
embeddings._load_cache = lambda: {}  # noqa: E731
embeddings._save_cache = lambda *a, **k: None  # noqa: E731
  # hermetic (see header)

STORE = os.environ["FOLDCRUMBS_DIR"]
QDIR = os.path.join(REPO, "benchmarks/quality")


def snapshot():
    return {f: hashlib.sha256(open(f, "rb").read()).hexdigest()
            for f in glob.glob(os.path.join(STORE, "**", "*"), recursive=True)
            if os.path.isfile(f)}


def rank_of(titles, mem):
    e = mem.lower()[:30]
    for i, t in enumerate(titles):
        if e in t.lower():
            return i + 1
    return None


def live_ranks(top_k=5):
    """(query, memory) → (lexical_rank|None, semantic_rank|None), live."""
    pairs = json.load(open(os.path.join(QDIR, "hard_negatives_golden.json")))
    out = []
    for p in pairs:
        config.SEMANTIC = False
        lex = [m.title for m in store.search(p["query"], limit=top_k)]
        config.SEMANTIC = True
        sem = [m.title for m in store.search(p["query"], limit=top_k)]
        out.append((p["query"], p["memory"], rank_of(lex, p["memory"]), rank_of(sem, p["memory"])))
    return out


def main():
    before = snapshot()
    live = live_ranks()
    lex_fp = sum(1 for _, _, lr, _ in live if lr is not None)
    sem_fp = sum(1 for _, _, _, sr in live if sr is not None)
    n = len(live)

    errors = []

    # 1) committed JSON ranks must match live ranks
    pairs = json.load(open(os.path.join(QDIR, "hard_negatives_golden.json")))
    for (q, mem, lr, sr), p in zip(live, pairs):
        assert q == p["query"] and mem == p["memory"], "JSON/live pair misalignment"
        if p.get("lexical_rank") != lr:
            errors.append(f"JSON lexical_rank {p.get('lexical_rank')} != live {lr} for {q!r}")
        if p.get("semantic_rank") != sr:
            errors.append(f"JSON semantic_rank {p.get('semantic_rank')} != live {sr} for {q!r}")

    # 2) RESULTS.md table row must carry the live FP counts "lex / sem on N"
    results = open(os.path.join(QDIR, "RESULTS.md")).read()
    want_row = f"**{lex_fp} / {sem_fp} on {n} negatives**"
    if want_row not in results:
        errors.append(f"RESULTS.md hard-negatives FP cell must read {want_row!r}")
    if f"{lex_fp}/{n}" not in results:
        errors.append(f"RESULTS.md must state the lexical FP {lex_fp}/{n} in the reading")
    if f"{sem_fp}/{n}" not in results:
        errors.append(f"RESULTS.md must state the semantic FP {sem_fp}/{n} in the reading")

    after = snapshot()
    if after != before:
        errors.append("store mutated during verification (should be read-only)")

    if errors:
        print("DIVERGENZA — RESULTS.md / JSON non allineati all'output live:\n", file=sys.stderr)
        for e in errors:
            print("  ✗", e, file=sys.stderr)
        print(f"\nlive: lexical {lex_fp}/{n}, semantic {sem_fp}/{n}", file=sys.stderr)
        sys.exit(1)
    print(f"OK — artefatti allineati all'output live: lexical {lex_fp}/{n}, semantic {sem_fp}/{n}; store read-only verificato.")


if __name__ == "__main__":
    main()
