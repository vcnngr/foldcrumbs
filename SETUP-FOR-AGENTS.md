# foldcrumbs setup — instructions for AI agents

This page is meant to be read and executed by an AI client (the way
Walrus-style `curl …/skills/setup` pages are). If you are a human, the
[README](README.md) is the better starting point.

Fetch it from anywhere with:

```
curl -sL https://raw.githubusercontent.com/vcnngr/foldcrumbs/main/SETUP-FOR-AGENTS.md
```

## Agent goal

Install foldcrumbs, wire it into the user's AI client, and verify that
remember/recall work. Stop at the first real blocker and report the exact
failure. Keep the final status short.

There is no account, no API key, no signup: foldcrumbs is a Python package
and the memories are markdown files on the user's disk. The core and the
default setup send nothing anywhere (recall is local and offline). The only
optional exceptions are features the user explicitly configures later, not
part of setup: the `openai` distillation backend pointed at a remote
endpoint, and an explicit remote `FOLDCRUMBS_EMBEDDING_ENDPOINT` for
semantic recall — both send text off-machine by the user's own choice. Do not ask the user for credentials — there are
none.

## Setup rules

1. Identify the AI client first: Claude Code, Codex, OpenCode, Pi, Hermes,
   or another MCP-capable client. The wiring command differs per client.
2. Check Python: `python3 --version` must be ≥ 3.10. If a PEP 668
   externally-managed environment blocks `pip install`, use
   `pipx install foldcrumbs` or a venv — do not use `--break-system-packages`.
   After a pipx install, `foldcrumbs`/`foldcrumbs-mcp` resolve by name only
   if pipx's bin dir is on PATH: run `pipx ensurepath` (and open a NEW
   shell) when the command is not found; until then use the absolute path
   pipx printed (typically `~/.local/bin/foldcrumbs`).
3. Install: `pip install foldcrumbs` (or `pipx`). The core is stdlib-only;
   the optional `[semantic]` extra (local ONNX embedding model support) is
   NOT needed for setup and must not be installed unless the user asks for
   semantic recall.
4. Wire the client (merge-safe, idempotent, writes a `.foldcrumbs-bak`
   backup first):
   - Claude Code, global: `foldcrumbs install`
   - Claude Code, project only: `foldcrumbs install --local`
   - Codex: `foldcrumbs install --agent codex` — heads-up before you run it:
     besides `~/.codex/hooks.json`, the installer AUTOMATICALLY merges an
     `[mcp_servers.foldcrumbs]` stanza into `~/.codex/config.toml`
     (merge-safe; existing keys preserved; a `.foldcrumbs-bak` backup is
     written when the file already exists). Tell the user this will happen
     before running it; never hand-edit `config.toml` yourself afterwards.
   - OpenCode: `foldcrumbs install --agent opencode`
   - Pi: `foldcrumbs install --agent pi`
   - Hermes: no hooks exist; use the profile mechanism instead:
     `foldcrumbs profile import --agent hermes --apply`, then
     `foldcrumbs profile env <name>` prints the single env line the user
     must set for that agent.
   - Other MCP clients: register the stdio command `foldcrumbs-mcp` —
     stdlib-only, no MCP SDK dependency. It exposes 12 tools (remember,
     recall, fetch, timeline, answer, forget, supersede, graph_path,
     relate, ingest, adopt, outcome). Do NOT register `python3 -m foldcrumbs.mcp_server`:
     that form only works with the exact interpreter foldcrumbs was
     installed into, and fails outright after a pipx install (the module is
     not visible to system python).
5. On a TTY the installer asks how to run *distillation* (recall never uses
   an LLM). If you are non-interactive, pass `--backend claude-cli` (Claude
   subscription), `--backend codex`, `--backend openai`, or
   `--backend none`, or `--no-backend-prompt`. Ask the user which backend
   they prefer — do not guess silently; the choice is saved per-machine and
   changeable later with `foldcrumbs backend <name>`.
6. Verify the round-trip. `status` and `recall` are read-only; the
   `remember` step WRITES one small test memory (that is the point of the
   test — say so if the user is watching):
   - `foldcrumbs status` — shows where the store lives and its counts
   - `foldcrumbs remember "setup verified by <client> on <date>" --type event`
   - `foldcrumbs recall "setup verified"` — must return the memory just written
   - optional cleanup of the test memory: `foldcrumbs forget <file>`
7. Tell the user to restart open agent sessions so hooks/commands load.
8. Never print or exfiltrate store contents during setup; the memories may
   contain project secrets the user recorded.
9. If a step fails, stop and report the exact command + error. Do not
   retry blindly, do not fall back to editing the user's config files by
   hand unless the installer itself instructs it.

## Optional next steps (only if the user asks)

- Semantic recall (paraphrase matching): `pip install 'foldcrumbs[semantic]'`
  then `foldcrumbs embeddings setup` (downloads a ~23 MB pinned ONNX model,
  sha256-verified). Check `foldcrumbs embeddings status`.
- Several CLI instances sharing one project: `foldcrumbs roots add <path>`
  (federation, read-only across instances).
- Deeper integration contract for coding agents: [AGENTS.md](AGENTS.md) —
  the retrieval loop, visibility rules, and what the tool will not hide.
