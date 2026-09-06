"""Minimal MCP (Model Context Protocol) server over stdio — stdlib only.

Exposes four tools on the shared foldcrumbs store so MCP-speaking agents (Codex,
OpenCode, any MCP client) read/write the same memory Claude Code uses:

  * remember(content, type, title, confidence, tags) — store a memory
  * recall(query, limit, type, tags)                 — search the store
  * answer(question, limit)                          — grounded answer (LLM)
  * forget(name)                                     — soft-delete one memory

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio
transport). We implement only what a client needs to list and call tools —
initialize / notifications / tools.list / tools.call / ping — so there are no
extra dependencies and nothing to keep running between sessions (the client
spawns this process on demand).

Run:  python3 -m foldcrumbs.mcp_server     (or the `foldcrumbs-mcp` console script)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import __version__, config, llm, store
from .profile import format_context_block
from .relations import PREDICATES
from .schema import VALID_TYPES, MemoryRecord

SERVER_NAME = "foldcrumbs"
DEFAULT_PROTOCOL = "2025-06-18"

# --- tool registry --------------------------------------------------------- #

TOOLS = [
    {
        "name": "remember",
        "description": (
            "Store a durable memory in the project's foldcrumbs store so future "
            "sessions recall it. Use for decisions, conventions, preferences, "
            "stable facts, lessons and goals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory, one self-contained statement."},
                "type": {"type": "string", "enum": sorted(VALID_TYPES), "description": "Memory category."},
                "title": {"type": "string", "description": "Short title (optional)."},
                "confidence": {"type": "number", "description": "0.0-1.0 (optional, default 0.85)."},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Search the project's foldcrumbs memory and return the most relevant "
            "memories as a context block. Call this at the start of a task to "
            "load prior decisions and conventions. mode='index' returns a "
            "compact filename/type/title index (savings grow with memory "
            "body length) — then use the fetch tool for the entries you "
            "actually need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Max memories (default 10)."},
                "type": {
                    "description": "Only memories of this type (or types). "
                                   "A string or an array of strings (repeatable).",
                    "anyOf": [
                        {"type": "string", "enum": sorted(VALID_TYPES)},
                        {"type": "array", "items": {"type": "string",
                                                    "enum": sorted(VALID_TYPES)}},
                    ],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only memories carrying at least one of these tags.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["full", "index"],
                    "description": "full (default): context block. index: "
                                   "compact hit list — pair with the fetch tool.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "graph_path",
        "description": (
            "Walk the strong (memory→memory) relations between two memories. "
            "Answers 'how are these connected / why did X lead to Y'. "
            "Result is tri-state: FOUND (the path, with evidence per edge and "
            "the direction each edge was walked), NOT_FOUND_EXHAUSTIVE (search "
            "completed, no connection), or TRUNCATED:<reason> (budget ran out "
            "— NOT proof of absence)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string",
                         "description": "Start memory: exact title, memory id, or filename stem."},
                "to": {"type": "string",
                       "description": "End memory: exact title, memory id, or filename stem."},
                "depth": {"type": "integer",
                          "description": "Max hops (default 3, hard cap 4)."},
                "max_nodes": {"type": "integer",
                              "description": "Max memories to visit (default 500)."},
                "include_inferred": {
                    "type": "boolean",
                    "description": "Also walk agent/inferred/legacy arcs and "
                                   "pending proposals. Default walks only "
                                   "human-attested (manual) arcs — set this "
                                   "only when you explicitly want model-"
                                   "suggested connections."},
            },
            "required": ["from", "to"],
        },
    },
    {
        "name": "relate",
        "description": (
            "Propose a typed relation between two memories (or from a memory "
            "to an external entity). Recorded with provenance 'agent' and "
            "confidence capped at 0.5 — model-suggested edges are NOT walked "
            "by graph_path unless the user explicitly opts in "
            "(include_inferred). Use when the transcript makes a durable "
            "link explicit (caused_by, supersedes, depends_on...). Do not "
            "invent evidence: pass the exact supporting quote."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory": {"type": "string",
                           "description": "Source memory: exact title, id, or filename stem."},
                "predicate": {"type": "string",
                              "enum": sorted(PREDICATES),
                              "description": "Relation type."},
                "to_memory": {"type": "string",
                              "description": "Target memory (title/id/stem). "
                                             "Use this OR to_entity, not both."},
                "to_entity": {"type": "string",
                              "description": "Target external entity label."},
                "namespace": {"type": "string",
                              "description": "Namespace for an external entity (default general)."},
                "evidence": {"type": "string",
                             "description": "Exact supporting quote from the transcript."},
                "confidence": {"type": "number",
                               "description": "0.0-1.0 (capped at 0.5 for agents)."},
            },
            "required": ["memory", "predicate"],
        },
    },
    {
        "name": "forget",
        "description": (
            "Forget one memory: mark it deleted so it drops out of the index "
            "and recall (the file is kept on disk for audit). Pass the exact "
            "memory filename as shown in MEMORY.md or a recall result. Use when "
            "a memory is wrong or explicitly revoked by the developer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Exact memory filename (e.g. decision_use_grep.md)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "answer",
        "description": (
            "Answer a question grounded in the project's memory (retrieves "
            "relevant memories, then asks the local LLM). Falls back to listing "
            "the memories if no LLM is available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "ingest",
        "description": (
            "Ingest an external document (local file path or http(s) URL) into "
            "the store as typed memories with provenance 'imported' and source "
            "'ingest:<origin>'. Use for design docs, ADRs, articles, specs — "
            "NOT for session transcripts (use distill for those)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string",
                           "description": "Local file path or http(s) URL."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "adopt",
        "description": (
            "Adopt ONE memory from a federated root into this store — "
            "explicit, never sync. The copy carries provenance 'imported' "
            "and source 'adopted:<root_id>:<memory_id>'; the attestation "
            "lives in the local adoption ledger. Refusals are explicit: "
            "unknown root, unstable/ambiguous id, non-live original, "
            "destination collision, already adopted. Pass 'search' + "
            "'from_root' instead of 'ref' to list live candidates without "
            "adopting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "<root_id>:<memory-file> — ids via "
                                       "the roots tool / `foldcrumbs roots`."},
                "note": {"type": "string",
                         "description": "Adoption evidence, stored in the "
                                        "ledger (one memory at a time)."},
                "as_type": {"type": "string",
                            "description": "Re-type the copy on adoption."},
                "search": {"type": "string",
                           "description": "List live candidates matching "
                                          "this query (adopts nothing)."},
                "from_root": {"type": "string",
                              "description": "Root id to search in "
                                             "(with 'search')."},
            },
            "required": [],
        },
    },
    {
        "name": "outcome",
        "description": (
            "Record the fleet outcome loop verdict on a memory: 'good' (it "
            "held — bumps validation) or 'bad' (it burned us — sets the "
            "persisted contradiction flag; a penalty never promotes). "
            "Effects apply to effective-weight paths (answer/audit), not "
            "to search ranking. Pass 'list' instead to see recorded "
            "outcomes with adoption annotations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory": {"type": "string",
                           "description": "Memory to judge: id, title or "
                                          "filename."},
                "verdict": {"type": "string", "enum": ["good", "bad"],
                            "description": "The verdict."},
                "note": {"type": "string",
                         "description": "Evidence for the verdict "
                                        "(flattened to one line)."},
                "list": {"type": "boolean",
                         "description": "List recorded outcomes instead."},
            },
            "required": [],
        },
    },
    {
        "name": "fetch",
        "description": (
            "Layer 3 of the token-efficient recall workflow: fetch the full "
            "text of memories by filename — batch the IDs/filenames you got "
            "from recall(mode='index'). Unknown names are reported, not "
            "silently dropped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "names": {
                    "description": "One filename or an array of filenames.",
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
            },
            "required": ["names"],
        },
    },
    {
        "name": "timeline",
        "description": (
            "Layer 2 of the recall workflow: chronological context around "
            "one memory (or a query's top hit) — the N memories before and "
            "after it by creation time, anchor marked with '>>'. Answers "
            "'what else was happening when this was decided'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "Memory filename/id/title, or a query "
                                       "(timeline around the top hit)."},
                "window": {"type": "integer",
                           "description": "Memories before/after the anchor "
                                          "(default 3)."},
            },
            "required": ["ref"],
        },
    },
]


# --- tool implementations -------------------------------------------------- #


def _search(query: str, limit: int, types: list[str] | None = None,
            tags: list[str] | None = None) -> list[MemoryRecord]:
    return store.search(query, limit=limit, types=types, tags=tags)


def tool_remember(args: dict[str, Any]) -> str:
    rec = MemoryRecord(
        title=str(args.get("title") or args["content"])[:80],
        content=str(args["content"]),
        type=str(args.get("type") or "fact"),
        confidence=float(args.get("confidence", 0.85)),
        provenance="explicit_statement",
        source="mcp",
        tags=list(args.get("tags") or []),
    )
    action, path = store.upsert(rec)
    store.rebuild_index()
    return f"{action} memory '{rec.title}' ({rec.type}) at {path.name}"


def tool_recall(args: dict[str, Any]) -> str:
    types = args.get("type")
    if isinstance(types, str):
        types = [types]
    tags = args.get("tags")
    mems = _search(str(args["query"]), int(args.get("limit", 10)),
                   types=list(types) if types else None,
                   tags=list(tags) if tags else None)
    mode = args.get("mode", "full")
    if mode is None:
        mode = "full"
    if not isinstance(mode, str) or mode not in ("full", "index"):
        return (f"refused: mode must be the string 'full' or 'index' "
                f"(got {type(mode).__name__}: {mode!r})")
    if mode == "index":
        # Layer 1 of 3: compact index — ref, type, title, date only.
        # Savings depend on body length; fetch full bodies via `fetch`.
        # RT F1: a FOREIGN hit is never offered as a bare local filename —
        # it is qualified <root_id>:<filename> and marked, so fetch cannot
        # silently resolve it to a local homonym.
        if not mems:
            return "(no matching memories)"
        lines = []
        for m in mems:
            name = m.source_path or m.filename()
            day = m.updated_at.strftime("%Y-%m-%d") if m.updated_at else "?"
            if m.is_foreign:
                ref = f"{m.origin_root_id}:{name}"
                lines.append(f"{ref}  [{m.type}]  {m.title}  ({day})  "
                             f"(foreign: {m.origin_root}, read-only)")
            else:
                lines.append(f"{name}  [{m.type}]  {m.title}  ({day})")
        lines.append(f"fetch full text with: fetch(names=[...]) "
                     f"— {len(mems)} hit(s)")
        return "\n".join(lines)
    block = format_context_block(mems, heading=str(args["query"]))
    return block or "(no matching memories)"


def _is_memory_filename(name: str) -> bool:
    """fetch is for memory .md files only (RT F3).

    Refuses anything that is not a plain ``*.md`` basename: store artifacts
    (index, ledgers, handoffs, dotfiles), paths, traversal.
    """
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return False
    if "\x00" in name:
        return False
    if not name.endswith(".md"):
        return False
    return not store.is_store_artifact(name)


def _fetch_one(name: str) -> tuple[str | None, str]:
    """Full text of one memory by ref. Returns (text, refusal_reason).

    Two ref shapes, no others (RT F1):
    * ``file.md`` — a LOCAL memory, resolved via store.get (path-safe).
    * ``<root_id>:file.md`` — a FOREIGN memory in a registered federated
      root, read-only, resolved inside that root's memory dir only.
    """
    if not isinstance(name, str) or not name:
        return None, "not found"
    if ":" in name:
        root_id, _, rel = name.partition(":")
        if not _is_memory_filename(rel):
            return None, "not a memory file (or unsafe ref)"
        from . import federation
        if not federation.valid_id(root_id):
            return None, "not found"
        root = federation.get_root(root_id)
        if root is None:
            return None, "not found"
        memdir = root.memory_dir()
        path = memdir / rel
        try:
            contained = path.resolve().is_relative_to(memdir.resolve())
        except OSError:
            return None, "not found"
        if not contained or not path.is_file():
            return None, "not found"
        try:
            return path.read_text(encoding="utf-8"), ""
        except OSError:
            return None, "unreadable"
    if not _is_memory_filename(name):
        return None, "not a memory file (or unsafe ref)"
    rec = store.get(name)
    if rec is None or rec.is_foreign:
        return None, "not found"
    real = rec.source_path or rec.filename()
    path = config.memory_dir() / real
    try:
        return path.read_text(encoding="utf-8"), ""
    except OSError:
        return None, "unreadable"


def tool_fetch(args: dict[str, Any]) -> str:
    names = args.get("names")
    if isinstance(names, str):
        names = [names]
    if (not isinstance(names, list) or not names
            or not all(isinstance(n, str) for n in names)):
        return ("refused: fetch needs 'names' — one filename string or a "
                "flat list of filename strings.")
    out = []
    for n in names:
        text, reason = _fetch_one(n)
        if text is None:
            out.append(f"--- {n}: {reason}")
        else:
            out.append(f"--- {n}\n{text.rstrip()}")
    return "\n\n".join(out)


def _timeline_rows(anchor: MemoryRecord, window: int) -> list[MemoryRecord]:
    all_mems = [m for m in store.iter_memories(config.memory_dir())
                if m.status == "active" and not m.is_expired]
    all_mems.sort(key=lambda m: (m.created_at, m.source_path or m.filename()))
    try:
        i = next(k for k, m in enumerate(all_mems) if m.id == anchor.id)
    except StopIteration:
        return []
    return all_mems[max(0, i - window): i + window + 1]


def tool_timeline(args: dict[str, Any]) -> str:
    ref = args.get("ref")
    if not isinstance(ref, str) or not ref:
        return ("refused: timeline needs 'ref' — a non-empty string "
                "(memory filename/title, or a query).")
    window = args.get("window", 3)
    # RT F4: bools are ints in Python and floats truncate — refuse both
    # instead of coercing; only a true non-negative int passes.
    if isinstance(window, bool) or not isinstance(window, int):
        return ("refused: 'window' must be a non-negative integer "
                f"(got {type(window).__name__}: {window!r})")
    if window < 0:
        return "refused: 'window' must be >= 0."
    anchor, refusal = _resolve_timeline_anchor2(ref)
    if anchor is None:
        return refusal or f"no memory matches {ref!r}"
    rows = _timeline_rows(anchor, window)
    lines = []
    for m in rows:
        day = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "?"
        mark = ">>" if m.id == anchor.id else "  "
        lines.append(f"{mark} {day}  [{m.type}] {m.title} "
                     f"({m.source_path or m.filename()})")
    return "\n".join(lines) or "(timeline empty)"


def _resolve_timeline_anchor2(ref: str) -> tuple[MemoryRecord | None, str]:
    """Resolve the anchor, failing VISIBLY on excluded states (RT F2).

    Returns (record, "") on success or (None, refusal) — a deleted/archived/
    expired/foreign anchor is refused with a reason, never rendered as an
    empty timeline that looks like 'nothing happened around this memory'.
    """
    rec = store.get(ref)
    refusal = _anchor_exclusion(rec, ref)
    if rec is not None:
        if refusal:
            return None, refusal
        return rec, ""
    try:
        local = _resolve_local_ref(ref)
    except ValueError:
        local = None
    if local is not None:
        refusal = _anchor_exclusion(local, ref)
        if refusal:
            return None, refusal
        return local, ""
    hits = _search(ref, 1)
    if hits:
        top = hits[0]
        if top.is_foreign:
            return None, (f"refused: the top hit for {ref!r} is foreign "
                          f"(root {top.origin_root}, read-only) — the "
                          "timeline is local-only; use recall for it")
        refusal = _anchor_exclusion(top, ref)
        if refusal:
            return None, refusal
        return top, ""
    return None, f"no memory matches {ref!r}"


def _anchor_exclusion(rec: MemoryRecord | None, ref: str) -> str:
    """Why this anchor cannot head a timeline ('' when it can)."""
    if rec is None:
        return ""
    if rec.is_foreign:
        return (f"refused: {ref!r} is foreign (root {rec.origin_root}, "
                "read-only) — the timeline is local-only")
    if rec.status == "deleted":
        return f"refused: {ref!r} is deleted — restore it first"
    if rec.status != "active":
        return (f"refused: {ref!r} is {rec.status} — the timeline covers "
                "active memories")
    if rec.is_expired:
        return f"refused: {ref!r} is expired — it left the active view"
    return ""


def tool_answer(args: dict[str, Any]) -> str:
    mems = _search(str(args["question"]), int(args.get("limit", 8)))
    if not mems:
        return "(no relevant memories found)"
    # Attribute foreign memories: without this the model can answer as if
    # another instance's conclusion were this store's own.
    context = "\n".join(
        f"- [{m.type}] {m.content}"
        + (f" (from {m.origin_root}, read-only)" if m.is_foreign else "")
        for m in mems
    )
    answer = llm.chat(
        messages=[
            {"role": "system", "content": "Answer the question using ONLY the "
             "provided project memories. If they don't cover it, say so."},
            {"role": "user", "content": f"Memories:\n{context}\n\nQuestion: {args['question']}"},
        ],
        temperature=0.1,
    )
    return answer or f"(LLM unavailable — relevant memories)\n{context}"


def tool_forget(args: dict[str, Any]) -> str:
    name = str(args["name"])
    if store.get(name) is None:
        # Local only: another instance's memory is readable from here but not
        # forgettable, so it must not be offered as a candidate.
        hits = store.search(name, limit=5, federated=False)
        if hits:
            options = "\n".join(f"  {m.source_path or m.filename()} — {m.title}"
                                for m in hits)
            return (f"'{name}' is not a memory filename. Closest matches:\n"
                    f"{options}\nCall forget again with the exact filename.")
        return f"no memory named or matching '{name}'"
    action = store.forget(name)
    if action is None:
        return f"failed to forget {name}"
    return f"{action}: {name} (file kept on disk; index rebuilt)"


def _resolve_local_ref(ref: str):
    """Resolve a memory reference (id, exact title, or filename stem) to the
    single local memory it names. Mirrors the CLI's resolution rules. Returns
    the record or raises ValueError with candidates listed."""
    mems = [m for m in store.load_all() if not m.is_foreign]
    by_id = {m.id: m for m in mems}
    if ref in by_id:
        return by_id[ref]
    for m in mems:
        if m.title == ref:
            return m
    for m in mems:
        if Path(m.filename()).stem == ref:
            return m
    hints = [m.title for m in mems if ref.lower() in m.title.lower()][:5]
    msg = f"no memory matches {ref!r}"
    if hints:
        msg += f" — did you mean: {', '.join(repr(h) for h in hints)}?"
    raise ValueError(msg)


def _parse_include_inferred(args: dict[str, Any]) -> bool:
    """Fail-closed boolean parsing (GPT code-RT P0-2): the published schema
    is boolean, so ONLY a real boolean counts. A string ("false", "true"),
    a number, or null must never enable the non-manual traversal — opting in
    is a conscious act and silent coercion would defeat the whole containment
    promise of G2. Absent -> False; wrong type -> visible refusal."""
    if "include_inferred" not in args or args["include_inferred"] is None:
        return False
    value = args["include_inferred"]
    if not isinstance(value, bool):
        raise ValueError(
            "include_inferred must be a boolean (true/false), got "
            f"{type(value).__name__}: {value!r} — non-manual arcs stay "
            "hidden until you opt in explicitly")
    return value


def tool_graph_path(args: dict[str, Any]) -> str:
    from . import relations
    try:
        src = _resolve_local_ref(str(args["from"]))
        dst = _resolve_local_ref(str(args["to"]))
    except ValueError as exc:
        return str(exc)
    try:
        include_inferred = _parse_include_inferred(args)
    except ValueError as exc:
        return f"refused: {exc}"
    res = relations.find_path(
        src.id, dst.id,
        depth=int(args.get("depth", 3)),
        max_nodes=int(args.get("max_nodes", 500)),
        include_inferred=include_inferred)
    status = res["status"]
    if status == "FOUND":
        lines = [f"FOUND — {len(res['path'])} steps:"]
        for step in res["path"]:
            edge = step.get("edge")
            # D3-bis: a transit step is never silent about being superseded.
            mark = (" (superseded — transit)"
                    if step.get("status") == "superseded" else "")
            if edge:
                arrow = "--" if step.get("forward", True) else "<--"
                ev = edge.get("e", "")
                tail = f"[{arrow} {edge['p']}, conf {edge['c']}"
                prov = edge.get("prov")
                if prov and prov != "manual":
                    tail += f", {prov}"
                if edge.get("_overlay"):
                    tail += ", pending proposal"
                tail += f"; evidence: {ev}" if ev else "; no evidence"
                lines.append(f"  -> {step['title']}{mark} "
                             f"({step['file']}) {tail}]")
            else:
                lines.append(f"  * {step['title']}{mark} ({step['file']})")
        return "\n".join(lines)
    if status == "NOT_FOUND_EXHAUSTIVE":
        note = res.get("note", "")
        out = (f"NOT_FOUND_EXHAUSTIVE — search completed, no connection "
               f"between these memories. Visited {res['reached']} memories.")
        if note:
            out += f" Note: {note}"
        return out
    return (f"{status} — visited {res['reached']} memories before the budget "
            f"ran out. {res['note']}")


def tool_relate(args: dict[str, Any]) -> str:
    """Agent-proposed relation (D1: prov=agent, confidence capped at 0.5).

    The arc is written to the store immediately but is NOT default-traversable
    in graph_path — it is a second-class citizen until a human promotes it.
    Refusals are explicit, never silent.
    """
    from . import relations
    try:
        src = _resolve_local_ref(str(args["memory"]))
    except ValueError as exc:
        return str(exc)
    predicate = str(args.get("predicate", ""))
    to_memory = str(args.get("to_memory") or "")
    to_entity = str(args.get("to_entity") or "")
    if to_memory and to_entity:
        return "refused: pass exactly one of to_memory / to_entity."
    if to_memory:
        try:
            dst = _resolve_local_ref(to_memory)
        except ValueError as exc:
            return str(exc)
        target = {"k": "m", "id": dst.id}
    elif to_entity:
        target = {"k": "x", "ns": str(args.get("namespace") or "general"),
                  "l": to_entity}
    else:
        return "refused: pass one of to_memory / to_entity."
    try:
        added = relations.add_relation(
            src.id, predicate, target,
            evidence=str(args.get("evidence") or ""),
            confidence=float(args.get("confidence", 0.5)),
            prov="agent")           # D1: agents are capped, never manual
    except relations.InvalidRelation as exc:
        return f"refused: {exc}"
    except relations.RelationLockBusy as exc:
        return f"refused: {exc}"
    if added:
        return ("relation added with provenance 'agent' (confidence capped "
                "0.5). It is NOT walked by graph_path unless include_inferred "
                "is set — a human can promote it to manual via "
                "`foldcrumbs graph doctor`.")
    return "relation already present — nothing written."


def tool_ingest(args: dict[str, Any]) -> str:
    from . import ingest as ingest_mod
    try:
        res = ingest_mod.ingest(str(args["source"]))
    except ingest_mod.IngestError as exc:
        return f"error: {exc}"
    return (f"ingested {res['created']} memories "
            f"({res['validated']} validated, {res['superseded']} superseded) "
            f"from {args['source']}")


def tool_adopt(args: dict[str, Any]) -> str:
    from . import adopt as adopt_mod
    search = str(args.get("search") or "")
    if search:
        from_root = str(args.get("from_root") or "")
        if not from_root:
            return "refused: 'search' needs 'from_root' — the root id to search in."
        try:
            limit = int(args.get("limit", 10))
        except (TypeError, ValueError):
            return "refused: 'limit' must be a non-negative integer."
        if limit < 0:
            return "refused: 'limit' must be >= 0."
        try:
            cands = adopt_mod.search_candidates(search, from_root, limit=limit)
        except adopt_mod.AdoptError as exc:
            return f"refused: {exc}"
        if not cands:
            return "no live candidates in that root."
        lines = [f"  {c['filename']}  [{c['type']}]  {c['title']}"
                 for c in cands]
        lines.append(f"adopt one with ref='{from_root}:<filename>'")
        return "\n".join(lines)
    ref = str(args.get("ref") or "")
    if not ref:
        return ("refused: adopt needs 'ref' (<root_id>:<memory-file>) — "
                "or 'search' + 'from_root' to list candidates.")
    note = str(args.get("note") or "") or "adopted via MCP (agent)"
    res = adopt_mod.adopt(ref, note=note,
                          as_type=args.get("as_type"))
    if not res["ok"]:
        return f"refused: {res['reason']}"
    return (f"adopted: {res['filename']}  ({res['source']}) — attested in "
            f"the local ledger")


def tool_outcome(args: dict[str, Any]) -> str:
    from . import outcome as outcome_mod
    if args.get("list"):
        rows = outcome_mod.list_outcomes()
        if not rows:
            return "no outcomes recorded yet."
        lines = []
        for r in rows:
            mark = "✓" if r["outcome"] == "good" else "✗"
            src = f"  [adopted from {r['adopted_from']}]" \
                if r.get("adopted_from") else ""
            note = f"  — {r['note']}" if r["note"] else ""
            lines.append(f"  {mark} {r['outcome']:4}  {r['filename']}{src}{note}")
        return "\n".join(lines)
    memory = str(args.get("memory") or "")
    verdict = str(args.get("verdict") or "")
    if not memory or not verdict:
        return ("refused: outcome needs 'memory' and 'verdict' "
                "(good|bad) — or 'list' to see recorded outcomes.")
    res = outcome_mod.set_outcome(memory, verdict,
                                  note=str(args.get("note") or ""))
    if not res["ok"]:
        return f"refused: {res['reason']}"
    if res["outcome"] == "good":
        return (f"recorded good — validation_count={res['validation_count']} "
                f"(effective-weight paths: answer/audit; search ranking "
                f"unchanged)")
    return ("recorded bad — contradiction persisted; effective weight "
            "penalized (a penalty never promotes). Only supersede clears "
            "the history.")


_DISPATCH = {"remember": tool_remember, "recall": tool_recall,
             "answer": tool_answer, "forget": tool_forget,
             "graph_path": tool_graph_path, "relate": tool_relate,
             "ingest": tool_ingest, "adopt": tool_adopt,
             "outcome": tool_outcome, "fetch": tool_fetch,
             "timeline": tool_timeline}


# --- JSON-RPC / MCP plumbing ----------------------------------------------- #


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    """Handle one JSON-RPC message. Returns a response, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")

    # Notifications carry no id and expect no response.
    if msg_id is None and method and method.startswith("notifications/"):
        return None

    if method == "initialize":
        client_proto = (msg.get("params") or {}).get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": client_proto or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": "Project memory. Call recall before a task; remember "
                            "durable decisions after.",
        })

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _DISPATCH.get(name)
        if fn is None:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
        try:
            text = fn(args)
            return _result(msg_id, {"content": [{"type": "text", "text": text}],
                                    "isError": False})
        except Exception as exc:  # tool-level error, not protocol error
            return _result(msg_id, {"content": [{"type": "text", "text": f"error: {exc}"}],
                                    "isError": True})

    if msg_id is None:
        return None  # unknown notification
    return _error(msg_id, -32601, f"Method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        try:
            response = handle(msg)
        except Exception as exc:
            response = _error(msg.get("id"), -32603, f"Internal error: {exc}")
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> int:
    # Optional: a fixed memory root via env so the client's cwd doesn't matter.
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
