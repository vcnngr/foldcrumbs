# Conformity audits — SETUP-FOR-AGENTS.md

These scripts execute `SETUP-FOR-AGENTS.md` **as an AI agent would**, in an
isolated fake HOME, and check every claim/command in the page against the
real installed CLI. They are manual audit tools (like `benchmarks/quality`),
deliberately NOT named `test_*` and NOT collected by pytest/CI: they need a
pip-installed foldcrumbs in a venv and they spawn real installs.

```bash
python3 -m venv /tmp/fc_conf_venv
/tmp/fc_conf_venv/bin/pip install -q .            # or: pip install -q foldcrumbs
FC_BIN=/tmp/fc_conf_venv/bin/foldcrumbs \
PY_BIN=/tmp/fc_conf_venv/bin/python \
  python3 tools/conformity/audit_setup_page_claude.py     # 20 checks: install/idempotency/backend/round-trip/store/profile/uninstall
FC_BIN=/tmp/fc_conf_venv/bin/foldcrumbs \
  python3 tools/conformity/audit_setup_page_clients.py    # 10 checks: codex/opencode/pi wiring, invalid agent, foldcrumbs-mcp entrypoint
```

Exit 0 = every claim conforms. If you change SETUP-FOR-AGENTS.md (or the
installer), re-run both audits; a FAIL line names the page claim that the
CLI contradicts.
