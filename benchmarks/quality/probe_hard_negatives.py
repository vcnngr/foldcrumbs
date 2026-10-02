"""Probe hard-negative candidates: for each query, show what BOTH channels
rank in top-10 against the real store. Hard negatives must be selected
EMPIRICALLY (ranked high by lexical but answering a different question),
not by committer judgment (PR #82 discipline).

Read-only: reinforce+counts neutralized, snapshot checked at exit.
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
os.environ.setdefault("FOLDCRUMBS_STATE_DIR", "/tmp/fc_hn_state")

REPO = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, REPO)
from foldcrumbs import config, embeddings_local, recalls, store  # noqa: E402

assert embeddings_local.available(), embeddings_local.status()
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


STORE = os.environ["FOLDCRUMBS_DIR"]


def snapshot():
    return {f: hashlib.sha256(open(f, "rb").read()).hexdigest()
            for f in glob.glob(os.path.join(STORE, "**", "*"), recursive=True)
            if os.path.isfile(f)}


# Candidate queries — each targets ONE specific memory; the probe shows which
# near-miss memories the channels rank high (hard-negative candidates).
QUERIES = [
    ("what is the default LLM model configured", "engram-llm-model-configuration"),
    ("which port does the local LLM server listen on", "Local LLM server on port 8081"),
    ("which directory is synced across machines", "Only ~/.claude is Syncthing-synced"),
    ("when is memory distillation triggered", "Automatic memory distillation trigger"),
    ("what is the context budget", "Engram Context Budget Configuration"),
    ("which interpreter runs the hooks", "Engram hook interpreter is /usr/local/bin/python3"),
    ("what is the PyPI package name", "foldcrumbs published to PyPI, installable"),
    ("where is the backend state directory", "State dir migrated to ~/.foldcrumbs"),
    ("which machines run claude instances", "MacBook also runs 4 Claude instances"),
    ("what does the extraction prompt guard against", "Extraction Prompt Guardrail"),
    ("is the memory store shared between projects", "Engram memory store is per-project (keyed by cwd)"),
    ("how do new instances get hooks installed", "Install engram hooks into a new instance"),
]

before = snapshot()
out = []
for q, target in QUERIES:
    config.SEMANTIC = False
    lex = [m.title for m in store.search(q, limit=10)]
    config.SEMANTIC = True
    sem = [m.title for m in store.search(q, limit=10)]
    row = {"query": q, "target": target, "lexical": lex, "semantic": sem}
    out.append(row)
    print(f"\n=== {q}   [target: {target[:45]}]")
    for i in range(10):
        lt = lex[i][:55] if i < len(lex) else ""
        st = sem[i][:55] if i < len(sem) else ""
        mark_l = "*" if target[:25].lower() in lt.lower() else " "
        mark_s = "*" if target[:25].lower() in st.lower() else " "
        print(f"  {i+1:>2}. L{mark_l} {lt:<57} | S{mark_s} {st}")

after = snapshot()
assert after == before, "store mutated!"
json.dump(out, open("/tmp/hn_probe.json", "w"), indent=1)
print("\nstore read-only verificato. probe → /tmp/hn_probe.json")
