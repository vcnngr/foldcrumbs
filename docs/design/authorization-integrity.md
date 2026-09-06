# Design: authorization integrity — memory as an honest authorization ledger

Rev 2 — 2026-09-07. Status: PROPOSED (RT r2 pending).
Rev 1 was RED: Kimi F1 (T11 cited a lock `store.upsert` does not take) and
GPT F1-F4 (same lock finding; lifecycle writers can erase/undo a revocation;
`superseded_by` is not a graph_path arc and superseded endpoints are
excluded; the honest-state contract never reached fetch/index/hooks/answer).
All four are absorbed below; GPT's F5-F7 (P1) are folded in as explicit
limits and schema detail rather than left implicit.

Source: arXiv 2609.01836, "Agent Memory Is a Surface for Endogenous
Authorization Laundering" (EAL-Bench).

## 1. The problem, honestly scoped

The paper's finding: when agents write permissions into persistent memory
incrementally, spurious authority appears (up to 50.2% of unauthorized
requests in their bench), and once it is in memory, executors act on it
(98.6%). No attacker needed — the memory system itself launders authority:
a permission's provenance washes out, the record survives the context that
created it, and revocation never propagates.

What foldcrumbs is NOT: a policy enforcement point. It cannot stop an
agent from doing anything; it never sees the action.

What foldcrumbs CAN be: an honest authorization ledger. When an agent (or
a human through an agent) asks memory "am I allowed to X?", every surface
the product serves must carry: the grant, its backing event, its expiry,
and its retirement chain — or a visible refusal. Never a bare "yes"
washed clean of history.

Honest limits, stated up front (RT r1 F5 — these are product limits, not
bugs to fix in this pass):
- **Semantic laundering is out of reach.** A `decision` whose body says
  "X is allowed forever" passes every gate here; no type system reads
  meaning. This design protects the TYPED authorization surface only.
- **The CLI attestation is procedural, not identity.** Whoever runs
  `remember --type authorization` attests meaning and legitimacy; the
  software cannot verify the human behind the shell. Same trust model as
  `graph transit` — a recorded choice, not an authenticated person.
- **Raw files stay greppable.** An agent that ignores served surfaces and
  cats the markdown sees the grant text without derived state. The
  contract binds what the PRODUCT serves (recall/fetch/index/hooks/
  answer/doctor), not the OS filesystem. Declared, not solved.
- We do NOT claim a measured EAL-Bench improvement; we never ran it.

## 2. Design decisions

### D1 — a new memory type: `authorization`

Added to VALID_TYPES. Schema fields (frontmatter, required FOR THIS TYPE
at the core, optional-and-ignored for others):
- `granted_to`: non-empty string — who holds the authority.
- `grants`: non-empty string — what is permitted, one statement.
- `backed_by`: memory id — pointer to the source event/decision record.

Identity (RT r1 F6): an authorization carries the standard stable UUID
`id`; additionally, the grant's IDENTITY is the tuple
(granted_to, grants, backed_by) — used for exact-match dedup refusal
below. `expires_at` is MANDATORY for this type and must parse to an
aware future datetime at write time; `is_expired`'s legacy
"expires_at=None means never expires" behavior is NOT inherited: a
stored authorization with missing/naive/past-expiry `expires_at` is
treated as EXPIRED by every served read (fail-closed on read), and is
refused at write.

### D2 — creation constraints (minting; fail-closed, at the core)

A single new validator, called by every creation path (upsert/
write_memory/import/adopt/ingest-persist), refuses an authorization
unless ALL hold:
1. `expires_at` set, aware, future. No immortal permissions.
2. `granted_to` and `grants` non-empty; `backed_by` present.
3. `backed_by` resolves to a LIVE (active, non-expired) memory of type
   `event` or `decision` IN THE SAME STORE (foreign roots cannot back a
   local grant; a local grant cannot back into another store).
4. `provenance` in {explicit_statement, verified} — never inferred,
   never imported.
5. Identity/dedup (RT r1 F2.2, F6): authorizations are EXCLUDED from the
   fuzzy title+content dedup entirely. Creation is create-only on BOTH
   id and destination filename: a same-title/same-slug grant does not
   replace the predecessor's file — it is refused with a collision error
   naming both ids (an exact (granted_to, grants, backed_by) match
   against a LIVE grant is refused as duplicate; against a RETIRED one
   it is refused as collision — retirement history is never
   overwritten). The predecessor's bytes and id survive every path.

