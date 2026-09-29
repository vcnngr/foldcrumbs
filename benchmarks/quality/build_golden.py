"""Ricostruisce il golden set dal negozio reale (READ-ONLY).

Metodo (lo stesso della Fase 0, dichiarato):
- positivi: memorie che store.search() serve davvero sulla query derivata
  dal titolo (caso FAVOREVOLE al lessicale — dichiarato)
- negativi: memorie non servite con zero overlap lessicale coi termini query
Le etichette derivano da segnali oggettivi, non da giudizi del committer.
"""
import json
import os
import re
import sys
from pathlib import Path

# REPO derived from THIS file's location (RT PR #82 r4 P0): a hardcoded
# ~/Documents/claude/foldcrumbs broke on any other checkout — from an
# ordinary clone it raised ModuleNotFoundError, and on the owner's host it
# could silently import/write a DIFFERENT checkout, masking the test.
REPO = str(Path(__file__).resolve().parents[2])

STORE = os.environ.get("FOLDCRUMBS_DIR") or os.path.expanduser(
    "~/.claude/projects/-Users-vincenzoingrosso-Documents-claude-foldcrumbs/memory")
os.environ["FOLDCRUMBS_DIR"] = STORE
sys.path.insert(0, REPO)
from foldcrumbs import store  # noqa: E402

STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "to", "is", "it",
        "for", "with", "at", "by", "from"}


def derive_query(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    words = [w for w in words if w not in STOP and len(w) > 2]
    return " ".join(words[:6])


def build():
    # READ-ONLY + REPRODUCIBILITY GUARANTEE (RT PR #82 P0-1 e r3 P0-1):
    # store.search() both WRITES recall stats (recalls.reinforce →
    # .recalls.json) and READS them (recalls.counts → tiebreak in
    # store.py). The write would mutate the store being measured; the read
    # would make selection host-local (sidecar counts differ per machine).
    # Neutralize BOTH before the first search: the builder then depends
    # only on the store's markdown content, identical on any host.
    from foldcrumbs import recalls
    recalls.reinforce = lambda *a, **k: None  # noqa: E731
    recalls.counts = lambda *a, **k: {}       # noqa: E731

    recs = list(store.iter_memories())
    recs = [r for r in recs if getattr(r, "status", "active") == "active"]
    # Deterministic on ANY machine (RT PR #82 r2 P0): iter_memories() walks
    # the directory in filesystem order, which differs between hosts — the
    # reviewer regenerated a different golden.json from the same store and
    # script. Sorting by title makes selection order-independent.
    recs.sort(key=lambda r: r.title)
    pairs = []
    used_titles = set()
    # positivi: query derivata dal titolo → la memoria deve essere servita
    for r in recs:
        q = derive_query(r.title)
        if len(q.split()) < 2 or r.title in used_titles:
            continue
        served = store.search(q, limit=10)
        if any(m.title == r.title for m in served):
            pairs.append({"query": q, "memory": r.title, "label": "relevant"})
            used_titles.add(r.title)
        if len(pairs) >= 40:
            break
    # negativi: AL PIÙ UNO per query positiva, a giro (RT PR #82 P0-3:
    # 10 negativi tutti sulla prima query non misuravano copertura).
    # Criterio oggettivo: la query non serve quella memoria e l'overlap di
    # termini titolo-vs-query è ≤1.
    neg_needed = 10
    positives = list(pairs)
    for p in positives:
        if neg_needed <= 0:
            break
        qterms = set(p["query"].split())
        served = store.search(p["query"], limit=10)
        served_titles = {m.title for m in served}
        for r in recs:
            if r.title in used_titles or r.title in served_titles:
                continue
            terms = set(re.findall(r"[a-z0-9]+", r.title.lower()))
            if len(terms & qterms) > 1:
                continue                  # overlap alto → ambiguo, scartato
            pairs.append({"query": p["query"], "memory": r.title,
                          "label": "irrelevant"})
            used_titles.add(r.title)
            neg_needed -= 1
            break                           # max 1 negativo per query
    return pairs


if __name__ == "__main__":
    out = build()
    dest = os.path.join(REPO, "benchmarks/quality/golden.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1)
    pos = sum(1 for p in out if p["label"] == "relevant")
    print(f"golden set: {len(out)} coppie ({pos} pos / {len(out)-pos} neg) → {dest}")
