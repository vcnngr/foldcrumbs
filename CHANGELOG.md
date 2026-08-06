# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Dashboard** (`foldcrumbs dashboard`) — one self-contained HTML page over
  the live store: inline CSS, no scripts, no `http(s)` references, opens
  offline and never phones home. Every panel is computed by the same
  functions the CLI uses — store status, what the decay sweep would archive,
  superseded chains, federated roots (shard age, entries), recall
  reinforcement, latest memories, trust histogram, anti-rot — and memory
  names link to the actual files on disk. Expiry and Conflicts panels appear
  automatically when those features have something to show. `--json` prints
  the data instead of the page; `--out` writes it to a path; `--no-open`
  skips the browser.
- **Reconciliation queue** (`foldcrumbs conflicts`) — the contradiction pass has
  three verdicts now: supersede, coexist, flag. An unsure or garbled LLM answer
  no longer collapses into a silent "no"; the pair is recorded in a
  machine-local queue and surfaced by `foldcrumbs conflicts`, alongside the
  claims this store holds on other instances' memories and the claims other
  instances hold on ours. Pairs drop out of the queue automatically once either
  side is retired. `doctor` points at a non-empty queue. Resolving is always
  explicit: the queue suggests the exact `supersede`/`forget` command and
  writes nothing itself.
- **Expiring memories** — the "true until a known date" class. `remember
  --expires <date>` stamps an `expires_at` on a memory (`2026-09-01`,
  `2026-09-01T12:00`, or relative `30d`/`2w`/`6m`; a bare date means the end
  of that day). Past it, the memory is invisible everywhere an archived one
  is — index, recall, federation, dedup, the contradiction pass — while the
  file stays untouched on disk. The `decay` sweep then archives lapsed
  memories (labelled `(expired)`), `status` reports what has lapsed and what
  expires next, and removing or moving the date is the explicit revival.
  Expiry is only ever set by user intent: distillation never guesses a date.
- Optional semantic recall (`FOLDCRUMBS_SEMANTIC=1`, off by default). With
  the switch on, `recall`/`answer` and the MCP tools also score candidates by
  embedding similarity against an OpenAI-compatible `/v1/embeddings` endpoint
  (`FOLDCRUMBS_EMBEDDING_ENDPOINT`, default: the distillation endpoint) and
  keep the better of the two relevance signals. The semantic signal is capped
  below a perfect word match, so it rescues paraphrases the lexical pass
  misses but can never outrank what the words already matched exactly.
- Two gates, both the user's, neither blocking: without `FOLDCRUMBS_SEMANTIC`
  nothing is ever attempted (zero requests, zero latency, behaviour identical
  to before); with it on, an endpoint that is missing, slow or erroring is a
  silent fallback to lexical recall (`FOLDCRUMBS_EMBEDDING_TIMEOUT`, default
  10 s). No new dependencies — the request is stdlib `urllib`; vectors are
  cached machine-locally in the state dir (never in the store, which may sync
  across machines with different endpoints) and survive endpoint/model
  changes by cache key.
- `foldcrumbs status` reports the semantic channel (on/off, endpoint, model,
  cache size).

## [0.6.1] — 2026-08-05

Hardening of the federation released in 0.6.0. No new features: every change
here closes a way the federated view could lose a memory, hide one, hang a
recall, or write outside the state directory.

### Fixed

- **Arbitrary file deletion through `forget --hard`.** A memory name was joined
  onto the store directory without a containment check, so an absolute name
  replaced the directory outright and `../` walked out of it. Every
  filename-addressed operation now resolves inside the store or refuses.
- **A departure could write outside the configured state directory.** The
  registry path comes from the root's marker — a hand-editable file — and
  taking a lock is itself a write, so a crafted marker had foldcrumbs create a
  directory, a lock and a tombstone anywhere and unlink a file there. The
  registration being withdrawn is now verified before the lock is taken, and
  again under it.
- **Paths compared by spelling.** A state directory reached through a symlink,
  a bind mount, or an alternate spelling read as a *different* registry, which
  in turn emptied federation, deleted freshly published shards, and reported a
  split that did not exist. Registry and directory identity now come from the
  filesystem, and `status` distinguishes a confirmed split from one it could
  not reach.
- **A moved root stayed invisible.** Publication skipped writing whenever the
  entries matched, so a root whose layout changed without its memories
  changing never refreshed the directory its shard names — and readers, which
  refuse a shard describing a layout the root has left, hid it indefinitely.
- **Legacy handoffs and Syncthing conflict copies read as memories.** They
  parsed as an "Untitled" record carrying the whole file, and federation then
  showed them to every other instance.
