# foldcrumbs for coding agents

A short guide for AI agents (and the humans wiring them up) that want
foldcrumbs as their cross-session memory. It complements the
[README](README.md), which documents everything; this file covers the few
things an agent has to get right — and the honest limits of what the tool
will do for you.

## The mental model

foldcrumbs is **a folder of markdown files**, one memory per file, with
YAML-ish frontmatter carrying the metadata. No server, no daemon, no
database — and no network on the core path: files plus lexical recall work
fully offline. The optional LLM-backed features (distill, semantic recall,
answer) call whatever local or remote endpoint you point them at; nothing
phones home. Every claim in this file can be verified by opening those
files.

```bash
foldcrumbs remember "We deploy Tuesdays 10-12 UTC." --type decision
foldcrumbs recall "deploy window"          # ranked context block
foldcrumbs status                          # where the store lives, counts
```

The store location is derived from the working directory (per project) and
the agent instance, e.g. `~/.claude/projects/<slug>/memory/`. Override with
`FOLDCRUMBS_DIR` (store) and `FOLDCRUMBS_STATE_DIR` (locks/state). Files are
plain markdown — you can read, grep, edit and version them with your own
tools. **The files are the product; the CLI is one interface to them.**

## The retrieval loop an agent should use

Three layers, cheapest first — this is what the MCP `recall` tool does with
`index=true`, and what you should mirror on the CLI:

```bash
foldcrumbs recall "postgres migration" --index   # 1. hit list: filename, type, title
foldcrumbs fetch decision_postgres_migration.md  # 2. full file(s) by name
foldcrumbs timeline decision_postgres_migration.md --window 5
                                                 # 3. what happened around it
```

`recall` without `--index` returns the full context block directly — fine
for small stores, wasteful on token budget for large ones. `answer`
exists ("answer this question grounded in memory"); with an LLM
endpoint configured (`FOLDCRUMBS_LLM_*`) it composes the answer, and
without one it degrades to serving the relevant memories as raw
context.

## What the tool will NOT hide from you (and what it hides for you)

The served views (`recall`, `index`, `timeline`, federation) apply visibility
rules **on every read**. An agent must know these, because "not in the
results" has several distinct meanings:

| you don't see it because… | what it means | how to get it back |
|---|---|---|
| `status: archived` | decayed out of relevance, not wrong | `foldcrumbs restore <file>` |
| `status: superseded` | replaced by a newer memory | follow `superseded_by` in the frontmatter |
| `expires_at` in the past | it had a date and the date passed | rewrite it fresh; expiry is never extended silently |
| `contested_by` set | another instance claims this memory is superseded; recall hides **this record** — the claimer's own newer memory may still be served (claims are directional, not symmetric) — but a contested match the query would have served comes back as a "matched but not served" diagnostic line naming the claim. Read `conflicts` before picking a side | `foldcrumbs conflicts` lists the claim + exact resolution command |
| `invalidated_by` target died | the fact it depended on is gone | `foldcrumbs doctor` lists dangling/invalidated contracts |
| type `authorization` | grants are served **only** through the ledger section with derived state (ACTIVE/EXPIRED/UNBACKED/RETIRED), never as plain context | read the `Authorizations (…)` ledger section that **full** recall appends (CLI full mode and MCP `recall` full mode; `recall --index` returns before it). The `## Authorizations` block in `MEMORY.md` is the snapshot *pointer*, not the ledger |

Two guarantees worth trusting:

- **Explicit deletion is never silent.** `forget` is soft by default (the
  file stays, auditable); hard-delete is an explicit `--hard`. The
  pollution auto-prune that rides along with `distill`
  (`FOLDCRUMBS_NO_AUTO_PRUNE` disables it) never unlinks on a text match
  anymore: no textual pattern proved safe enough for unattended deletion
  (a table, a fence, even boilerplate can be legitimate prose), so it
  deletes nothing — shape-based junk is *flagged* for the pollution report
  and removed only by an explicit `prune --apply` (dry-run by default).
  Memory files are only as safe as your git history — version the store.
- **Snapshots never assert derived state.** `MEMORY.md` (the index) is a
  snapshot; anything whose truth depends on live state — grants, memories
  under an invalidation contract — is excluded from it structurally, with a
  pointer line. If `fetch` returns an envelope above the raw file
  (`[not served as current: …]`, `[authorization: …]`), **treat the
  envelope as authoritative over the body**. The converse does NOT hold:
  an envelope-free fetch is not a freshness certificate — an expired
  memory, for instance, comes back with no envelope; check `expires_at`
  in the frontmatter.

## Writing memories an agent should write

- One fact per file. `remember` deduplicates near-identical content by
  fuzzy match — a near-duplicate VALIDATES the existing record (bumps its
  trust), it is not a content-replacement command: the old text stays.
  (Authorizations are excluded from the fuzzy dedup and are create-only —
  they are never validated in place by a similar-looking new record.)
- Use `--type` honestly: `fact`, `preference`, `goal`, `decision`,
  `artifact`, `learning`, `event`, `instruction`, `relationship`,
  `context`, `observation`, `commitment`, `error`, `authorization`.
  Type drives ranking and visibility (authorizations get the ledger
  treatment above).
