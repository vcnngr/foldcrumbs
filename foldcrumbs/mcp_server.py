"""Minimal MCP (Model Context Protocol) server over stdio — stdlib only.

Exposes three tools on the shared foldcrumbs store so MCP-speaking agents (Codex,
OpenCode, any MCP client) read/write the same memory Claude Code uses:

  * remember(content, type, title, confidence, tags) — store a memory
  * recall(query, limit)                              — search the store
  * answer(question, limit)                           — grounded answer (LLM)
  * forget(name)                                      — soft-delete one memory

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
from typing import Any

from . import __version__, llm, store
from .profile import format_context_block
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
            },
            "required": ["query"],
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
]


# --- tool implementations -------------------------------------------------- #


def _search(query: str, limit: int) -> list[MemoryRecord]:
    return store.search(query, limit=limit)


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
    mems = _search(str(args["query"]), int(args.get("limit", 10)))
    block = format_context_block(mems, heading=str(args["query"]))
    return block or "(no matching memories)"


def tool_answer(args: dict[str, Any]) -> str:
    mems = _search(str(args["question"]), int(args.get("limit", 8)))
    if not mems:
        return "(no relevant memories found)"
    context = "\n".join(f"- [{m.type}] {m.content}" for m in mems)
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
        hits = store.search(name, limit=5)
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


_DISPATCH = {"remember": tool_remember, "recall": tool_recall,
             "answer": tool_answer, "forget": tool_forget}


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
