# foldcrumbs as a memory provider for Paperclip

[Paperclip](https://github.com/paperclipai/paperclip) orchestrates teams of
AI agents as a company (org chart, budgets, governance, heartbeats). Its
roadmap lists **Memory / Knowledge as not yet built** (`⚪` in ROADMAP.md),
and its SPEC declares a knowledge base an explicit anti-goal for core —
"it will be a plugin." The maintainer's [Memory API discussion
#1155](https://github.com/paperclipai/paperclip/discussions/1155) asks for
a **local markdown-first baseline** ("similar to `memsearch`", "inspectable
provenance", company-scoped, zero-config, no cloud) behind a two-layer
model: a Paperclip control plane that binds a provider per company/agent,
and a **provider adapter** that maps Paperclip memory requests onto a
concrete backend.

foldcrumbs is that local markdown-first backend. This document is for
whoever wires the two together. It is **not** a claim of an official
Paperclip plugin — per the maintainer's ruling on the honcho PR (#1092),
memory plugins live in their own repo; what ships here is the Python
**provider surface** a thin adapter shells into.

## What foldcrumbs brings to Paperclip's requirements

| Paperclip Memory API need (#1155) | foldcrumbs |
|---|---|
| local markdown-first baseline, inspectable provenance | file-per-memory markdown on disk; grep/edit/version it |
| company-scoped, complete data isolation | one store per company/agent/project scope (see Scoping) |
| zero-config path, no cloud, no API key | stdlib core, fully offline; `no_cloud`/`no_api_key` in the manifest |
| portable core: ingest/query/browse/get/forget/usage | the six bridge ops below |
| provenance back to runs/issues/comments/documents | Paperclip entity refs stored as tags, echoed in every result |
| optional richer capabilities (semantic, graph, federation) | opt-in, advertised as capability flags, never required |
| governance on destructive operations | soft-delete default, explicit hard-delete, auditable |

## The provider bridge

`integrations/paperclip/bridge.py` implements the portable-core operations
as a stdlib-only Python surface. A Paperclip `process`/http adapter (or a
future plugin) shells into it:

```bash
# what this provider offers (Paperclip reads this to bind capabilities)
python integrations/paperclip/bridge.py capabilities

# ingest — store a memory, scoped, with provenance
python integrations/paperclip/bridge.py call ingest '{
  "company": "acme", "agent": "cto", "project": "web",
  "text": "We deploy Tuesdays 10-12 UTC.",
  "type": "decision", "title": "Deploy window",
  "issue": "PAP-530", "run": "run-42"
}'
# -> {"ok": true, "action": "created", "handle": "decision_deploy_window.md", ...}

# query — recall within the SAME scope used on ingest
python integrations/paperclip/bridge.py call query \
  '{"company":"acme","agent":"cto","project":"web","query":"deploy window"}'
# -> {"ok": true, "count": 1, "results": [{... "paperclip": {"issue":"PAP-530","run":"run-42"} ...}]}

# get by handle, browse, forget, usage — same scope key
python integrations/paperclip/bridge.py call get     '{"company":"acme","agent":"cto","project":"web","handle":"decision_deploy_window.md"}'
python integrations/paperclip/bridge.py call browse  '{"company":"acme","agent":"cto","project":"web"}'
python integrations/paperclip/bridge.py call usage   '{"company":"acme","agent":"cto","project":"web"}'
python integrations/paperclip/bridge.py call forget  '{"company":"acme","agent":"cto","project":"web","handle":"decision_deploy_window.md"}'
```

**Scope keys must be consistent across operations.** The scope is the exact
`company`/`agent`/`project` triple — each distinct triple is its own
isolated store. If you `ingest` under `company=acme, agent=cto,
project=web`, you must `query`/`get`/`browse` under the same triple to see
it; querying `company=acme, agent=cto` (no project) hits a *different*
store and returns nothing. Paperclip's control plane owns which scope key
to pass and should pass it consistently.

Every request is a JSON object; every result is JSON with an `ok` boolean.
Exit codes — **every refusal is a JSON `ok:false` on stdout, never a
traceback and never bare argparse prose**:

- `0` ok
- `1` operation refused (valid envelope, bad operation): unknown op,
  validation failure, unknown handle, wrong field type
- `2` malformed envelope: short/unknown argv, unparsable JSON, non-object
  payload

The bridge never raises to the caller on a handled error — an optional
provider must degrade, not crash the control plane. Requests are validated
centrally: string fields reject non-strings, `limit` must be an integer in
1..200, `confidence` must be a finite number in 0..1, booleans must be
booleans.

## Scoping and isolation

Paperclip scopes memory by **company / agent / project**. The bridge maps
that triple to a synthetic working directory and passes it as `cwd=` to
every store call, so each scope lands in its **own isolated markdown
store**. Two important, honest properties:

- **Isolation is by construction and asserted**: each scope segment carries
  a 128-bit sha256 digest of the raw id, so distinct ids can never collide
  onto one store (`a/b` ≠ `a_b`, `..` ≠ `_`, `a-agent-b` ≠ `a`+`b`), even
  through foldcrumbs' own path encoding. `query` uses `federated=False`, so
  a company scope never surfaces another company's (or an unrelated
  federated root's) memories — matching Paperclip's "complete data
  isolation, company-scoped".
- **Scope id policy**: ids are compared on raw UTF-8 bytes — case-sensitive
  (`ACME` ≠ `acme`) and normalization-sensitive (NFC `é` ≠ NFD `e+accent`).
  Distinct raw ids are distinct tenants, always, on every filesystem. Ids of
  any length are safe: the readable path prefix is capped, the digest is the
  identity. The control plane should pass ids consistently.
- **The synthetic cwd is a KEY, not a container**: the physical store
  resolves under foldcrumbs' standard config-dir location derived from that
  key (`config.memory_dir`), namespaced by `FOLDCRUMBS_PAPERCLIP_ROOT`. The
  files are still plain markdown you can open — just at foldcrumbs' normal
  path, not literally under the root. (Stated plainly so nobody is
  surprised; verified in `bridge.py`'s Scoping note.)

```bash
export FOLDCRUMBS_PAPERCLIP_ROOT=/var/lib/paperclip/foldcrumbs   # namespace key
```

## Provenance

Pass Paperclip entity refs (`issue`, `run`, `comment`, `document`, plus
`company`/`agent`/`project`) on `ingest`; when provided they are stored as
tags and echoed back in `results[].paperclip` on query/get — a memory then
traces to the run/issue/comment that produced it (Paperclip's "keeps
provenance back to Paperclip runs, issues, comments, and documents"). The
refs are OPTIONAL at the provider level (only `company` and `text` are
required): enforcing them per workflow is the control plane's policy, which
is where Paperclip puts it.

## Optional capabilities (never required)

The `capabilities` manifest advertises what foldcrumbs can do beyond the
portable core, as flags Paperclip may ignore:

- `semantic_recall` — install `foldcrumbs[semantic]` for a local bundled
  embedding model (or point `FOLDCRUMBS_EMBEDDING_*` at any OpenAI-compatible
  embeddings endpoint). Core stays lexical and stdlib without it.
- `federation` — multiple Paperclip instances sharing a project's memory,
  each seeing the other's clearly-labelled foreign hits read-only.
- `graph` — typed relations between memories (`relate`, `graph path`).
- `forget_governance` — soft-delete default, explicit `hard`, auditable.

## Honest limits

- **usage reporting is structural, not token-cost.** The bridge reports
  counts-by-type/status and store bytes. It does **not** invent an LLM
  token/latency cost it cannot measure — Paperclip's control plane records
  that on its side (their concern, per #1155).
- **This is the provider surface, not a finished Paperclip plugin.** The
  adapter (process/http/plugin glue) is the integrator's thin layer; the
  maintainer's own guidance is that a memory plugin lives in its own repo.
- **No Paperclip-side changes are needed** to use the bridge as a process
  adapter; a deeper, "native" integration would be a provider adapter
  conforming to whatever Memory API contract #1155 lands on.

## Try it

```bash
pip install foldcrumbs                 # core, stdlib only
pip install 'foldcrumbs[semantic]'     # optional local semantic recall
python -m pytest tests/test_paperclip_bridge.py -q   # provider contract tests
```

The bridge is covered by `tests/test_paperclip_bridge.py`: portable-core
round-trips, company isolation (including the no-federated-leak regression),
scope-traversal safety, soft/hard forget governance, honest usage, and a
stdlib-only import check.