- **Superseded and contested records could still be recalled.** A foreign
  memory this store has declared obsolete is left out of federated search
  unless explicitly asked for.
- **Recall could hang or leak threads.** Foreign scans, availability probes and
  marker reads are each bounded *and* gated: one blocked worker per root
  rather than one per call, and an answer that arrives late is still used.
- **The local store was read twice per federated recall.**
- **Stale shards.** A mode change now drops the shards it invalidates across
  every project, a departure takes its project shards with it, and neither
  deletes a shard it cannot prove is stale.
- CI lint pinned to a declared rule set. `ruff check` was inheriting whatever
  the newest release considered default, so 0.16 turned `main` red on code
  that had not changed — v0.5.0 fails it too. The rules the project actually
  enforces are now named in `pyproject.toml`.

### Known limits

- Federation remains machine-local: foreign stores are read from the local
  filesystem, so instances on different hosts do not see each other.
- Duplicate memories across federated instances are left in place. A read-only
  foreign store cannot be deduplicated by the instance reading it.

## [0.6.0] — 2026-08-04

The "federation" release: several CLI instances on the same project stop being
blind to each other, **without** merging their stores.

Running `claude`, `claude-work`, `claude-peo`, … means one `CLAUDE_CONFIG_DIR`
each, so one store each — a decision recorded in one was invisible to the
others, and `import` could only close the gap by copying. Federation gives
every instance a read-only view of what the others learned about the same
project, live, with nothing duplicated. Each store stays separately owned: an
instance only ever writes its own.

### Added

- **Root registry.** `foldcrumbs roots list|add|remove`; instances self-register
  on `install`. Each root carries a stable id in a `.foldcrumbs-root` marker
  *inside* the root, so it survives moving or renaming the config dir. Registry
  entries are one file per root under `<state-dir>/roots/` — never a shared
  manifest, which would put every instance on the same write path.
- **Per-root index shards** under `<state-dir>/projects/<project>/roots/`,
  published on every index rebuild, and on the first federated read so an
  instance that federates an existing store is not advertised as empty.
  Writes hold a lock scoped to that shard across the scan, so there is no
  window between reading the store and publishing it in which another process
  could publish something fresher — and no instance waits on another's scan.
  Sharding removes that race between instances; one instance still runs
  several processes. Every lock wait is bounded: a hook declines to publish
  rather than delay a session start. Merged at read time with a total ordering
  key (type, `created_at` descending, root id, filename), so every instance
  derives the same order without a shared file to agree through.
- **Federated block** injected after the local index at SessionStart *and*
  PostCompact, listing each instance's memory dir and every entry's absolute
  path — the agent's grep is the recall engine, so it has to be able to reach
  them. Capped by count and by size, and it says what it left out.
- **Federated recall.** `search()` scores the other instances' stores too, so
  `recall`/`answer` and the MCP tools see the whole project. This is what makes
  federation visible to OpenCode, which recalls only through MCP. Results are
  labelled with their origin and marked read-only.
- **External supersession claims.** When distillation finds a new memory that
  obsoletes one in *another* instance's store, it records the claim on its own
  record (`supersedes_external`) instead of editing a file it does not own; the
  federated view then marks that entry as contested.
- `foldcrumbs status` reports federated roots and warns about a split
  `FOLDCRUMBS_STATE_DIR` or a `FOLDCRUMBS_DIR` that disagrees with how the root
  is registered.

### Changed

- Cross-root writes are refused in code (`ForeignMemoryError`), not merely
  discouraged in the prompt: `write_memory`, `upsert` and
  `mark_superseded_on_disk` reject a record belonging to another instance.
- `MEMORY.md` is deliberately untouched by federation. It stays byte-identical
  while only other instances write, so the SessionStart-injected prefix keeps
  riding the agent's prompt cache; the federated view is a separate block.
- `claude_config_dir()`, `memory_dir()` and the state dir are now normalised to
  absolute paths. Shard entries are read by other instances from *their* cwd, so
  a relative override would have resolved somewhere else entirely.

### Fixed

- A timestamp without a timezone raised `TypeError` in the index sort the
  moment two records were compared — reachable before this release by importing
  a hand-written file. Naive timestamps are now read as UTC.
- A record whose `created_at` is missing *or* unparseable had one invented on
  every parse, so any ordering built on it was irreproducible. Such records are
  now flagged and pinned to their file's mtime in the federated view.
- **Path escape in filename-addressed operations.** `get`, `forget` and
  `supersede` joined the given name onto the memory dir without checking
  containment, so an absolute path or one containing `..` resolved outside the
  store — and since any text parses into an "Untitled" record, `forget --hard`
  would then unlink that file. Predates federation. Names are now resolved
  through a containment check.
