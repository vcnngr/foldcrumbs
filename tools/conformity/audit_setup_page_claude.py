#!/usr/local/bin/python3.12
"""Conformity audit — executes SETUP-FOR-AGENTS.md end-to-end as an agent
would, in an isolated fake HOME. Manual audit tool (like benchmarks/quality):
NOT part of CI (needs a pip-installed foldcrumbs venv).

Run:
    python3 -m venv /tmp/fc_conf_venv && /tmp/fc_conf_venv/bin/pip install -q .
    FC_BIN=/tmp/fc_conf_venv/bin/foldcrumbs PY_BIN=/tmp/fc_conf_venv/bin/python \
      python3 tools/conformity/audit_setup_page_claude.py
Exit 0 = all claims conform.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

FC = os.environ.get("FC_BIN", "/tmp/fc_conform/venv/bin/foldcrumbs")
PY = os.environ.get("PY_BIN", "/tmp/fc_conform/venv/bin/python")
HOME = Path("/tmp/fc_conform/home")
if HOME.exists():
    shutil.rmtree(HOME)
HOME.mkdir(parents=True)

env = dict(os.environ, HOME=str(HOME))
results = []


def run(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       env=env, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    print(("  OK  " if cond else "  FAIL") + f" | {name}" + (f" — {detail[:140]}" if detail else ""))


print("== step 2: python version check ==")
rc, out = run(f"{PY} --version")
check("python >=3.10", rc == 0 and "3.1" in out, out.strip())

print("== step 3: install verification (import + dependency-free core) ==")
rc, out = run(f"{PY} -c 'import foldcrumbs; print(foldcrumbs.__version__)'")
check("import foldcrumbs", rc == 0, out.strip())
rc, out = run(f"{PY} -m pip show foldcrumbs")
req = [ln for ln in out.splitlines() if ln.startswith("Requires:")]
deps = req[0].split(":", 1)[1].strip() if req else "?"
check("core dependency-free (page: stdlib-only)", deps == "", f"Requires: {deps!r}")

print("== step 4: foldcrumbs install --backend none --no-backend-prompt ==")
rc, out = run(f"{FC} install --backend none --no-backend-prompt")
check("install exits 0", rc == 0, out.strip()[:200])
settings = HOME / ".claude/settings.json"
if settings.exists():
    data = json.loads(settings.read_text())
    hooks = json.dumps(data.get("hooks", {}))
    check("hooks written to ~/.claude/settings.json", "foldcrumbs" in hooks)
else:
    check("hooks written", False, "settings.json missing")
cmds = list((HOME / ".claude/commands").glob("*.md")) if (HOME / ".claude/commands").exists() else []
names = {c.stem for c in cmds}
check("slash commands remember/recall present",
      {"remember", "recall"} <= names, str(sorted(names)))

print("== step 4b: idempotency claim (merge-safe, idempotent) ==")
run(f"{FC} install --backend none --no-backend-prompt")
n1 = settings.read_text().count("foldcrumbs")
run(f"{FC} install --backend none --no-backend-prompt")
n2 = settings.read_text().count("foldcrumbs")
check("re-install idempotent", n1 == n2, f"{n1} -> {n2}")

print("== step 5: backend switching ==")
rc, _ = run(f"{FC} backend none")
check("backend none accepted", rc == 0)
rc, out = run(f"{FC} backend bogus-name")
check("invalid backend rejected (page: names are claude-cli|codex|openai|none)", rc != 0, out.strip()[:120])

print("== step 6: verification round-trip status -> remember -> recall ==")
rc, out = run(f"{FC} status")
check("status exits 0", rc == 0, out.strip()[:120])
rc, out = run(f'{FC} remember "setup verified by conformance test" --type event')
check("remember --type event exits 0", rc == 0, out.strip()[:120])
rc, out = run(f'{FC} recall "setup verified"')
check("recall returns the written memory (round-trip)", rc == 0 and "setup verified" in out,
      out.strip()[:160])

print("== step 6b: store-is-markdown claim ==")
stores = list((HOME / ".claude/projects").glob("*/memory/*.md"))
check("store contains markdown files", len(stores) >= 2, f"{len(stores)} md files")

print("== Hermes path: profile import/env ==")
(HOME / ".hermes/profiles/testagent").mkdir(parents=True)
rc, out = run(f"{FC} profile import --agent hermes")
check("profile import dry-run exits 0", rc == 0, out.strip()[:140])
rc, out = run(f"{FC} profile import --agent hermes --apply")
check("profile import --apply exits 0", rc == 0, out.strip()[:140])
rc, out = run(f"{FC} profile env testagent")
check("profile env prints env line", rc == 0 and "FOLDCRUMBS" in out, out.strip()[:160])

print("== optional paths mentioned by the page (graceful without extra) ==")
rc, out = run(f"{FC} embeddings status")
check("embeddings status graceful without [semantic] model", rc == 0, out.strip()[:140])
rc, out = run(f"{FC} roots list")
check("roots list exits 0", rc == 0, out.strip()[:100])

print("== hygiene claim: uninstall removes only ours ==")
rc, out = run(f"{FC} uninstall")
check("uninstall exits 0", rc == 0, out.strip()[:120])
if settings.exists():
    data = json.loads(settings.read_text())
    hooks = json.dumps(data.get("hooks", {}))
    check("uninstall removed foldcrumbs hooks", "foldcrumbs" not in hooks)
else:
    check("uninstall removed config", True, "settings.json gone")

fails = [r for r in results if not r[0]]
print()
print(f"RESULT: {len(results)-len(fails)}/{len(results)} PASS" + (f", FAILURES: {[f[1] for f in fails]}" if fails else " — ALL CONFORM"))
raise SystemExit(1 if fails else 0)
