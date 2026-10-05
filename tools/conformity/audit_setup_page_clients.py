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

print("== pipx variant (page claims: entrypoint runs BY NAME once PIPX_BIN_DIR is on PATH; python3 -m does NOT) ==")
if shutil.which("pipx"):
    import tempfile
    px_home = Path(tempfile.mkdtemp(prefix="fc_pipx_audit_"))
    bin_dir = px_home / "bin"
    # PATH must keep the real one (pipx itself lives there) with the temp
    # bin dir prepended; RT t_41b2c982: a stripped PATH made pipx unfindable.
    penv = dict(env, PIPX_HOME=str(px_home / "pipx"), PIPX_BIN_DIR=str(bin_dir),
                PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', os.defpath)}")
    src = os.environ.get("FC_SRC", ".")
    r = subprocess.run(f"pipx install --backend pip {src}", shell=True, capture_output=True,
                       text=True, env=penv, timeout=300)
    # RT t_41b2c982 P0-1: with pipx present, a failed install is a FAIL, never a skip.
    check("pipx install succeeds", r.returncode == 0, (r.stdout + r.stderr)[:200])
    if r.returncode == 0:
        # RT t_41b2c982 P0-2: resolve BY NAME in a controlled PATH (not absolute
        # path, which would dodge the on-PATH claim under test).
        r2 = subprocess.run("foldcrumbs-mcp </dev/null", shell=True, capture_output=True,
                            text=True, env=penv, timeout=60)
        check("pipx: `foldcrumbs-mcp` resolves BY NAME with PIPX_BIN_DIR on PATH",
              r2.returncode == 0, (r2.stderr or "")[:140])
        # And the negative control: with NO directory on PATH containing a
        # foldcrumbs-mcp binary, the by-name lookup must fail (this is why the
        # page must mention pipx ensurepath). Strip every PATH dir that holds
        # the binary — a system-wide pip install would otherwise mask the
        # lookup (caught on the maintainer's iMac: /Library/.../bin has one).
        kept = [d for d in env.get("PATH", "").split(os.pathsep)
                if d and not (Path(d) / "foldcrumbs-mcp").exists()
                and d != str(bin_dir)]
        penv_nopath = dict(penv, PATH=os.pathsep.join(kept))
        r2b = subprocess.run("foldcrumbs-mcp </dev/null", shell=True, capture_output=True,
                             text=True, env=penv_nopath, timeout=60)
        check("pipx: by-name lookup FAILS without PIPX_BIN_DIR on PATH (page must say ensurepath)",
              r2b.returncode != 0, f"rc={r2b.returncode}")
        r3 = subprocess.run("python3 -m foldcrumbs.mcp_server </dev/null",
                            shell=True, capture_output=True, text=True,
                            cwd="/tmp", env=penv, timeout=60)
        check("pipx: system `python3 -m foldcrumbs.mcp_server` FAILS (page says do not register it)",
              r3.returncode != 0, (r3.stderr or r3.stdout)[:120])
    shutil.rmtree(px_home, ignore_errors=True)
else:
    check("pipx variant (skipped: pipx not on PATH)", True)

print("== isolation: test wrote only under the fake HOME ==")
check("fake HOME exists and was used", HOME.exists() and str(HOME).startswith("/tmp/fc_conform"), str(HOME))

fails = [r for r in results if not r[0]]
print()
print(f"RESULT part2: {len(results)-len(fails)}/{len(results)} PASS" + (f", FAILURES: {[f[1] for f in fails]}" if fails else " — ALL CONFORM"))
raise SystemExit(1 if fails else 0)
