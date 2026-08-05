# foldcrumbs

[![tests](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml/badge.svg)](https://github.com/vcnngr/foldcrumbs/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/foldcrumbs.svg)](https://pypi.org/project/foldcrumbs/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** · [Italiano](README.it.md) · [中文](README.zh.md)

Persistent cross-session memory for coding agents — **no Docker, no vector DB, no external service**.

`/clear` and compaction wipe Claude Code's knowledge every session. foldcrumbs keeps a small
folder of typed memory files so the agent reopens already knowing your decisions, conventions
and codebase facts. It also fights context rot: around 45% context it checkpoints memory in the
background and nudges you to `/compact` or `/clear` — nothing is lost.

Several CLI instances on one project (`claude`, `claude-work`, …) keep their own
stores but see each other's memory, read-only — see
[Federation](#several-instances-one-project-federation).

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
FEDERATE  every registered instance publishes an index shard; each session also
          sees the others' memory, read-only, paths announced for grep
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

## Quick start

Thirty seconds from zero to a working store:

```bash
pip install foldcrumbs
cd your-project
foldcrumbs install          # wires Claude Code hooks + slash commands
```

That's it. The next Claude Code session starts with an empty but live store,
and memories begin accumulating as you work. Verify with `foldcrumbs status`.

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

For Claude Code the installer also writes four slash commands — **`/remember`**,
**`/recall`**, **`/forget`**, **`/foldcrumbs`** (dashboard) — so memory becomes an in-session capability,
not just a background layer. `/remember` with no arguments distills durable memories from
the live conversation (with confirmation) using the session's own model — no LLM backend
needed. The files are marked as managed: edit one and remove the marker line to take
ownership; `uninstall` removes only ours. Restart open sessions to pick them up.
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
| `FOLDCRUMBS_LLM_MODEL` | `gemma-4-26b-a4b-it` | model name |
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
python3 -m foldcrumbs decay                          # archive low-trust memories (dry-run; --apply writes)
python3 -m foldcrumbs restore fact_old.md            # bring an archived memory back
python3 -m foldcrumbs import --from ~/.claude/projects/<slug>/memory --apply

python3 -m foldcrumbs profile list                   # every registered profile
python3 -m foldcrumbs profile add kimi --kind dedicated
python3 -m foldcrumbs profile env kimi               # the one env line that selects it
```

`decay` archives — it never deletes. A memory whose trust has fallen below the
threshold (0.3) **and** has gone 30 days without being touched is moved to
`status: archived`; it leaves the index and recall but stays on disk. `restore
<name>` brings it back whole, and `prune --apply` is still the separate,
explicit act that removes files for good. Dry-run by default.

### Profiles — one store per agent

A **profile** is a registered memory root with a name and a shape:

- **dedicated** — one memory directory shared by every project; what a
  long-running agent (a CI bot, a review agent) wants;
- **shared** — one memory directory *per project* under a config dir; how an
  interactive assistant like Claude Code works (honours `CLAUDE_CONFIG_DIR`).

```bash
foldcrumbs profile add kimi-review --kind dedicated            # one dir, all projects
foldcrumbs profile add work   --kind shared --path ~/.claude-work
foldcrumbs profile env kimi-review
# → export FOLDCRUMBS_DIR=/Users/you/.foldcrumbs/profiles/kimi-review
```

There is no `profile use`. Which store a process reads is decided by its
environment **before it starts** — a CLI cannot reach back into the shell that
launched it. So `profile env` prints the one line that does work, and you put
it wherever the agent's process is born (a shell rc file, a worker's env, a
Hermes profile's `.env`). Point a process at a dedicated profile and it gets a
read-only federated view of every shared store registered on the machine.

`profile import --agent hermes --apply` registers one profile per agent of a
multi-agent runtime, so each gets a memory of its own (dry-run by default).
`profile remove` unregisters without touching the memories.

## Curating the store

Every memory has a status: **active** → (**superseded** | **deleted** | **archived**) → *file removed*.
Only active memories appear in `MEMORY.md` and recall. Non-active files stay on disk —
auditable and recoverable (`restore` revives an archived one) — until `foldcrumbs prune --apply`
removes them for real.

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

**It fades on its own — `decay`.** A memory nobody trusts and nobody touches
is not wrong, it is just old. `foldcrumbs decay` finds active memories whose
confidence has dropped below 0.3 **and** that have gone 30 days without being
written or validated, and moves them to `status: archived`. Archived memories
leave the index, recall and the federated shards — other instances stop being
shown them — but the file stays on disk. `foldcrumbs restore <name>` brings one
back. The sweep is explicit and dry-run by default; it is never a side effect
of a recall, so reading can never silently change what the store holds.

## Several instances, one project: federation

Running `claude`, `claude-work`, `claude-peo`, … means one `CLAUDE_CONFIG_DIR`
each, so **one store each** — a decision recorded in one is invisible to the
others. Federation gives every instance a read-only view of what the others
learned about the same project, live and without duplicating anything. The
stores stay separate and separately owned: an instance only ever writes its own.

```bash
foldcrumbs install          # each instance self-registers
foldcrumbs roots            # who is federated, and where their memory lives
```

What each instance then sees at SessionStart: its own `MEMORY.md` exactly as
before, followed by a separate block listing the other instances' memory dirs
and their entries, each with an absolute path. `recall`, `answer` and the MCP
tools search across all of them, labelling results with their origin.

```
<foldcrumbs-federated>
Memory from this project's other agent instances. … READ-ONLY from here …

- claude-work: /Users/you/.claude-work/projects/<project>/memory
- claude-peo:  /Users/you/.claude-peo/projects/<project>/memory

- [claude-work] Recall is grep, no vector DB — the retrieval engine is the agent
  /Users/you/.claude-work/projects/<project>/memory/decision_recall_is_grep.md
</foldcrumbs-federated>
```

Three properties are deliberate, and each cost something to get right:

**Nothing is shared-written.** Each instance publishes an index shard of its
own under `~/.foldcrumbs/projects/<project>/roots/<root-id>.json`; readers merge
them. One shared index would have meant two instances scanning and rewriting
concurrently, and an atomic replace prevents a torn file, not a stale one.
Ordering is a total key (type, date, root id, filename) so every instance
derives the same order without a shared file to agree through.

**`MEMORY.md` is untouched.** Federation never edits it, so it stays
byte-identical while only other instances write — which is what keeps the
injected prefix riding the agent's prompt cache. The federated view is appended
after it, in the region the handoff already invalidates each session.

**Read-only is enforced, not requested.** The block tells the model those files
belong to someone else, but `write_memory`, `upsert` and `mark_superseded_on_disk`
also refuse a foreign record outright. When distillation finds a new memory that
contradicts one in another instance's store, it records the claim on its own
record and the federated view marks that entry as contested — their instance
stays the only one that can retire their file.

Leave the shared view with `foldcrumbs roots remove <id>`; the store itself is
untouched, and only an explicit `install` / `roots add` brings it back.

Limits worth knowing: federation is per machine (roots register into
`FOLDCRUMBS_STATE_DIR`, so instances pointed at different ones can't see each
other — `status` says so when it can tell); an unreachable root keeps its last
published entries, flagged, rather than appearing to have been emptied; and
**after upgrading the package, run `foldcrumbs install` again** — hooks run from
a runtime snapshot staged at install time, so an upgrade alone does not reach
them.

## Sharing memory between stores: `import`

Stores are namespaced **per instance × per project**: memory lives in
`<config-dir>/projects/<encoded-cwd>/memory/`, where `<config-dir>` honours
`CLAUDE_CONFIG_DIR`. Run several instances (e.g. `~/.claude`, `~/.claude-work`) and
it is *structural* that one store ends up rich while another starts empty for the
same project.

Two ways to close that gap, and they answer different questions. **Federation**
(above) lets an instance *see* the others' memory, live, without copying — that
is what you want most of the time. `import` **adopts** it: a decision that
matured in `claude-work` becomes genuinely yours, with a trust bump on merge,
and survives that instance going away. Federation shows; import takes ownership.

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
- **Phase 2.5 ✓** — federation: several CLI instances share a read-only view of one
  project without merging their stores.
- **Phase 2.7 ✓** — memory engineering: recall reinforcement and freshness in the
  ranking, a decay pass that archives, named profiles (one store per agent), and
  `/remember` `/recall` `/forget` `/foldcrumbs` slash commands.
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
