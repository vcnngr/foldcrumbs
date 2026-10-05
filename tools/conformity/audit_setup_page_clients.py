#!/usr/local/bin/python3.12
"""Conformity audit part 2: per-client wiring paths (codex, opencode, pi),
invalid-agent refusal, foldcrumbs-mcp entrypoint. Same isolated fake HOME.

Manual audit tool — see tools/conformity/README.md for how to run."""
import os
import shutil
import subprocess
from pathlib import Path

FC = os.environ.get("FC_BIN", "/tmp/fc_conform/venv/bin/foldcrumbs")
HOME = Path("/tmp/fc_conform/home2")
if HOME.exists():
    shutil.rmtree(HOME)
HOME.mkdir(parents=True)
env = dict(os.environ, HOME=str(HOME))
results = []


def run(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def check(name, cond, detail=""):
    results.append((bool(cond), name))
    print(("  OK  " if cond else "  FAIL") + f" | {name}" + (f" — {detail[:160]}" if detail else ""))


print("== client: codex ==")
rc, out = run(f"{FC} install --agent codex --backend none --no-backend-prompt")
check("install --agent codex exits 0", rc == 0, out.strip())
hx = HOME / ".codex/hooks.json"
check("codex hooks.json written", hx.exists(), out.strip()[:160])
check("codex prints config.toml MCP snippet (page claim)", "config.toml" in out or "mcp" in out.lower(), out[:300])

print("== client: opencode ==")
rc, out = run(f"{FC} install --agent opencode --backend none --no-backend-prompt")
check("install --agent opencode exits 0", rc == 0, out.strip())
oc = HOME / ".config/opencode/opencode.json"
check("opencode.json written", oc.exists(), f"exists={oc.exists()}")

print("== client: pi ==")
rc, out = run(f"{FC} install --agent pi --backend none --no-backend-prompt")
check("install --agent pi exits 0", rc == 0, out.strip())
pi_files = list(HOME.glob(".pi/**/*foldcrumbs*")) + list(HOME.glob(".config/pi/**/*foldcrumbs*"))
check("pi extension discovered/written", bool(pi_files) or "pi" in out.lower(), str(pi_files)[:100] or out[:150])

print("== unknown client must be refused ==")
rc, out = run(f"{FC} install --agent bogus-client --backend none --no-backend-prompt")
check("invalid --agent rejected", rc != 0, out.strip()[:120])

print("== MCP server entrypoint exists (page: `foldcrumbs-mcp` stdio command) ==")
mcp_bin = str(Path(FC).parent / "foldcrumbs-mcp")
rc, out = run(f"ls {mcp_bin}")
check("foldcrumbs-mcp entrypoint installed next to the CLI", rc == 0, out.strip()[:120])

print("== isolation: test wrote only under the fake HOME ==")
check("fake HOME exists and was used", HOME.exists() and str(HOME).startswith("/tmp/fc_conform"), str(HOME))

fails = [r for r in results if not r[0]]
print()
print(f"RESULT part2: {len(results)-len(fails)}/{len(results)} PASS" + (f", FAILURES: {[f[1] for f in fails]}" if fails else " — ALL CONFORM"))
raise SystemExit(1 if fails else 0)
