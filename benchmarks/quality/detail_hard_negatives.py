"""Per-pair FP detail for hard_negatives_golden.json — which channel serves
which wrong memory at which rank. Read-only (same neutralization as bench)."""
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ["FOLDCRUMBS_SEMANTIC"] = "1"
os.environ["FOLDCRUMBS_EMBEDDING_MODEL"] = "minilm-bundled"
os.environ["FOLDCRUMBS_EMBEDDING_ENDPOINT"] = "http://127.0.0.1:1"
os.environ.setdefault("FOLDCRUMBS_STATE_DIR", os.path.expanduser("~/.foldcrumbs"))

REPO = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, REPO)
from foldcrumbs import config, embeddings_local, recalls, store  # noqa: E402

assert embeddings_local.available(), embeddings_local.status()
recalls.reinforce = lambda *a, **k: None  # noqa: E731
recalls.counts = lambda *a, **k: {}  # noqa: E731

STORE = os.environ["FOLDCRUMBS_DIR"]


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


before = snapshot()
pairs = json.load(open(os.path.join(REPO, "benchmarks/quality/hard_negatives_golden.json")))
print(f"{'query':<48} {'memoria sbagliata':<42} L-rank S-rank")
for p in pairs:
    config.SEMANTIC = False
    lex = [m.title for m in store.search(p["query"], limit=5)]
    config.SEMANTIC = True
    sem = [m.title for m in store.search(p["query"], limit=5)]
    lr, sr = rank_of(lex, p["memory"]), rank_of(sem, p["memory"])
    print(f"{p['query'][:46]:<48} {p['memory'][:40]:<42} {str(lr or '-'):>6} {str(sr or '-'):>6}")
after = snapshot()
assert after == before, "store mutated!"
print("\nstore read-only verificato.")
