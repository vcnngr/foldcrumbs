# foldcrumbs

[![tests](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml/badge.svg)](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/foldcrumbs.svg)](https://pypi.org/project/foldcrumbs/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Persistent cross-session memory for coding agents — **no Docker, no vector DB, no external service**.

`/clear` and compaction wipe Claude Code's knowledge every session. foldcrumbs keeps a small
folder of typed memory files so the agent reopens already knowing your decisions, conventions
and codebase facts. It also fights context rot: around 45% context it checkpoints memory in the
background and nudges you to `/compact` or `/clear` — nothing is lost.

## How it works

```
STORE     markdown files + MEMORY.md index in
          ~/.claude/projects/<project>/memory/
RECALL    Claude Code's own Grep/Read (no LLM, no vector DB)
          + SessionStart injects the index
DISTILL   async, local LLM only (MLX/Ollama/OpenRouter via env)
          at ~45% context and at session end → gated, dedup'd
ANTI-ROT  PostToolUse monitor → checkpoint + reminder (no forced compaction)
          PostCompact → re-inject index after compaction
HANDOFF   each checkpoint also writes a live working-state snapshot, re-injected
          at SessionStart → resume the exact task after a /clear
```

The retrieval engine is the agent itself: it greps the folder when relevant. The LLM is used
**only** for async distillation — so recall is instant and never depends on a model being up.

Distillation also runs a **contradiction pass**: when a new memory covers the same subject as
an old one (a reversed decision, a "deferred" thing that has since happened), the LLM is asked
whether the new one makes the old obsolete — if yes, the old memory is marked superseded (file
kept on disk, out of the index; `prune` clears it). Dedup alone can't catch this: it only merges
near-identical text. Disable with `FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`; with no LLM nothing changes.

Pure Python stdlib: hook scripts never fail on a missing import.

The `MEMORY.md` index is written in a **deterministic order** (by immutable
creation time, newest first within each type), so a trust bump, re-touch or
re-distillation never reshuffles existing entries. Only adding or removing a
memory changes the file. This keeps the SessionStart-injected prefix identical
across sessions — so it rides the agent's own prompt cache instead of busting it
— and keeps the file diff-clean for sync tools like Syncthing.

## What's different from memanto

foldcrumbs started from ideas in [memanto](https://github.com/moorcheh-ai/memanto), but takes a
deliberately different shape:

| | memanto | foldcrumbs |
|--|--|--|
| Retrieval | Moorcheh engine (closed) | the agent's own grep — no engine |
| Footprint | Docker + engine + LLM + REST API | a folder + hooks |
| LLM | required for retrieval & answers | async distillation only; recall never needs it |
| Anti-rot | — | context monitor + checkpoint near 45% |
| Deps | service stack | zero runtime deps (stdlib) |
| Scope | tool-agnostic service | per-project memory, agent-side |

The original work here is the architecture: grep-based recall, the file store + index, the
anti-rot monitor, the merge-safe installer, the hooks and CLI. See **Credits** for the parts
adapted from memanto.

## Install

```bash
pip install foldcrumbs                  # from PyPI (or: pip install -e . from a checkout)
```

Then wire it into your agent:

```bash
foldcrumbs install                      # Claude Code, global (~/.claude/settings.json)
foldcrumbs install --local              # Claude Code, project (.claude/settings.json)
foldcrumbs install --agent codex        # Codex: hooks.json + prints the config.toml MCP snippet
foldcrumbs install --agent opencode     # OpenCode: opencode.json MCP + plugin + AGENTS.md block
```
The installer is merge-safe and idempotent: it appends its own hook groups and leaves existing
hooks (GSD, graphify, …) untouched. A `.foldcrumbs-bak` backup is written first.
Hook and MCP commands use a self-contained runtime snapshot under `~/.foldcrumbs/runtime`, so
editable checkouts can live in macOS-protected folders such as `~/Documents` without breaking
agent subprocesses.

On a TTY, install asks **how to distill** (recall never uses an LLM):

```
1) claude-cli   Claude subscription — `claude -p`, no API key
2) codex        Codex subscription — `codex exec`, no API key
3) openai       OpenAI-compatible HTTP endpoint (local server or remote gateway)
4) none         no LLM — keyword heuristic only (last resort)
```

The choice is saved per-machine in `~/.foldcrumbs` (not synced), so a shared store can have one
indexer with a local model and others using their own CLI subscription. Skip the prompt with
`foldcrumbs install --backend codex` (or `--no-backend-prompt`), and change it anytime with
`foldcrumbs backend <name>` (`foldcrumbs backend` alone shows the current one).

All agents share **one** memory store per project, so a decision recorded in Claude Code is
recalled in Codex and OpenCode.

## Configure (env)

| var | default | meaning |
|-----|---------|---------|
| `FOLDCRUMBS_LLM_ENDPOINT` | `http://localhost:8081` | OpenAI-compatible endpoint (MLX server) |
| `FOLDCRUMBS_LLM_MODEL` | `gemma-4-26b-a4b` | model name |
| `FOLDCRUMBS_LLM_API_KEY` | – | optional bearer token |
| `FOLDCRUMBS_CONTEXT_BUDGET` | `200000` | context window size (tokens) for the monitor |
| `FOLDCRUMBS_CONTEXT_PCT` | `0.45` | fraction at which to checkpoint + nudge |
| `FOLDCRUMBS_MIN_CONFIDENCE` | `0.7` | write gate floor |
| `FOLDCRUMBS_NO_AUTO_SUPERSEDE` | – | set to disable the contradiction pass at distill time |
| `FOLDCRUMBS_DIR` | derived from cwd | override the memory directory |

Swap the LLM for a remote gateway or OpenRouter by changing `FOLDCRUMBS_LLM_ENDPOINT` — recall is
unaffected.

## CLI

```bash
python3 -m foldcrumbs status
python3 -m foldcrumbs remember "Recall is grep, no vector DB" --type decision --tag arch
python3 -m foldcrumbs recall "vector db" --type decision --tag arch   # filters, repeatable
python3 -m foldcrumbs index
python3 -m foldcrumbs distill transcript.txt    # distil durable memories (LLM)
python3 -m foldcrumbs checkpoint transcript.txt # write a resume handoff (LLM)
python3 -m foldcrumbs handoff                   # print the current handoff
python3 -m foldcrumbs answer "how does recall work?"
python3 -m foldcrumbs forget fact_wrong.md --apply   # soft-delete (--hard removes the file)
python3 -m foldcrumbs supersede decision_old.md --by decision_new.md
python3 -m foldcrumbs import --from ~/.claude/projects/<slug>/memory --apply
```

## Curating the store

Every memory has a status: **active** → (**superseded** | **deleted**) → *file removed*.
Only active memories appear in `MEMORY.md` and recall. Non-active files stay on disk —
auditable and recoverable — until `foldcrumbs prune --apply` removes them for real.

Three ways a memory stops being true:

**You say it's wrong — `forget`.** Takes the exact filename shown in `MEMORY.md`
(or in a recall result). Dry-run by default, like `prune`:

```bash
foldcrumbs forget fact_wrong.md                 # dry-run: shows what would happen
foldcrumbs forget fact_wrong.md --apply         # marks status: deleted, file kept
foldcrumbs forget fact_wrong.md --apply --hard  # unlinks the file immediately
foldcrumbs forget "wrong deploy"                # not a filename → lists candidate files
```

MCP agents get the same via the `forget` tool (soft-delete only).

**Something replaced it — `supersede`.** You point at both sides; the old memory
keeps a `superseded_by` link to the new one and its confidence collapses to 0:

```bash
foldcrumbs supersede decision_pypi_deferred.md --by fact_published_to_pypi.md
```

**Distillation notices on its own — the contradiction pass.** Dedup only merges
*near-identical* text; a reversed decision reads completely differently. So at
distill time, when a new memory covers the same subject as an old one (crude
word-stem overlap picks candidates), the LLM is asked one question: *does the new
memory make the old one obsolete?* Only an explicit yes supersedes anything.
Example: an old decision "PyPI publishing is deferred" is auto-superseded when a
new fact "published to PyPI" is distilled. Fail-soft (no LLM → nothing changes);
disable with `FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`. Superseded events are logged to
`~/.foldcrumbs/foldcrumbs.log`.

## Sharing memory between stores: `import`

Stores are namespaced **per instance × per project**: memory lives in
`<config-dir>/projects/<encoded-cwd>/memory/`, where `<config-dir>` honours
`CLAUDE_CONFIG_DIR`. Run several instances (e.g. `~/.claude`, `~/.claude-work`) and
it is *structural* that one store ends up rich while another starts empty for the
same project. `import` closes that gap.

The two sides of the command:

- **target** (written to) — the store of the instance *running the command*, i.e.
  your `CLAUDE_CONFIG_DIR` (default `~/.claude`) + the directory you run it from;
- **source** (`--from`) — any path: a memory dir directly, or a project dir
  resolved through the same convention.

```bash
# fill the work instance's store from the main one (run from the project dir):
CLAUDE_CONFIG_DIR=~/.claude-work foldcrumbs import \
  --from ~/.claude/projects/<slug>/memory --apply

# promote what the work instance learned back into main:
foldcrumbs import --from ~/.claude-work/projects/<slug>/memory --apply
```

What it does — and deliberately doesn't do:

| | |
|--|--|
| record-level merge | each memory goes through `upsert`: new → created, near-duplicate → **validates** the existing one (trust bump, no doubles) |
| skips noise | `MEMORY.md`, `HANDOFF*`, files without frontmatter, superseded/deleted records — dead history stays where it is |
| dry-run first | default shows the `{created, validated, skipped}` plan; `--apply` writes and rebuilds the index |
| idempotent | re-running only validates — safe to use as a periodic manual sync |
| one-way | bidirectional = run it twice, once per direction |
| no LLM | the contradiction pass does **not** run on import (predictability); an imported memory that contradicts a local one coexists until a distill reviews it or you `supersede` by hand |

Contrast with `migrate --from`, which is a raw file copy for one-time moves.
If the *main* store is synced across machines (e.g. Syncthing), a natural pattern
is hub-and-spoke: import into main from one machine only, refresh the per-machine
instances from main.

## Surviving `/clear` and `/compact`

Two layers cross the context switch:

- **Durable memories** (decisions, rules, preferences, facts) — always re-injected via
  the `MEMORY.md` index at SessionStart / PostCompact.
- **Working-state handoff** — a single overwritten snapshot of the *current* task, files
  in flight and next steps, written at each checkpoint and re-injected so you resume the
  exact task after a hard `/clear`.

At ~45% context foldcrumbs nudges you; pick `/compact` (keep working) or `/clear` (fresh start) —
either way the next turn is re-primed. Force a snapshot anytime with `foldcrumbs checkpoint`.

## Local LLM

Distillation needs any OpenAI-compatible chat endpoint — point `FOLDCRUMBS_LLM_ENDPOINT`
at whatever you run. It's used only for async distillation, so a cold model load is
invisible to the editor, and **recall needs no model at all**.

Common local servers (all expose `/v1/chat/completions`):

```bash
# MLX — Apple Silicon only, fastest on Mac
mlx_lm.server  --model <gemma-mlx-repo> --port 8081     # or mlx_vlm.server for VLMs

# Ollama — cross-platform (macOS / Linux / Windows)
ollama serve                                            # endpoint :11434/v1

# llama.cpp / LM Studio / vLLM — also OpenAI-compatible
```

Then e.g. `export FOLDCRUMBS_LLM_ENDPOINT=http://localhost:11434 FOLDCRUMBS_LLM_MODEL=qwen2.5`.
A remote gateway or OpenRouter works the same way — only the env var changes.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## MCP server

foldcrumbs ships a minimal MCP server (stdio, stdlib only — no `mcp` SDK dependency) exposing
`remember`, `recall`, `answer` and `forget` to any MCP client:

```bash
foldcrumbs-mcp            # or: python3 -m foldcrumbs.mcp_server
```
Codex and OpenCode are wired to it by `foldcrumbs install --agent …`. Use it directly from any
MCP-speaking tool by registering the command above.

## How each agent is wired

| Agent | Inject at start | Capture | Notes |
|-------|-----------------|---------|-------|
| Claude Code | SessionStart hook | PostToolUse monitor + SessionEnd | full lifecycle hooks |
| Codex | SessionStart hook (`additionalContext`) | Stop + PostToolUse hooks | same scripts; + MCP for in-session tool calls |
| OpenCode | AGENTS.md → agent calls `recall` (MCP) | plugin `session.idle`/`session.compacted` | no inject-capable hook, so prompt-driven recall |

## Roadmap

- **Phase 1 ✓** — Claude Code: file store, grep recall, distillation, anti-rot.
- **Phase 2 ✓** — Codex + OpenCode on the same store via a stdlib MCP server + installers.
- **Phase 3** — embeddings + open vector DB only if scale outgrows grep; document ingest via OCR.

Release history: [CHANGELOG.md](CHANGELOG.md).

## Credits

foldcrumbs adapts a few utilities from [memanto](https://github.com/moorcheh-ai/memanto)
(MIT, © Moorcheh / Edge AI Innovations): the typed-memory categories and confidence/decay
model, the session-distillation approach, the transcript-reading helper, and the context-block
rendering idea. These are reimplemented here against a file store; the Moorcheh retrieval engine
is not used. Full notice in [LICENSE](LICENSE). Thanks to the memanto authors for releasing it
under MIT.

## License

MIT — see [LICENSE](LICENSE).