- **Test isolation.** The suite pointed only some of the store-locating
  variables at temp dirs, and only the legacy `ENGRAM_*` spellings in places —
  which the `FOLDCRUMBS_*` ones outrank. A developer with either exported had
  their real backend choice overwritten and a runtime snapshot staged beside
  it by `TestBackendConfig`, and, once recall became federated, their actual
  memories read. Every such name is now redirected to a throwaway sandbox
  before the package is imported (clearing them is not enough: with nothing
  set, the state dir falls back to the real `~/.foldcrumbs`). The sandbox is
  shared by every test module, so running one on its own is covered too, and
  each module asserts it cannot resolve to a real store.

- Dated handoffs from older versions, and the `sync-conflict` copies Syncthing
  leaves in a shared store, were read as memories: each parses as an
  "Untitled" record holding the whole file. They were skipped on `import` but
  not by recall or the index, so federation would have shown them to every
  other instance. Found in a real store during rollout.

### Known limits

- Federation is per machine: roots register into `FOLDCRUMBS_STATE_DIR`
  (default `~/.foldcrumbs`), and instances pointed at different ones form
  disjoint groups. `status` says so when it can detect it.
- Upgrading the package does not re-register anything, and hooks run from the
  runtime snapshot staged at install time — **run `foldcrumbs install` again
  after upgrading**.
- A process killed while holding the registry lock leaves it behind on
  platforms without `fcntl`; the log names the directory to remove. Stale locks
  are never broken automatically, because age cannot distinguish a dead holder
  from a slow one and stealing loses data.
- `created_at` is not backfilled into files that lack it; the mtime fallback
  moves if the file is touched.
- Root ids are 64 random bits with no collision retry — ample for a handful of
  local roots, not a global namespace.

## [0.5.0] — 2026-07-21

The "active surface" release: memory stops being only a background layer and
becomes an in-session capability on every supported agent. Hardened by five
rounds of Codex review (12 findings, all fixed).

### Added
- The same `/remember`, `/recall`, `/forget`, `/foldcrumbs` surface for the other
  agents: Codex gets managed custom prompts in `~/.codex/prompts/`, OpenCode
  gets `command` entries merged into `opencode.json` (user-defined commands
  with the same name are never overwritten).
- Claude Code MCP registration — `foldcrumbs install` registers the
  `foldcrumbs-mcp` server via `claude mcp add` (user scope; project scope with
  `--local`), so `remember`/`recall`/`answer`/`forget` become real tools in
  Claude Code too. Falls back to printing a `.mcp.json` snippet when the CLI
  is unavailable; `uninstall` removes the registration.
- Claude Code skill — `foldcrumbs install` writes a managed
  `skills/foldcrumbs/SKILL.md` so the model activates memory on natural
  triggers ("remember that…", "what did we decide about…", corrections of
  stored facts) without an explicit slash command.
- Slash commands for Claude Code — `foldcrumbs install` now also writes
  `/remember`, `/recall`, `/forget` and `/foldcrumbs` (dashboard) to `<config-dir>/commands/`
  (managed files: user-edited copies are never touched; `uninstall` removes
  only ours). `/remember` with no arguments distills durable memories from the
  live conversation with user confirmation — in-context distillation, no LLM
  backend required.
- CI publishes to PyPI automatically when a GitHub release is created
  (`publish.yml`, `PYPI_API_TOKEN` repo secret).

### Fixed
- Dashboard command renamed `/memory` → `/foldcrumbs`: Claude Code reserves
  `/memory` for its built-in editor, which shadowed ours entirely.
- Reinstall refreshes marked OpenCode command entries when templates change
  (user commands still skipped), and repairs a Claude MCP registration
  shadowed by another scope (failed `mcp add` → remove + retry in the
  requested scope).
- OpenCode uninstall identifies our command entries by an explicit ownership
  marker in the template — a user command that merely mentions foldcrumbs is
  never removed.
- Slash commands allow the `Read` tool, so `/recall` with no query can
  actually read `MEMORY.md`.
- Claude MCP registration verifies command/args as well as scope: a stale
  registration (old interpreter or runtime path) is replaced on reinstall.
- Codex prompts are written under `$CODEX_HOME/prompts` when a custom
  `CODEX_HOME` is set (previously always `~/.codex/prompts`).
- `uninstall --agent opencode` removes the foldcrumbs command entries from
  `opencode.json` (user commands with the same name are kept).
- Command frontmatter emits quoted YAML scalars — the dashboard command's description
  contained `: ` and produced invalid frontmatter (found by Codex review).
- Codex prompts are documented under their real invocation names
  (`/prompts:remember` etc. — Codex namespaces `~/.codex/prompts` files).
