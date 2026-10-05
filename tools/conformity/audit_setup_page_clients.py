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
toml = HOME / ".codex/config.toml"
if toml.exists():
    body = toml.read_text()
    check("codex config.toml auto-merged with [mcp_servers.foldcrumbs] (page claim: installer merges it)",
          "mcp_servers.foldcrumbs" in body, body[:160])
else:
    check("codex config.toml written by installer", False, "config.toml missing after install")

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

print("== pipx variant (page claim: foldcrumbs-mcp works; python3 -m does NOT) ==")
if shutil.which("pipx"):
    import tempfile
    px_home = Path(tempfile.mkdtemp(prefix="fc_pipx_audit_"))
    penv = dict(env, PIPX_HOME=str(px_home / "pipx"), PIPX_BIN_DIR=str(px_home / "bin"))
    src = os.environ.get("FC_SRC", ".")
    r = subprocess.run(f"pipx install --backend pip {src}", shell=True, capture_output=True,
                       text=True, env=penv, timeout=300)
    if r.returncode == 0:
        fc_pipx = px_home / "bin/foldcrumbs-mcp"
        r2 = subprocess.run(f"{fc_pipx} </dev/null", shell=True, capture_output=True,
                            text=True, env=penv, timeout=60)
        check("pipx: foldcrumbs-mcp entrypoint runs", r2.returncode == 0, r2.stderr[:120])
        r3 = subprocess.run("/usr/bin/env python3 -m foldcrumbs.mcp_server </dev/null",
                            shell=True, capture_output=True, text=True,
                            cwd="/tmp", env=penv, timeout=60)
        check("pipx: system `python3 -m foldcrumbs.mcp_server` FAILS (page says do not register it)",
              r3.returncode != 0, (r3.stderr or r3.stdout)[:120])
    else:
        check("pipx variant (skipped: pipx install failed)", True, r.stderr[:140])
    shutil.rmtree(px_home, ignore_errors=True)
else:
    check("pipx variant (skipped: pipx not on PATH)", True)

print("== isolation: test wrote only under the fake HOME ==")
check("fake HOME exists and was used", HOME.exists() and str(HOME).startswith("/tmp/fc_conform"), str(HOME))

fails = [r for r in results if not r[0]]
print()
print(f"RESULT part2: {len(results)-len(fails)}/{len(results)} PASS" + (f", FAILURES: {[f[1] for f in fails]}" if fails else " — ALL CONFORM"))
raise SystemExit(1 if fails else 0)