- Use `--expires` for anything with a date on it ("trial ends in
  September", "on-call until the 15th"). Expired memories leave every
  served view while staying on disk.
- `--tag` for retrieval facets you will actually filter on (`recall --tag`).
- Provenance is recorded automatically (who/when/source). Do not fabricate
  `--confidence`; the default (0.85 explicit, lower when inferred) is what
  the trust scoring consumes.
- **Permissions are not facts.** If the user grants the agent an
  authorization ("you may deploy to prod"), store it as
  `--type authorization --grants … --granted-to … --backed-by <event-id>`
  with an expiry. The MCP `remember` tool refuses to mint authorizations —
  that is deliberate: grants are minted on the CLI path with a backing
  event, not by the model mid-conversation. Be honest about what that
  buys: CLI-only is a *procedural* boundary, not caller authentication —
  a shell-capable agent can invoke the CLI, and provenance records the
  process's claim of who/when/source, not a verified identity. The
  guarantee is that minting is a distinct, auditable, backed-by-an-event
  act — not that it was necessarily a human hand on the keyboard.

## Relations, if you use the graph layer

`foldcrumbs relate A depends_on --to-memory B --evidence "…"` — nine
predicates, evidence required for `manual` provenance, memory-target only
for `invalidated_by`. Derived relations (from distill) are proposals, never
authority. `foldcrumbs graph path A B` walks strong edges. Traversal
grants no authority: a path through a superseded memory is information,
not permission.

## Federation, if multiple instances share a project

Stores are namespaced per instance × per project. `roots` lists other
registered stores; their memories appear in recall as foreign hits
(`<root_id>:<filename>`), fetchable read-only, clearly labelled. Writing to
another instance's store is refused everywhere — including for grants and
contradictions: claims are recorded (`supersedes_external`) and wait for
the owner instance to act. `adopt` copies a foreign memory into your store
with provenance intact; `adopt --check-fresh` (or the MCP `adopt` tool with
`check_fresh: true`) is a read-only report on whether each adopted source
has since changed, died, or vanished — freshness as information, never
automatic sync. `outcome` records whether an adopted memory proved
good or bad in practice (bad penalizes weight, it never hides the memory —
it gets served with a `(tentative)` marker).

## Freshness — the honest bit

There is no index to go stale, because there is no index in the retrieval
path: every served **live read** re-derives visibility from the files as
they are right now. (The two snapshot surfaces are the exception by
construction: `MEMORY.md` and the published federation shard are
point-in-time captures — they exclude derived-state records, and a
snapshot injected earlier in the session is not retroactively refreshed.)
`foldcrumbs index` rebuilds on demand. If you edited memory files by
hand, your edit is live on the next read; the only thing that can lag is
a snapshot a hook injected earlier in the session — re-run `foldcrumbs
index` or use `recall`, which always reads the files.

An agent may also just `grep -r` the store directory. That is supported,
not a hack: the files are stable-order, diff-clean markdown. Hand edits go
live on the next read — but keep the frontmatter parseable: a malformed
`relations_json` line, for instance, is dropped at parse time, and a
dropped line can mean a silently lost invalidation contract. Edit like you
edit code, with the file under version control. foldcrumbs deliberately
owns no search infrastructure — retrieval quality comes from the
frontmatter contract, not from a proprietary index. (If your store ever
grows past tens of thousands of files and grep itself becomes the
bottleneck, point a trigram-indexed grep of your choice at the directory
from the outside; nothing in foldcrumbs needs to change for that.)

## Environment quick reference

| var | effect |
|---|---|
| `FOLDCRUMBS_DIR` | store location override (legacy: `ENGRAM_DIR`) |
| `FOLDCRUMBS_STATE_DIR` | state/locks location (legacy: `ENGRAM_STATE_DIR`) |
| `FOLDCRUMBS_SEMANTIC=1` | enable the optional semantic channel (needs an embedding endpoint) |
| `FOLDCRUMBS_LLM_*` | endpoint for `answer`/`distill`/`checkpoint` LLM calls |
| `FOLDCRUMBS_G2=1` | relation proposals during distill (default off) |

Everything degrades gracefully, but know what degradation means: no LLM
configured → `recall`/`remember` work fully; `answer` returns the
relevant memories as raw context instead of a composed answer;
**`distill` still runs** — it falls back to a keyword heuristic and
writes memories from it (they carry `provenance: inferred` and lower
confidence); `checkpoint` still writes a handoff — its fallback is the
scrubbed transcript tail, clearly marked `_(LLM unavailable — raw
tail)_`. Nothing that degrades stops silently pretending to be the
LLM version: every fallback labels itself. No semantic endpoint →
lexical ranking only. That is the design, not a failure mode.

## MCP

`foldcrumbs mcp` serves the same surface over stdio MCP: 11 tools
(`remember`, `recall`, `fetch`, `timeline`, `answer`, `forget`,
`graph_path`, `relate`, `ingest`, `adopt`, `outcome`) with the same
visibility rules and refusals as the CLI — including the grant-minting
refusal. Registration: `foldcrumbs install` wires it into Claude Code /
Codex / supported hosts.
