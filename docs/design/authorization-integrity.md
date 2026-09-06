# Design: authorization integrity — memory as an honest authorization ledger

Rev 1 — 2026-09-07. Status: PROPOSED (RT pending).
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
agent from doing anything; it never sees the action. Claiming otherwise
would be the same overclaim the paper's "safeguards" make when they say
they reduce laundering — they only make memory a harder surface to
launder through.

What foldcrumbs CAN be: an honest authorization ledger. When an agent (or
a human through an agent) asks memory "am I allowed to X?", the answer
must carry: the grant, its backing event, its expiry, and its revocation
chain — or a visible refusal. Never a bare "yes" washed clean of history.

The paper's two safeguards map onto machinery we already have:
1. "permissions backed by valid source events" → our explicit relations
   (manual arcs with evidence) + provenance grammar.
2. "bounded event sourcing" → our supersede chain + graph transit rules
   (superseded stays superseded; only human attestation reopens a walk).

## 2. Design decisions

### D1 — a new memory type: `authorization`

Added to VALID_TYPES. An authorization is a grant: "X may do Y until Z,
because event E". It is NOT a fact, NOT a decision, NOT an instruction —
conflating it with those is exactly how laundering starts (a permission
buried in a "decision" never gets expiry or revocation semantics).

Schema addition (all in frontmatter, all optional-for-other-types):
- `granted_to`: string — who holds the authority (agent, role, person).
- `grants`: string — what is permitted, one self-contained statement.
- `backed_by`: memory id — the event/decision record that IS the source
  of this grant. A pointer, not a claim: the backing record must exist
  and be an `event` or `decision`.

### D2 — creation constraints (fail-closed, at the core)

`upsert`/`write_memory` refuse an authorization record unless ALL hold:
1. `expires_at` is set and in the future. No immortal permissions.
   (The expiry machinery already exists — FL era `--expires`; here it
   stops being optional.)
2. `backed_by` resolves to a LIVE (active, non-expired) memory of type
   `event` or `decision` in the same store. A grant pointing at a
   superseded/deleted/foreign/missing record is refused at write time.
3. `provenance` is `explicit_statement` or `verified` — never
   `inferred`, never `imported`. An LLM must not mint authority by
   inference; a document must not smuggle it via import.

Refusals are visible and typed (`AuthorizationError`, rendered as
"refused: ..." everywhere — CLI, MCP, hooks), following the adopt
pattern.

### D3 — write-path lockdown (who may create one)

- CLI `remember --type authorization`: allowed, requires `--grants`,
  `--expires`, and `--backed-by <memory>` (the backing record must
  already exist; the error names what's missing).
- MCP `remember`: type `authorization` REFUSED outright. The MCP caller
  is an agent; an agent minting its own permissions to an executor that
  trusts memory is the paper's attack with extra steps. Humans use the
  CLI (or an agent using the CLI under human attestation — same trust
  model as `graph transit`).
- `distill`: never emits authorization (LLM output types are filtered;
  the filter list gains the type).
- `ingest`: authorization stripped/demoted to `context` like the other
  reserved keys (transit, outcome*). A web page cannot grant authority.
- `import_store` / `migrate`: authorization records refused (not
  silently retyped) — a grant's backing event lives in the source
  store's history; without it the grant is unbacked by definition.
  The import summary reports the refusals count.
- `adopt`: REFUSED, always. Federated roots are exactly the untrusted
  surface: another instance's permission is not this instance's
  permission. (Design fleet-learning §: adoption copies memories;
  authority never travels.)

### D4 — read-path honesty (the ledger answer)

- `recall` / profile context block: authorizations render in their OWN
  section, never mixed into facts/decisions:
  ```
  Authorizations (verify before acting — memory is not the source of truth):
    - <grants> | to: <granted_to> | until: <expires_at>
      backed by: <backed_by title> (<status>) | <ACTIVE|EXPIRED|REVOKED>
  ```
  Status derivation: EXPIRED if past `expires_at`; REVOKED if superseded
  (the supersede chain IS the revocation log — bounded event sourcing);
  ACTIVE otherwise. An ACTIVE grant whose backing record died since
  write-time renders as ACTIVE (UNBACKED — backing <id> is <status>):
  the write-time check was honest when it ran; the read stays honest
  when it breaks.
- `answer`: authorization memories are EXCLUDED from the LLM context by
  default (`--include-authorizations` to opt in, CLI-only). Rationale:
  the paper's executors act 98.6% on what memory says; the cheapest
  honest move is not handing grants to the answer path at all.
- `doctor`: new checks — unbacked authorizations, expired-but-active
  (should have been archived by decay; flag the gap), grants whose
  backing record is superseded (revocation candidates).

### D5 — revocation

`supersede <grant> --by <record>` is the revocation verb; no new
machinery. The superseding record SHOULD be an event ("permission
withdrawn in standup") — the chain then reads as event sourcing:
grant → revoke-event → (new grant). `graph path` walks it; transit
rules unchanged (a revoked grant stays revoked; walking through it
needs human attestation like any superseded memory).

### D6 — non-goals

- No enforcement, no webhooks, no "block the action" — out of scope,
  out of honesty.
- No automatic expiry archiving beyond what decay already does
  (no-automation-by-design holds).
- No cross-store authorization views (federation shows them as
  foreign, read-only, and fetch/index already mark them; recall's
  authorization section is local-only like the timeline).
- No historical backfill magic: existing memories keep their types;
  only NEW records get the constraints.

## 3. Test obligations (RT should hold these against the implementation)

T1. remember --type authorization without expires/backed-by → refused,
    nothing written.
T2. backed_by pointing at superseded/deleted/foreign/nonexistent/wrong-type
    → refused at write; matrix per case.
T3. MCP remember(type=authorization) → refused even with valid fields.
T4. ingest document containing authorization frontmatter → demoted to
    context, refusal reported in the ingest summary.
T5. import/migrate of a store containing authorizations → refused count
    reported, no authorization lands.
T6. adopt of a foreign authorization → refused (identity check BEFORE
    any file write, per adopt's fail-before-write).
T7. recall renders the separate section; EXPIRED/REVOKED/(UNBACKED)
    states each exercised; expired grant never renders as ACTIVE.
T8. answer excludes authorizations by default; CLI opt-in works; MCP
    answer has NO opt-in.
T9. supersede chain: grant → revoke → recall shows REVOKED; graph path
    walks grant→revoke with the transit rules intact.
T10. distill with LLM proposing type=authorization → filtered out.
T11. TOCTOU: backing record deleted between check and write → the write
     still refuses or the record lands UNBACKED-flagged at read; never
     a silent clean grant. (Lock scope: the same memory-lock upsert
     already takes; the check runs inside it.)
T12. doctor flags all three gap classes (unbacked, expired-active,
     backing-superseded).

## 4. Docs obligations

README x3 (EN source of truth): the authorization section states the
honest scope line verbatim: "foldcrumbs is not a policy enforcement
point — it is an honest ledger: every grant carries its source event,
its expiry, and its revocation chain, or it does not exist."
CHANGELOG under [Unreleased]. MCP docs note the refusal.

## 5. Sequencing

One PR, schema+core+paths together (a half-landed type is a laundering
hole: authorization without D3 lockdown is worse than no type at all).
Size estimate: schema (~60 lines), store/core checks (~120), CLI (~60),
MCP refusals (~30), recall/answer/doctor (~120), tests (~400). Docs x3.
