"""Prova r3: il builder è indipendente dal sidecar .recalls.json host-local.
Gira il builder 3 volte con sidecar diversi (assente, pieno, ostile) su una
COPIA dello store e confronta gli sha256 dei golden.json prodotti.
Lo store reale non viene toccato (lavoro su copia)."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Store: env FOLDCRUMBS_DIR o lo store reale del proprietario. Repo e
# python derivati da dove vive QUESTO file — funziona su qualsiasi host.
STORE = os.environ.get("FOLDCRUMBS_DIR") or os.path.expanduser(
    "~/.claude/projects/-Users-vincenzoingrosso-Documents-claude-foldcrumbs/memory")
REPO = str(Path(__file__).resolve().parents[2])
PY = sys.executable

results = []
for scenario in ("sidecar-assente", "sidecar-pieno", "sidecar-ostile"):
    with tempfile.TemporaryDirectory() as d:
        store_copy = os.path.join(d, "store")
        shutil.copytree(STORE, store_copy)
        sc = os.path.join(store_copy, ".recalls.json")
        if os.path.exists(sc):
            os.unlink(sc)
        if scenario == "sidecar-pieno":
            # conteggi alti su memorie a caso
            titles = [f[:-3] for f in os.listdir(store_copy) if f.endswith(".md")]
            json.dump({t: 999 for t in titles[:20]}, open(sc, "w"))
        elif scenario == "sidecar-ostile":
            json.dump({"Project renamed engram - foldcrumbs": 10 ** 6},
                      open(sc, "w"))
        env = dict(os.environ, FOLDCRUMBS_DIR=store_copy)
        r = subprocess.run(
            [PY, os.path.join(REPO, "benchmarks/quality/build_golden.py")],
            capture_output=True, text=True, env=env, timeout=300)
        assert r.returncode == 0, r.stderr[-500:]
        # il builder scrive in REPO/benchmarks/quality/golden.json
        gj = os.path.join(REPO, "benchmarks/quality/golden.json")
        sha = hashlib.sha256(open(gj, "rb").read()).hexdigest()
        results.append((scenario, sha))
        # ripristina il golden committato per la scenario successiva
        subprocess.run(["git", "-C", REPO, "checkout", "--",
                        "benchmarks/quality/golden.json"], check=True)

for s, sha in results:
    print(f"{s:18} {sha[:16]}")
uniq = {sha for _, sha in results}
committed = hashlib.sha256(open(os.path.join(
    REPO, "benchmarks/quality/golden.json"), "rb").read()).hexdigest()
print(f"{'committato':18} {committed[:16]}")
ok = len(uniq) == 1 and uniq == {committed}
print("\nINDIPENDENTE DAL SIDECAR + UGUALE AL COMMITTATO:", "SÌ" if ok else "NO")
sys.exit(0 if ok else 1)
