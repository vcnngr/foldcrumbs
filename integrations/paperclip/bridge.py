"""Paperclip memory-provider bridge — foldcrumbs as a control-plane memory
provider for Paperclip (https://github.com/paperclipai/paperclip).

WHY THIS EXISTS (verified against Paperclip's own sources, 2026-09-29):
Paperclip's ROADMAP lists `⚪ Memory / Knowledge` as NOT implemented, and
doc/SPEC.md declares a knowledge base an explicit anti-goal for core — "it
will be a plugin." The maintainer's Memory-API discussion (#1155) asks for
exactly what foldcrumbs already is: a *local markdown-first baseline*
(their words: "similar to memsearch", "inspectable provenance",
company-scoped, zero-config, no cloud). Their recommended two-layer model
is (1) a Paperclip binding/control-plane that picks the provider per
company/agent and logs provenance+usage, and (2) a *provider adapter*
(built-in or plugin) that turns Paperclip memory requests into
provider-specific calls. THIS module is the provider side of layer 2: it
implements their portable-core primitives on top of the foldcrumbs store,
so any Paperclip adapter (process/http/plugin) can call it. It does NOT
claim to be a Paperclip plugin — per the maintainer's honcho-PR ruling,
memory plugins live in their own repo; this is the Python provider surface
a thin adapter shells into.

CONSTITUTION: stdlib only, same as the foldcrumbs core. No new
dependencies, no network on the bridge path. The optional semantic channel
(foldcrumbs[semantic]) is inherited from the store, never required here.

THEIR portable core (doc #1155 "The portable core should cover"):
    ingest / write   -> cmd_ingest
    search / recall  -> cmd_query
    browse / inspect -> cmd_browse
    get by handle    -> cmd_get
    forget/correction-> cmd_forget
    usage reporting  -> cmd_usage
Plus their control-plane concerns: company/agent/project SCOPING and
PROVENANCE back to Paperclip entities — both first-class below.

SCOPING (verified mechanism): foldcrumbs resolves the store from a
working directory (config.memory_dir(cwd) -> <claude_config_dir>/projects/
<encoded cwd>/memory). Paperclip scopes are company / agent / project, so
we map a scope to a synthetic cwd whose path encodes the triple, and pass
it as `cwd=` to EVERY store call. NOTE (accurate, not aspirational): the
synthetic cwd is a KEY, not a container — FOLDCRUMBS_PAPERCLIP_ROOT
namespaces the key, while the store physically lands in foldcrumbs'
standard config-dir location derived from that key. Isolation still holds
by construction: distinct scopes → distinct encoded cwds → distinct stores
(asserted in tests, acme vs globex never cross). No global env mutation at
request time (the root is read per call only to build the key), which
keeps a multi-tenant server race-free. A company/agent/project triple
therefore lands in its own isolated, inspectable markdown store — exactly
Paperclip's "complete data isolation, company-scoped" requirement.

PROVENANCE: every ingested memory records the Paperclip entity refs
(company/agent/project/issue/run/comment/document) in tags and in the
record source, so a memory always traces back to the run/issue/comment
that produced it — their "keeps provenance back to Paperclip runs, issues,
comments, and documents."
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

# foldcrumbs is importable when this file is run from a checkout or an
# install; keep the path resolution local to THIS file (lesson from the
# quality-bench RT: never hardcode an absolute repo path).
# bridge.py lives at <repo>/integrations/paperclip/, so parents[2] is the
# repo root that makes `import foldcrumbs` resolve.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parents[2]))

from foldcrumbs import config, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

# Root under which Paperclip scopes get their isolated stores. Read ONCE at
# import (a server may then set per-request scope via arguments, not env —
# mutating os.environ per request would race across concurrent tenants).
_ROOT_ENV = "FOLDCRUMBS_PAPERCLIP_ROOT"
_DEFAULT_ROOT = str(Path.home() / ".foldcrumbs-paperclip")


def _root() -> Path:
    """Base dir for all Paperclip scope stores (env override, read per call)."""
    return Path(os.path.abspath(os.path.expanduser(
        os.environ.get(_ROOT_ENV) or _DEFAULT_ROOT)))


def scope_cwd(company: str, agent: str = "", project: str = "") -> str:
    """Map a Paperclip scope to a synthetic cwd → its own isolated store.

    company is required (Paperclip memory is company-scoped); agent/project
    narrow it.

    COLLISION-RESISTANT BY CONSTRUCTION (RT PR #83 P0-1): sanitizing ids
    alone is NOT enough — distinct ids can sanitize to the same segment
    ("a/b" and "a_b" both → "a_b"), and foldcrumbs' memory_dir encoding
    then flattens "/" to "-", so even distinct paths could share one store
    ("company/a-agent-b" vs "company/a/agent/b"). Every segment therefore
    carries a 128-bit sha256 digest of the RAW id (see _scope_seg for the
    bit-length rationale and the case/Unicode policy): distinct raw ids →
    distinct digests → distinct stores, whatever downstream encoders do.
    The readable prefix is convenience; the digest is the identity.
    """
    if not company:
        raise ValueError("company is required for a Paperclip memory scope")

    segs = ["company", _scope_seg(company)]
    if agent:
        segs += ["agent", _scope_seg(agent)]
    if project:
        segs += ["project", _scope_seg(project)]
    return str(_root().joinpath(*segs))


def _scope_seg(part: str) -> str:
    """One collision-resistant path segment for a raw scope id.

    `<readable-prefix>--<sha256(raw utf-8)[:32 hex]>` (RT PR #83 r2 P1s):

    - The digest is **128 bits** (32 hex chars), not 64: for a tenant-
      isolation guarantee a truncated 64-bit digest is not strong enough
      to *claim* injectivity (birthday bound ~2^32 chosen inputs). 128
      bits is collision-resistant for any practical id population — we
      say "collision-resistant", not "injective".
    - The readable prefix is **capped at 16 chars** so the segment length
      is bounded (~50 chars) regardless of id length: a 300-char company
      id can no longer blow the 255-byte filename limit downstream
      (memory_dir flattens the whole cwd into ONE encoded component).
      Readability is a convenience; identity is the digest.
    - Digest input is the **raw UTF-8 bytes**: ids are case-sensitive and
      Unicode-normalization-sensitive by policy ("ACME" ≠ "acme", NFC "é"
      ≠ NFD "e+́"). Distinct raw ids → distinct stores, always; equal
      raw ids → equal stores, on every platform (digest chars are
      lowercase hex, immune to case-insensitive filesystems).
    """
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_"
                   for c in part).strip(".") or "_"
    prefix = safe[:16] or "_"
    digest = hashlib.sha256(part.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}--{digest}"


class _BadRequest(ValueError):
    """A request that fails validation — never a crash, always ok:false."""


def _require_str(req: dict, key: str, required: bool = False) -> str:
    v = req.get(key, "")
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise _BadRequest(f"{key} must be a string, got {type(v).__name__}")
    if required and not v:
        raise _BadRequest(f"{key} is required")
    return v


def _opt_str_list(req: dict, key: str) -> list[str] | None:
    v = req.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _BadRequest(f"{key} must be a list of strings")
    return v


def _opt_bool(req: dict, key: str) -> bool:
    v = req.get(key, False)
    if not isinstance(v, bool):
        raise _BadRequest(f"{key} must be a boolean, got {type(v).__name__}")
    return v


def _opt_limit(req: dict, key: str = "limit", default: int = 10,
               cap: int = 200) -> int:
    v = req.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise _BadRequest(f"{key} must be a positive integer, "
                          f"got {type(v).__name__}")
    if v < 1 or v > cap:
        raise _BadRequest(f"{key} must be in 1..{cap}, got {v}")
    return v


def _opt_confidence(req: dict) -> float:
    v = req.get("confidence", 0.8)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _BadRequest(f"confidence must be a number, got {type(v).__name__}")
    v = float(v)
    if not math.isfinite(v) or not (0.0 <= v <= 1.0):
        raise _BadRequest(f"confidence must be finite and in 0..1, got {v!r}")
    return v


def _cwd_of(req: dict) -> str:
    """Resolve + validate the store cwd (company required, all str)."""
    return scope_cwd(_require_str(req, "company", required=True),
                     _require_str(req, "agent"),
                     _require_str(req, "project"))


_ENTITY_KEYS = ("company", "agent", "project", "issue", "run", "comment",
                "document")


def _provenance_tags(req: dict) -> list[str]:
    """Paperclip entity refs as retrieval tags (their provenance concern).

    Entity refs are OPTIONAL (P1 fix: docs no longer claim otherwise); when
    present they must be strings — a non-string ref is a bad request, not a
    crash.
    """
    for k in _ENTITY_KEYS:
        if req.get(k) is not None and not isinstance(req[k], str):
            raise _BadRequest(f"{k} must be a string, got {type(req[k]).__name__}")
    tags = [f"pc:{k}={req[k]}" for k in _ENTITY_KEYS if req.get(k)]
    tags += [f"tag:{t}" for t in (_opt_str_list(req, "tags") or [])]
    return tags


def _record_to_dict(rec: MemoryRecord) -> dict[str, Any]:
    """Serialize a MemoryRecord to their normalized memory object.

    Keeps the provider-native id (filename + uuid) AND the Paperclip-facing
    fields, per #1155: "a way to record provider-native ids and metadata
    without pretending all providers are equivalent internally."
    """
    pc_tags = sorted(t[3:] for t in rec.tags if t.startswith("pc:"))
    user_tags = sorted(t[4:] for t in rec.tags if t.startswith("tag:"))
    return {
        # provider-native handle (their "get by provider record handle")
        "handle": rec.source_path or rec.filename(),
        "id": rec.id,
        "title": rec.title,
        "content": rec.content,
        "type": rec.type,
        "description": rec.description,
        "status": rec.status,
        "confidence": rec.confidence,
        "provenance": rec.provenance,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        "tags": user_tags,
        # provenance back to Paperclip entities
        "paperclip": dict(p.split("=", 1) for p in pc_tags if "=" in p),
    }


# --- portable-core operations (doc #1155) --------------------------------

def cmd_ingest(req: dict) -> dict:
    """ingest / write — store a memory from text, scoped + with provenance."""
    cwd = _cwd_of(req)
    text = _require_str(req, "text", required=True)
    mtype = _require_str(req, "type") or "fact"
    rec = MemoryRecord(
        title=_require_str(req, "title") or text[:80],
        content=text,
        type=mtype,
        confidence=_opt_confidence(req),
        provenance=_require_str(req, "provenance") or "paperclip",
        source="paperclip",
        tags=_provenance_tags(req),
    )
    action, path = store.upsert(rec, cwd)
    store.rebuild_index(cwd)
    return {"ok": True, "action": action, "handle": path.name,
            "scope": {"company": req.get("company"), "agent": req.get("agent"),
                      "project": req.get("project")}}


def cmd_query(req: dict) -> dict:
    """search / recall — the minimum contract: ranked memories for a query.

    federated=False is a SECURITY requirement, not a preference (RT): a
    Paperclip company scope must not surface memories from unrelated
    federated roots. foldcrumbs' default is federated=True (multiple
    instances sharing one project); here isolation wins, matching
    Paperclip's "complete data isolation, company-scoped". Cross-store
    sharing, if ever wanted, is an explicit control-plane decision, not a
    leak through the provider.
    """
    cwd = _cwd_of(req)
    query = _require_str(req, "query")
    limit = _opt_limit(req)
    hits = store.search(query, limit=limit, cwd=cwd,
                        types=_opt_str_list(req, "types"),
                        tags=_opt_str_list(req, "tags"),
                        federated=False)
    return {"ok": True, "count": len(hits),
            "results": [_record_to_dict(m) for m in hits]}


def cmd_get(req: dict) -> dict:
    """get by provider record handle.

    Honest about lifecycle: a soft-deleted/archived/superseded record is
    still returned by handle WITH its status visible — get-by-handle is a
    raw fetch, not a served view, so the control plane can see *why* it is
    no longer served rather than getting a bare 404. `served` flags whether
    recall would surface it.
    """
    cwd = _cwd_of(req)
    handle = _require_str(req, "handle", required=True)
    rec = store.get(handle, cwd)
    if rec is None:
        return {"ok": False, "error": "no such handle in scope",
                "handle": handle}
    out = {"ok": True, "memory": _record_to_dict(rec),
           "served": rec.status == "active"}
    return out


def cmd_browse(req: dict) -> dict:
    """browse / inspect — list the scope's memories (their inspect surface).

    Default view is SERVED memories (status active) — what recall would
    surface. `include_inactive: true` also returns soft-deleted / archived /
    superseded records, each with its status, for an audit/inspect surface
    (foldcrumbs' visibility-over-arbitration: don't hide lifecycle, show it).
    """
    cwd = _cwd_of(req)
    include_inactive = _opt_bool(req, "include_inactive")
    recs = list(store.iter_memories(cwd))
    if not include_inactive:
        recs = [r for r in recs if r.status == "active"]
    recs.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
    return {"ok": True, "count": len(recs),
            "include_inactive": include_inactive,
            "memories": [_record_to_dict(r) for r in recs]}


def cmd_forget(req: dict) -> dict:
    """forget / correction — soft by default (auditable), --hard to unlink.

    Mirrors foldcrumbs' governance: deletion is never silent and the
    default keeps the file (status=deleted). Paperclip's "governance on
    destructive operations" is honored by making hard-delete explicit.
    """
    cwd = _cwd_of(req)
    handle = _require_str(req, "handle", required=True)
    action = store.forget(handle, cwd, hard=_opt_bool(req, "hard"))
    store.rebuild_index(cwd)
    if action is None:
        return {"ok": False, "error": "no such handle in scope",
                "handle": req.get("handle")}
    return {"ok": True, "action": action, "handle": req.get("handle")}


def cmd_usage(req: dict) -> dict:
    """usage reporting — counts the control plane logs for memory work.

    Honest scope: foldcrumbs is a local file store with no token meter, so
    this reports structural usage (counts by type/status, store size), NOT
    LLM token cost. The bridge does not invent a cost number it cannot
    measure; Paperclip records token/latency cost on its own side.
    """
    cwd = _cwd_of(req)
    recs = list(store.iter_memories(cwd))
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in recs:
        by_type[r.type] = by_type.get(r.type, 0) + 1
        by_status[r.status] = by_status.get(r.status, 0) + 1
    memdir = config.memory_dir(cwd)
    bytes_ = sum(f.stat().st_size for f in memdir.glob("*.md")) \
        if memdir.is_dir() else 0
    return {"ok": True, "scope": memdir_str(cwd), "count": len(recs),
            "by_type": by_type, "by_status": by_status,
            "store_bytes": bytes_,
            "note": "structural usage only; LLM token/latency cost is "
                    "recorded by the Paperclip control plane, not here"}


def memdir_str(cwd: str) -> str:
    return str(config.memory_dir(cwd))


# --- dispatch ------------------------------------------------------------

_OPS = {
    "ingest": cmd_ingest,
    "query": cmd_query,
    "get": cmd_get,
    "browse": cmd_browse,
    "forget": cmd_forget,
    "usage": cmd_usage,
}

CAPABILITIES = {
    "provider": "foldcrumbs",
    "shape": "local markdown-first, file-per-memory, inspectable on disk",
    "core_ops": sorted(_OPS),
    "scoping": ["company", "agent", "project"],
    "provenance": True,
    "optional_capabilities": {
        # advertised, never required (their capability-flag model)
        "semantic_recall": "foldcrumbs[semantic] (opt-in, local or endpoint)",
        "federation": "multiple Paperclip instances sharing a project",
        "graph": "typed relations between memories",
        "forget_governance": "soft-delete default, explicit hard-delete",
    },
    "no_cloud": True,
    "no_api_key": True,
}


def dispatch(op: str, req: Any) -> dict:
    """Run one portable-core op. NEVER raises on a handled error (RT P0-2).

    An optional provider must degrade, not crash the control plane: bad op,
    non-dict request, or any validation/store error comes back as a JSON
    result with ok:false. Only truly exceptional conditions (bugs) escape.
    """
    fn = _OPS.get(op) if isinstance(op, str) else None
    if fn is None:
        return {"ok": False, "error": f"unknown op: {op!r}"}
    if not isinstance(req, dict):
        return {"ok": False,
                "error": f"request must be a JSON object, got {type(req).__name__}"}
    try:
        return fn(req)
    except _BadRequest as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:            # scope/store contract violations
        return {"ok": False, "error": str(e)}
    except OSError as e:               # dying disk, unreadable store dir
        return {"ok": False, "error": f"store I/O failed: {e}"}


class _JsonArgParser(argparse.ArgumentParser):
    """argparse that answers malformed argv as JSON rc2, never SystemExit.

    RT PR #83 r2 P0: a Paperclip adapter shelling into this CLI must get a
    machine-readable envelope for EVERY refusal — but stock argparse calls
    sys.exit(2) with prose on stderr for missing args / unknown commands
    (main([]), main(["call"]), main(["bogus"])). Override error()/exit() so
    those become {"ok": false, ...} on stdout and a plain rc we return.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.json_error: str | None = None

    def error(self, message):              # noqa: A003 — argparse hook
        self.json_error = message
        # do NOT sys.exit; raise a sentinel main() turns into rc2 JSON
        raise _ArgError(message)


class _ArgError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    """CLI: the surface a Paperclip `process`/http adapter shells into.

    `bridge capabilities`           → JSON capability manifest
    `bridge call <op> '<json req>'` → JSON result (op ∈ ingest/query/…)

    Exit codes (every refusal is JSON ok:false on stdout, never a traceback
    and never bare argparse prose):
      0 = ok:true
      1 = operation refused — valid envelope, bad operation (unknown op,
          validation failure, unknown handle, wrong field type)
      2 = malformed request envelope — unparsable/short argv, non-JSON or
          non-object payload (argparse usage errors included)
    """
    ap = _JsonArgParser(
        prog="foldcrumbs-paperclip-bridge",
        description="foldcrumbs memory-provider bridge for Paperclip")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities", help="print the provider capability manifest")
    call = sub.add_parser("call", help="invoke one portable-core operation")
    # op is validated by hand below (not argparse choices) so an unknown op
    # is an rc1 operation refusal with a JSON body, not an argparse rc2.
    call.add_argument("op")
    call.add_argument("request", help="JSON request object")

    try:
        args = ap.parse_args(argv)
    except _ArgError as e:                 # malformed argv → JSON rc2
        print(json.dumps({"ok": False, "error": f"bad invocation: {e}"}))
        return 2

    if args.cmd == "capabilities":
        print(json.dumps(CAPABILITIES, indent=1, ensure_ascii=False))
        return 0

    # `call` with an unknown op: the envelope is fine, the OPERATION is not
    # → rc1 (was wrongly rc2 in r1; the r2 test masked this — RT r2 P0).
    if args.op not in _OPS:
        print(json.dumps({"ok": False,
                          "error": f"unknown op: {args.op!r} "
                                   f"(valid: {', '.join(sorted(_OPS))})"}))
        return 1
    try:
        req = json.loads(args.request)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON request: {e}"}))
        return 2
    # dispatch is total: every handled failure is JSON ok:false (rc 1), never
    # a traceback. rc 2 stays reserved for a malformed request envelope
    # (unparsable JSON / non-object), which we check here before dispatching.
    if not isinstance(req, dict):
        print(json.dumps({"ok": False, "error": "request must be a JSON object"}))
        return 2
    result = dispatch(args.op, req)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