- Claude MCP registration is scope-aware on both ends: `install --local` now
  registers the project scope even when a user-scoped entry exists, and
  `uninstall --local` removes the project-scoped entry it installed.

### Changed
- README: new "Curating the store" section (memory lifecycle: active →
  superseded/deleted → pruned; forget / supersede / contradiction pass) and
  "Sharing memory between stores" section (how `import` resolves target vs
  source, multi-instance examples, semantics table).

## [0.4.0] — 2026-07-13

Memory that curates itself: this release closes the lifecycle loop — memories
can now be forgotten, superseded, and merged across stores.

### Added
- **Contradiction pass (auto-supersede)** — at distill time, when a new memory
  covers the same subject as an old one (a reversed decision, a "deferred"
  thing that has since happened), the LLM is asked whether the new one makes
  the old obsolete; if yes, the old memory is marked superseded (file kept on
  disk, out of the index). Fail-soft with no LLM; disable with
  `FOLDCRUMBS_NO_AUTO_SUPERSEDE=1`. (#12)
- **`foldcrumbs forget <file>`** — soft-delete a memory (dry-run by default;
  `--hard` unlinks the file). A query argument lists candidate filenames.
  Also exposed as a `forget` MCP tool (soft-delete only). (#11)
- **`foldcrumbs supersede <old> --by <new>`** — explicitly mark one memory as
  replaced by another (`superseded_by` link, confidence collapses to 0). (#11)
- **`foldcrumbs import --from <dir>`** — record-level, dedup-aware merge from
  another store: near-duplicates validate the existing memory instead of
  doubling it; index/handoff files, non-frontmatter files and non-active
  records are skipped. Dry-run by default, idempotent. (#13)
- `recall --type` / `--tag` filters (repeatable). (#10)
- CI lint job (`ruff check`) alongside the 3.10/3.12/3.13 test matrix. (#9)

### Changed
- Search tokenizes Unicode word characters, so accented queries ("città")
  match. (#10)
- MCP `serverInfo.version` tracks the package version instead of a hardcoded
  literal. (#10)
- README: `pip install foldcrumbs` install step + PyPI badge. (#10)

### Fixed
- Agent subprocesses no longer depend on source-checkout location: hooks and
  the MCP server run from a staged runtime under `~/.foldcrumbs/runtime`, so
  editable checkouts can live in macOS-protected folders such as
  `~/Documents`. (#8)

## [0.3.0] — 2026-07-06

### Changed
- **Project renamed engram → foldcrumbs** (package, CLI, brand) with a
  non-destructive migration path: `foldcrumbs migrate` copies `~/.engram` →
  `~/.foldcrumbs`, env vars are read as `FOLDCRUMBS_*` with a legacy
  `ENGRAM_*` fallback, and the GitHub repo redirect is preserved.
- First release published to PyPI (`pip install foldcrumbs`).

## 0.2.0 — 2026-06 (unreleased tag, engram era)

### Added
- Deterministic `MEMORY.md` ordering (immutable creation time, newest first
  within each type) so the SessionStart-injected prefix rides the agent's
  prompt cache and stays diff-clean for sync tools.
- `CLAUDE_CONFIG_DIR` honoured for per-instance memory namespacing.
- Codex CLI distillation backend (`codex exec`, no API key) and an
  install-time prompt to pick the backend.
- Machine-local backend selection in the state dir (not synced), plus a
  per-machine distill opt-out for shared stores.
- `doctor` / `prune` / auto-prune / index self-heal (store audit).
- Claude CLI distillation backend (`claude -p`, no API key).

### Fixed
- Index links point at the real files on disk; distillation guarded against
  capturing its own tooling output (artifact guard, kept narrow so legit
  project prose survives).

## 0.1.0 — 2026-06 (initial, as engram)

### Added
- File-based memory store (one Markdown record per memory + `MEMORY.md`
  index), grep-based recall, LLM distillation with write gate and dedup,
  anti-rot context monitor (~45% checkpoint + nudge), working-state handoff
  re-injected after `/clear`.
- Cross-agent phase: stdlib MCP server (`remember` / `recall` / `answer`) and
  Codex / OpenCode installers sharing one store per project.
- Secret redaction before distillation; structured-output (json_schema)
  extraction; CI.
- Trust/decay model and typed-memory categories adapted from
  [memanto](https://github.com/moorcheh-ai/memanto) (MIT).

[Unreleased]: https://github.com/vcnngr/foldcrumbs/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/vcnngr/foldcrumbs/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/vcnngr/foldcrumbs/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vcnngr/foldcrumbs/releases/tag/v0.3.0
