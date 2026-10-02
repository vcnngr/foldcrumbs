"""Benchmark gate 0.12.0: recall lessicale (0.11.0 baseline) vs recall
semantico bundled (MiniLM locale), sugli stessi golden set della Fase 0.
Read-only sullo store reale (garantito e verificato a fine run). Nessuna
rete: il bundled è locale.

config.SEMANTIC è letto a runtime da store.search (store.py:1149), quindi il
toggle lessicale/semantico per-pair è fedele, non approssimato.
"""
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ["FOLDCRUMBS_SEMANTIC"] = "1"
os.environ["FOLDCRUMBS_EMBEDDING_MODEL"] = "minilm-bundled"
os.environ["FOLDCRUMBS_EMBEDDING_ENDPOINT"] = "http://127.0.0.1:1"  # morto → forza il bundled
os.environ.setdefault("FOLDCRUMBS_STATE_DIR", "/tmp/fc_sem_state")

# REPO derived from THIS file's location (RT PR #82 r4 P0): see
# build_golden.py — a hardcoded owner path breaks any other checkout and
# can mask the test by importing/writing a different one.
REPO = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, REPO)
from foldcrumbs import config, embeddings_local, recalls, store  # noqa: E402

assert embeddings_local.available(), embeddings_local.status()

# READ-ONLY + REPRODUCIBILITY GUARANTEE (RT PR #82 P0-1 e r3 P0-1):
# store.search() reinforces recall stats (write → .recalls.json) AND reads
# host-local counts into the tiebreak (recalls.counts). A benchmark must
# neither mutate the store it measures nor depend on host-local state:
# neutralize both, then prove byte-equality at the end.
recalls.reinforce = lambda *a, **k: None  # noqa: E731
recalls.counts = lambda *a, **k: {}  # noqa: E731

# HERMETIC ENV (RT t_316f39c7 P0): with a real FOLDCRUMBS_STATE_DIR the
# search ALSO scans federation roots (~/.foldcrumbs/roots/*.json) on
# time-bounded threads it stops waiting for → foreign duplicates enter the
# top-5 NONDETERMINISTICALLY (host/cache dependent: same command, same
# minute, gave 6/6 then 7/6 on 2026-10-02). This benchmark measures the
# two channels over ONE store: federation is host state, out of scope.
# Third neutralization alongside reinforce+counts.
store.iter_federated = lambda *a, **k: iter(())  # noqa: E731


QDIR = os.path.join(REPO, "benchmarks/quality")
STORE = os.environ["FOLDCRUMBS_DIR"]


def _snapshot():
    return {f: hashlib.sha256(open(f, "rb").read()).hexdigest()
            for f in glob.glob(os.path.join(STORE, "**", "*"), recursive=True)
            if os.path.isfile(f)}


def match(served_titles, expected):
    e = expected.lower()[:30]
    return any(e in t.lower() for t in served_titles)


def run(path, top_k=5):
    pairs = json.load(open(path))
    pos = [p for p in pairs if p["label"] == "relevant"]
    neg = [p for p in pairs if p["label"] != "relevant"]
    lex_hit = sem_hit = lex_fp = sem_fp = 0
    for p in pos:
        config.SEMANTIC = False
        lex = [m.title for m in store.search(p["query"], limit=top_k)]
        config.SEMANTIC = True
        sem = [m.title for m in store.search(p["query"], limit=top_k)]
        lex_hit += match(lex, p["memory"])
        sem_hit += match(sem, p["memory"])
    for p in neg:
        config.SEMANTIC = False
        lex = [m.title for m in store.search(p["query"], limit=top_k)]
        config.SEMANTIC = True
        sem = [m.title for m in store.search(p["query"], limit=top_k)]
        lex_fp += match(lex, p["memory"])
        sem_fp += match(sem, p["memory"])
    return len(pos), len(neg), lex_hit, sem_hit, lex_fp, sem_fp


def main():
    before = _snapshot()
    print(f"{'set':28} {'lessicale':>16} {'bundled-sem':>16}  FP(lex/sem)")
    print("-" * 78)
    for name in ("golden.json", "paraphrase_golden.json", "hard_negatives_golden.json"):
        path = os.path.join(QDIR, name)
        if not os.path.exists(path):
            print(f"{name}: ASSENTE")
            continue
        npos, nneg, lh, sh, lfp, sfp = run(path)
        if npos == 0:
            # negatives-only set (hard negatives): the ONLY signal is the FP
            # rate near the decision frontier — no positives to report.
            print(f"{name:28} {'—':>16} {'—':>16}   {lfp}/{sfp} su {nneg} neg")
        else:
            print(f"{name:28} {lh:>3}/{npos:<3} ({100*lh//npos:>3}%)   "
                  f"{sh:>3}/{npos:<3} ({100*sh//npos:>3}%)   {lfp}/{sfp} su {nneg} neg")
    after = _snapshot()
    if after != before:
        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        print(f"\nERRORE: il benchmark ha mutato lo store: {changed}",
              file=sys.stderr)
        sys.exit(1)
    print("\nstore read-only verificato: byte-identico prima/dopo (sha256).")


if __name__ == "__main__":
    main()
