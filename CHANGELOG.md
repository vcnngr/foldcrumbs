# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The same `/remember`, `/recall`, `/forget`, `/memory` surface for the other
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
  `/remember`, `/recall`, `/forget` and `/memory` to `<config-dir>/commands/`
  (managed files: user-edited copies are never touched; `uninstall` removes
  only ours). `/remember` with no arguments distills durable memories from the
  live conversation with user confirmation — in-context distillation, no LLM
  backend required.
- CI publishes to PyPI automatically when a GitHub release is created
  (`publish.yml`, `PYPI_API_TOKEN` repo secret).

### Fixed
- Command frontmatter emits quoted YAML scalars — `/memory`'s description
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

[Unreleased]: https://github.com/vcnngr/foldcrumbs/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/vcnngr/foldcrumbs/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vcnngr/foldcrumbs/releases/tag/v0.3.0
