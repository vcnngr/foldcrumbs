# engram

[![tests](https://github.com/vcnngr/engram/actions/workflows/test.yml/badge.svg)](https://github.com/vcnngr/engram/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Persistent cross-session memory for coding agents — **no Docker, no vector DB, no external service**.

`/clear` and compaction wipe Claude Code's knowledge every session. engram keeps a small
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

Pure Python stdlib: hook scripts never fail on a missing import.

## What's different from memanto

engram started from ideas in [memanto](https://github.com/moorcheh-ai/memanto), but takes a
deliberately different shape:

| | memanto | engram |
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
engram install                      # Claude Code, global (~/.claude/settings.json)
engram install --local              # Claude Code, project (.claude/settings.json)
engram install --agent codex        # Codex: hooks.json + prints the config.toml MCP snippet
engram install --agent opencode     # OpenCode: opencode.json MCP + plugin + AGENTS.md block
```
The installer is merge-safe and idempotent: it appends its own hook groups and leaves existing
hooks (GSD, graphify, …) untouched. A `.engram-bak` backup is written first.

On a TTY, install asks **how to distill** (recall never uses an LLM):

```
1) claude-cli   Claude subscription — `claude -p`, no API key
2) codex        Codex subscription — `codex exec`, no API key
3) openai       OpenAI-compatible HTTP endpoint (local server or remote gateway)
4) none         no LLM — keyword heuristic only (last resort)
```

The choice is saved per-machine in `~/.engram` (not synced), so a shared store can have one
indexer with a local model and others using their own CLI subscription. Skip the prompt with
`engram install --backend codex` (or `--no-backend-prompt`), and change it anytime with
`engram backend <name>` (`engram backend` alone shows the current one).

All agents share **one** memory store per project, so a decision recorded in Claude Code is
recalled in Codex and OpenCode.

## Configure (env)

| var | default | meaning |
|-----|---------|---------|
| `ENGRAM_LLM_ENDPOINT` | `http://localhost:8081` | OpenAI-compatible endpoint (MLX server) |
| `ENGRAM_LLM_MODEL` | `gemma-4-26b-a4b` | model name |
| `ENGRAM_LLM_API_KEY` | – | optional bearer token |
| `ENGRAM_CONTEXT_BUDGET` | `200000` | context window size (tokens) for the monitor |
| `ENGRAM_CONTEXT_PCT` | `0.45` | fraction at which to checkpoint + nudge |
| `ENGRAM_MIN_CONFIDENCE` | `0.7` | write gate floor |
| `ENGRAM_DIR` | derived from cwd | override the memory directory |

Swap the LLM for a remote gateway or OpenRouter by changing `ENGRAM_LLM_ENDPOINT` — recall is
unaffected.

## CLI

```bash
python3 -m engram status
python3 -m engram remember "Recall is grep, no vector DB" --type decision --tag arch
python3 -m engram recall "vector db"
python3 -m engram index
python3 -m engram distill transcript.txt    # distil durable memories (LLM)
python3 -m engram checkpoint transcript.txt # write a resume handoff (LLM)
python3 -m engram handoff                   # print the current handoff
python3 -m engram answer "how does recall work?"
```

## Surviving `/clear` and `/compact`

Two layers cross the context switch:

- **Durable memories** (decisions, rules, preferences, facts) — always re-injected via
  the `MEMORY.md` index at SessionStart / PostCompact.
- **Working-state handoff** — a single overwritten snapshot of the *current* task, files
  in flight and next steps, written at each checkpoint and re-injected so you resume the
  exact task after a hard `/clear`.

At ~45% context engram nudges you; pick `/compact` (keep working) or `/clear` (fresh start) —
either way the next turn is re-primed. Force a snapshot anytime with `engram checkpoint`.

## Local LLM

Distillation needs any OpenAI-compatible chat endpoint — point `ENGRAM_LLM_ENDPOINT`
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

Then e.g. `export ENGRAM_LLM_ENDPOINT=http://localhost:11434 ENGRAM_LLM_MODEL=qwen2.5`.
A remote gateway or OpenRouter works the same way — only the env var changes.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## MCP server

engram ships a minimal MCP server (stdio, stdlib only — no `mcp` SDK dependency) exposing
`remember`, `recall` and `answer` to any MCP client:

```bash
engram-mcp            # or: python3 -m engram.mcp_server
```
Codex and OpenCode are wired to it by `engram install --agent …`. Use it directly from any
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

## Credits

engram adapts a few utilities from [memanto](https://github.com/moorcheh-ai/memanto)
(MIT, © Moorcheh / Edge AI Innovations): the typed-memory categories and confidence/decay
model, the session-distillation approach, the transcript-reading helper, and the context-block
rendering idea. These are reimplemented here against a file store; the Moorcheh retrieval engine
is not used. Full notice in [LICENSE](LICENSE). Thanks to the memanto authors for releasing it
under MIT.

## License

MIT — see [LICENSE](LICENSE).
