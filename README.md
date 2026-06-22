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

## Credits

engram adapts a few utilities from [memanto](https://github.com/moorcheh-ai/memanto)
(MIT, © Moorcheh / Edge AI Innovations): the typed-memory categories and confidence/decay
model, the session-distillation approach, the transcript-reading helper, and the context-block
rendering idea. These are reimplemented here against a file store; the Moorcheh retrieval engine
is not used. Full notice in [LICENSE](LICENSE). Thanks to the memanto authors for releasing it
under MIT.

## License

MIT — see [LICENSE](LICENSE).
