"""Hermeticity proof for the hard-negative benchmark (RT t_2ce01083 P0-1).

r2 went RED because the frozen ranks depended on host state twice:
federation roots (fixed in r2) and the semantic embedding cache in the
state dir (stale-basis vectors shifted one rank). This script PROVES the
fix: it runs the detail benchmark under TWO scratch state-dirs (clean
cache, only the model copied in) plus optionally the host state-dir, and
asserts all outputs are byte-identical and that the divergence guard
passes under every one of them.

Prerequisite: the bundled model must be installed
(`foldcrumbs embeddings setup`). Local audit tool, like
verify_reproducible.py — not part of CI (no model there).
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable
_store = os.environ.get("FOLDCRUMBS_DIR")
if not _store:
    print("FOLDCRUMBS_DIR required (the store the frozen set was built on)", file=sys.stderr)
    sys.exit(2)
STORE: str = _store

HOST_STATE = Path(os.path.expanduser("~/.foldcrumbs"))
BUNDLE = HOST_STATE / "bundled"
if not (BUNDLE / "model_quantized.onnx").exists():
    print("bundled model not installed — run: foldcrumbs embeddings setup", file=sys.stderr)
    sys.exit(2)


def run(env_state: str, script: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["FOLDCRUMBS_DIR"] = STORE
    env["FOLDCRUMBS_STATE_DIR"] = env_state
    return subprocess.run([PY, str(REPO / "benchmarks/quality" / script)],
                          env=env, capture_output=True, text=True, timeout=900)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main() -> None:
    scenarios = []
    scratch_dirs: list[str] = []
    for i in range(2):  # two scratch state-dirs: clean cache, model copied
        d = tempfile.mkdtemp(prefix=f"fc_hermetic_{i}_")
        shutil.copytree(BUNDLE, Path(d) / "bundled")
        scenarios.append((f"scratch-{i}", d))
        scratch_dirs.append(d)
    scenarios.append(("host", str(HOST_STATE)))

    detail_hashes = {}
    failures = []
    for name, state in scenarios:
        det = run(state, "detail_hard_negatives.py")
        if det.returncode != 0:
            failures.append(f"{name}: detail rc={det.returncode} {det.stderr.strip()[:150]}")
            continue
        # only the rank table lines (skip the read-only confirmation line,
        # which is identical anyway) — hash the table
        rows = "\n".join(ln for ln in det.stdout.splitlines() if ln.strip() and "read-only" not in ln)
        detail_hashes[name] = sha(rows)
        ver = run(state, "verify_hard_negatives.py")
        if ver.returncode != 0:
            failures.append(f"{name}: verify rc={ver.returncode} {ver.stdout.strip()[:150]}{ver.stderr.strip()[:150]}")
        bench = run(state, "bench_semantic.py")
        if bench.returncode != 0:
            failures.append(f"{name}: bench rc={bench.returncode}")
        elif "7/6 su 7 neg" not in bench.stdout and "7/6 on 7" not in bench.stdout:
            failures.append(f"{name}: bench hard-neg row not 7/6:\n"
                            + "\n".join(ln for ln in bench.stdout.splitlines() if "hard" in ln))
        print(f"  {name:10} detail_sha={detail_hashes.get(name)} verify=ok bench=7/6")

    distinct = set(detail_hashes.values())
    if len(distinct) != 1:
        failures.append(f"detail output DIFFERS across state-dirs: {detail_hashes}")

    # Cleanup removes ONLY the scratch dirs created above — NEVER the host
    # state dir (an earlier version deleted it: real bug, caught in review of
    # my own tool before the RT round; the host's roots registry is
    # marker-derived and was re-registered, the bundle re-downloaded).
    for d in scratch_dirs:
        shutil.rmtree(d, ignore_errors=True)

    if failures:
        print("\nERMITICITÀ VIOLATA:", file=sys.stderr)
        for f in failures:
            print("  ✗", f, file=sys.stderr)
        sys.exit(1)
    print(f"\nOK — {len(scenarios)} state-dir (2 scratch clean-cache + host): "
          f"detail byte-identico (sha {distinct.pop()}), guard verde, bench 7/6 ovunque.")


if __name__ == "__main__":
    main()
