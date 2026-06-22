# engram

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
```

The retrieval engine is the agent itself: it greps the folder when relevant. The LLM is used
**only** for async distillation — so recall is instant and never depends on a model being up.

Design and reuse derive from [memanto](https://github.com/moorcheh-ai/memanto) (MIT): the
typed-memory schema, trust/decay logic, extraction prompts and hook patterns — but the closed
Moorcheh engine is replaced by grep. Pure Python stdlib: hook scripts never fail on a missing
import.

## Install

```bash
python3 -m engram install          # merge hooks into ~/.claude/settings.json (global)
python3 -m engram install --local  # or project .claude/settings.json
```
The installer is merge-safe and idempotent: it appends its own hook groups and leaves existing
hooks (GSD, graphify, …) untouched. A `.json.engram-bak` backup is written first.

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
python3 -m engram distill transcript.txt   # uses the LLM
```

## Local LLM (MLX)

```bash
mlx_vlm.server --model <gemma-4-26b-a4b-mlx-repo> --port 8081
```
Distillation is async, so a cold model load is invisible to the editor. Recall needs no model.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Roadmap

- **Phase 2** — connect Codex (native hooks + MCP) and OpenCode (plugin) to the same memory via a
  minimal MCP server.
- **Phase 3** — embeddings + open vector DB only if scale outgrows grep; document ingest via OCR.
