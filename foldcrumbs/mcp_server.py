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

from . import __version__, llm, store
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
            "load prior decisions and conventions."
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
    block = format_context_block(mems, heading=str(args["query"]))
    return block or "(no matching memories)"


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


_DISPATCH = {"remember": tool_remember, "recall": tool_recall,
             "answer": tool_answer, "forget": tool_forget,
             "graph_path": tool_graph_path, "relate": tool_relate,
             "ingest": tool_ingest}


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
