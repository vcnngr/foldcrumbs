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

STORE = os.path.expanduser(
    "~/.claude/projects/-Users-vincenzoingrosso-Documents-claude-foldcrumbs/memory")
REPO = os.path.expanduser("~/Documents/claude/foldcrumbs")
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
    recs = list(store.iter_memories())
    recs = [r for r in recs if getattr(r, "status", "active") == "active"]
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
    # negativi: per ogni query positiva, memorie che quella query NON serve
    # e con cui condividono al più 1 termine (overlap minimo, non ambiguo)
    neg_needed = 10
    for p in list(pairs):
        if neg_needed <= 0:
            break
        qterms = set(p["query"].split())
        served = store.search(p["query"], limit=10)
        served_titles = {m.title for m in served}
        for r in recs:
            if neg_needed <= 0:
                break
            if r.title in used_titles or r.title in served_titles:
                continue
            terms = set(re.findall(r"[a-z0-9]+", r.title.lower()))
            if len(terms & qterms) > 1:
                continue                  # overlap alto → ambiguo, scartato
            pairs.append({"query": p["query"], "memory": r.title,
                          "label": "irrelevant"})
            used_titles.add(r.title)
            neg_needed -= 1
    return pairs


if __name__ == "__main__":
    out = build()
    dest = os.path.join(REPO, "benchmarks/quality/golden.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1)
    pos = sum(1 for p in out if p["label"] == "relevant")
    print(f"golden set: {len(out)} coppie ({pos} pos / {len(out)-pos} neg) → {dest}")
