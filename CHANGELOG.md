# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  Writes are serialised and carry the store version they were taken from, so
  a slower scan cannot replace a newer snapshot — sharding removes that race
  between instances, but one instance runs several processes. Merged at read time with a total ordering
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
- Test isolation: the suite now isolates the state and config dirs, not just the
  memory dir — including the `FOLDCRUMBS_*` names, which take precedence over
  the legacy `ENGRAM_*` ones. With federated recall it would otherwise have read
  and written the developer's real registry and stores.

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
