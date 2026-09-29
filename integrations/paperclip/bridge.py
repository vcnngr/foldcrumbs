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
import json
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
    narrow it. The path is deterministic and collision-safe by construction:
    distinct scopes → distinct encoded cwds → distinct stores. Slashes are
    flattened so a malicious company id cannot escape the root.
    """
    if not company:
        raise ValueError("company is required for a Paperclip memory scope")

    def _safe(part: str) -> str:
        # flatten separators so a scope id can't traverse out of the root
        return "".join(c if (c.isalnum() or c in "-_.") else "_"
                       for c in part).strip(".") or "_"

    segs = ["company", _safe(company)]
    if agent:
        segs += ["agent", _safe(agent)]
    if project:
        segs += ["project", _safe(project)]
    return str(_root().joinpath(*segs))


def _cwd_of(req: dict) -> str:
    """Resolve the store cwd for a request dict (company/agent/project)."""
    return scope_cwd(req.get("company", ""), req.get("agent", ""),
                     req.get("project", ""))


def _provenance_tags(req: dict) -> list[str]:
    """Paperclip entity refs as retrieval tags (their provenance concern)."""
    tags = [f"pc:{k}={req[k]}" for k in
            ("company", "agent", "project", "issue", "run", "comment",
             "document") if req.get(k)]
    tags += [f"tag:{t}" for t in (req.get("tags") or [])]
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
    text = req.get("text") or ""
    if not text:
        return {"ok": False, "error": "text is required"}
    rec = MemoryRecord(
        title=req.get("title") or text[:80],
        content=text,
        type=req.get("type", "fact"),
        confidence=float(req.get("confidence", 0.8)),
        provenance=req.get("provenance", "paperclip"),
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
    limit = int(req.get("limit", 10))
    hits = store.search(req.get("query", ""), limit=limit, cwd=cwd,
                        types=req.get("types") or None,
                        tags=req.get("tags") or None,
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
    rec = store.get(req.get("handle", ""), cwd)
    if rec is None:
        return {"ok": False, "error": "no such handle in scope",
                "handle": req.get("handle")}
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
    include_inactive = bool(req.get("include_inactive", False))
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
    action = store.forget(req.get("handle", ""), cwd,
                          hard=bool(req.get("hard", False)))
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


def dispatch(op: str, req: dict) -> dict:
    """Run one portable-core op. Raises KeyError on unknown op."""
    return _OPS[op](req)


def main(argv: list[str] | None = None) -> int:
    """CLI: the surface a Paperclip `process`/http adapter shells into.

    `bridge capabilities`           → JSON capability manifest
    `bridge call <op> '<json req>'` → JSON result (op ∈ ingest/query/…)
    """
    ap = argparse.ArgumentParser(
        prog="foldcrumbs-paperclip-bridge",
        description="foldcrumbs memory-provider bridge for Paperclip")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("capabilities", help="print the provider capability manifest")
    call = sub.add_parser("call", help="invoke one portable-core operation")
    call.add_argument("op", choices=sorted(_OPS))
    call.add_argument("request", help="JSON request object")
    args = ap.parse_args(argv)

    if args.cmd == "capabilities":
        print(json.dumps(CAPABILITIES, indent=1, ensure_ascii=False))
        return 0

    try:
        req = json.loads(args.request)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad JSON request: {e}"}))
        return 2
    if not isinstance(req, dict):
        print(json.dumps({"ok": False, "error": "request must be a JSON object"}))
        return 2
    try:
        result = dispatch(args.op, req)
    except ValueError as e:            # e.g. missing company scope
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