**Minting vs maintenance are different gates** (RT r1 F2, closing note):
constraints 1-5 apply to CREATION. They must NOT be re-applied to
maintenance writes on an existing grant whose backing has since died or
whose expiry has passed — retiring/annotating a dead grant is the
ledger's job, and refusing it because the backing is gone would be
exactly backwards.

**Locking (RT r1 F1 — Kimi and GPT independently):** `store.upsert`
takes NO lock today; claiming one was false. The implementation PR adds
a dedicated per-store `authorization` lock (federation.file_lock
pattern, same as relations/adopt locks) held around the
validate→check-backing→write sequence for grants. This serializes
concurrent MINTING only. It does NOT make the backing record immortal:
a concurrent `forget` of the backing outside that lock can still land
between check and write. The contract is therefore OBSERVATIONAL, not
transactional, and T11 is rewritten to that truth: at the moment of
creation the backing is verified under the lock; if it dies
concurrently, the outcome is either a refused write or a grant that
every served read derives as UNBACKED from then on — never a clean
ACTIVE grant whose backing was dead at write time. State is re-derived
on every served read; nothing trusts the write-time verdict forever.

### D3 — write-path lockdown (who may create one) + lifecycle matrix

Creation:
- CLI `remember --type authorization`: allowed, requires `--grants`,
  `--granted-to`, `--expires`, `--backed-by <memory>` (backing must
  already exist; errors name what's missing).
- MCP `remember`: type `authorization` REFUSED outright (an agent
  minting its own authority is the paper's attack with extra steps).
- `distill`: never EMITS authorization (type filter). Distill's
  contradiction pass may mark candidates superseded cross-type
  (distill.py:313) — authorizations are EXCLUDED from auto-supersede
  candidates: retiring a grant is a human verb (D5), never an LLM one.
- `ingest`: the extractor's output is type-filtered — extracted
  `authorization` items are DEMOTED to `context` and counted in the
  ingest summary as `demoted_authorizations: N` (RT r1 F7: ingest does
  not import frontmatter as records; the filter acts on the extractor's
  output, not the source text — a permission SENTENCE inside the body
  remains, covered by the semantic limit in §1).
- `import_store` / `migrate`: authorization records REFUSED (not
  silently retyped); the import summary reports `refused_authorizations:
  N`. Every migrate branch that moves memory files passes through the
  same refusal (tested per branch, RT r1 inventory).
- `adopt`: REFUSED always — both source type `authorization` and
  `--as-type authorization` (the type check runs BEFORE any file write,
  per adopt's fail-before-write).

Lifecycle mutations on an EXISTING grant (RT r1 F2 — the full writer
inventory, each classified):
| writer | verdict for authorization records |
|---|---|
| `outcome.set_outcome` | REFUSED (visible): outcome rewrites the whole record outside write_memory (outcome.py:88-95) and a race with supersede demonstrably resurrects retired records. Grants do not take good/bad verdicts. |
| `forget` (soft/hard) | ALLOWED (human verb): soft-delete of a grant is retirement-adjacent; the ledger keeps it visible to reads as RETIRED (deleted). Hard forget is allowed and documented as destroying the ledger entry — the revocation chain then shows a dangling link (D5 trace renders "target removed", never silently complete). |
| `archive`/`restore` | ALLOWED via human CLI; a restored grant re-derives its state honestly (it may come back UNBACKED/EXPIRED and reads say so). |
| `supersede` | ALLOWED — this IS the retirement verb (D5). |
| `relations.add/set_transit/remove` | ALLOWED (maintenance): relations rewrite whole records via write_memory — the implementation MUST preserve derived-state inputs (status, superseded_by, expires_at, backed_by, outcome*) byte-stable through a relations write on a grant (T15). |
| `proposals.promote` | touches relations only; inherits the relations row. |
| `distill` contradiction/auto-supersede | REFUSED as candidate (see above). |
| `upsert` re-run on same title | refused by D2.5 collision rule. |
| hooks/_worker, dashboard, conflicts | no record writers for this type (verified inventory); hooks snapshots covered in D4. |

### D4 — read-path honesty: every SERVED surface derives state

A single shared `derived_state(rec) -> ACTIVE | EXPIRED | RETIRED |
UNBACKED | <composite>` function (precedence: RETIRED(superseded/
deleted) > EXPIRED > UNBACKED > ACTIVE; composites render as
"UNBACKED (was ACTIVE)"-style, with the leading state being the honest
one — RT r1 F5 naming note). Every served surface either uses it or
excludes grants:

- `recall` (full) / profile context block: authorizations render in
  their OWN section — and because `store.search` excludes
  expired/superseded before rendering (store.py:933/_visible:1054), the
  section is fed by a SEPARATE ledger selection (all grants incl.
  expired/retired), not by the search hits:
  ```
  Authorizations (verify before acting — memory is not the source of truth):
    - <grants> | to: <granted_to> | until: <expires_at>
      backed by: <backed_by title> (<status>) | <STATE>
  ```
  This section is EXEMPT from profile.py:62's global "honour it, do not
  re-ask" preamble — its own header line is the instruction (RT r1 F4).
- `fetch` (CLI+MCP): grants get a deterministic state ENVELOPE before
  the raw text: `--- <name> [authorization: <STATE>, expires <date>,
  backed_by <id/status>]` then the file body unchanged. The raw file is
  a historical document; the envelope is the product's claim.
- `recall --index` / MCP index mode: state appended to the index line
  for grants.
- index snapshots (MEMORY.md rebuild) and hooks (session_start/
  post_compact injection): authorizations are EXCLUDED from the static
  snapshot text; the snapshot carries a pointer line instead:
  "authorizations: served live via `recall` — snapshot states can be
  stale" (RT r1 F4: a snapshot predating expiry/backing-death must not
  present as live truth).
- `answer`: grants EXCLUDED from the LLM context. Rev 1's
  `--include-authorizations` opt-in is DEFERRED (RT r1 F4: raw-body
  opt-in loses the envelope, and LLM output cannot be trusted to carry
  the warning). If a later version adds it, the state envelope is
  attached deterministically OUTSIDE the generation, never left to the
  model.
- `doctor`: new checks — unbacked grants, expired-but-still-active-file
  (decay gap), grants whose backing is superseded (retirement
  candidates), invalid/naive expiry.
- `audit`/`graph view` text renderers: grants appear with derived state.

### D5 — retirement and its trace (RT r1 F3)

`supersede <grant> --by <record>` is the retirement verb; no new
machinery. Semantics stated honestly: superseded means "this grant is
no longer usable" — whether the cause was revocation or correction is
carried by the superseding record's own content; the ledger does not
invent a motive. Label is RETIRED (with `superseded_by` kept);
recall/doctor may render "REVOKED" only when the superseding record's
type is `event`/`decision` mentioning it — otherwise RETIRED. Expired
AND retired grants keep both facts (state precedence in D4).

**graph_path is NOT the trace** (RT r1 F3, probe-verified):
`superseded_by` is not a relations_json arc, `relations._bfs` never
walks it, and `find_path` excludes non-active endpoints — a retired
grant cannot be a path endpoint, transit or not. The design does not
change graph semantics. Instead: a BOUNDED ledger trace, rendered
inside recall's authorization section and `doctor` output (no new
command): follow `superseded_by` links from the grant, resolving ids,
max depth 8, rendering dangling links ("target removed") and cycles
("cycle at <id>") VISIBLY — the trace is never declared complete when
it is broken. `backed_by` likewise renders as a pointer with the
backing's live status, not as a graph arc.

### D6 — non-goals

- No enforcement, no webhooks, no action blocking.
- No automatic expiry archiving beyond existing decay.
- No cross-store authorization views; federation renders foreign grants
  read-only in federated search ONLY if the foreign root's product
  version serves them — foreign authorization records are EXCLUDED from
  our federated recall/index/fetch surfaces entirely (they are another
  store's policy surface, not context).
- No historical backfill: existing memories keep their types; only new
  records get the constraints.
- No semantic permission classifier; no identity attestation; no
  OS-level read protection of grant files (all declared in §1).

## 3. Test obligations (acceptance matrix for the implementation PR)

T1. CLI remember --type authorization without each required field
    (grants/granted_to/expires/backed_by) → refused, nothing written.
T2. backed_by → superseded/deleted/foreign/nonexistent/wrong-type/
    expired → refused at write; matrix per case.
T3. MCP remember(type=authorization) → refused even with valid fields;
    server stays usable.
T4. ingest with extractor emitting authorization → demoted to context,
    `demoted_authorizations` counted; source-text permission frontmatter
    does not create a record (extractor stub test).
T5. import_store AND every migrate branch containing authorizations →
    refused count reported, no authorization lands.
T6. adopt of foreign authorization AND adopt --as-type authorization →
    refused before any file write (fail-before-write order asserted).
T7. recall renders the separate ledger-fed section; EXPIRED/RETIRED/
    UNBACKED states each exercised; expired grant never renders ACTIVE;
    section header exempts it from "honour it".
T8. answer excludes authorizations; NO opt-in exists in this version
    (CLI or MCP).
T9. retirement trace: grant → supersede → trace shows the chain;
    DANGLING (hard-forgotten target) and CYCLE fixtures render visibly
    broken; graph_path STILL refuses the retired endpoint (asserted as
    correct behavior, not worked around); transit unchanged.
T10. distill proposing type=authorization → filtered; distill
    contradiction pass never auto-supersedes a grant.
T11. (rewritten, RT r1 F1) Observational contract under concurrency:
    deterministic interleaving (barrier, not sleep) — backing verified
    alive at check, deleted before write: outcome is refusal OR a grant
    whose first served read derives UNBACKED. NEVER a clean ACTIVE
    grant backed by a dead record. Authorization lock exercised:
    concurrent mints serialize; concurrent mint+forget-of-backing lands
    in one of the two honest states.
T12. doctor flags all four gap classes (unbacked, expired-active-file,
    backing-superseded, invalid-expiry).
T13. outcome on an authorization → refused visibly; outcome↔supersede
    race cannot resurrect a retired grant (the rev-1 probe scenario).
T14. same-title grant after retirement: predecessor bytes and id
    INTACT on disk; creation refused as collision.
T15. writer matrix: relations add/set_transit/remove on a grant
    preserve status/superseded_by/expires_at/backed_by byte-stable;
    archive/restore re-derives honest state (restored grant reads
    UNBACKED/EXPIRED when true); forget-soft shows RETIRED in reads.
T16. served-reads matrix after expiry/backing-death/retirement:
    CLI+MCP fetch (envelope), recall full+index, hooks snapshot
    (exclusion line, never a stale grant), doctor — every surface
    states current derived state or excludes.
T17. ledger trace: depth cap 8, dangling, cycle fixtures; never
    "complete" when broken.
T18. fuzzy dedup exclusion: two grants, same body, different
    granted_to/backed_by/expiry → NEITHER validated onto the other;
    exact live duplicate refused as duplicate.
T19. schema hardening: naive/invalid/past expires_at refused at write;
    a hand-corrupted stored grant (missing expiry) reads as EXPIRED,
    never ACTIVE; all provenances matrixed (inferred/imported refused).
T20. semantic limits documented: permission-in-decision body,
    permission-in-ingested-context, hand-edited raw file — each
    asserted as DECLARED LIMIT (test pins the documentation line, not a
    block); no false authentication claim anywhere in docs.

## 4. Docs obligations

README x3 (EN source of truth): the authorization section carries §1's
four honest limits VERBATIM in spirit (ledger not enforcement; typed
surface only; procedural attestation; raw files greppable). CHANGELOG
under [Unreleased]. MCP docs note the remember-refusal. The recall
section header text is part of the docs (it is an instruction to
models).

## 5. Sequencing

One PR, schema+core+all served surfaces together (a half-landed type is
a laundering hole). GPT r1 concurs one atomic PR is right; internal
commits may separate schema/core/reads/tests. Size estimate: schema
(~80), core validator+lock (~150), lifecycle refusals (~100), served
reads incl. shared derived_state (~200), doctor (~60), CLI (~70), tests
(~600), docs x3 (~150).
