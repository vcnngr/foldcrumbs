"""engram CLI (stdlib argparse).

Commands:
  remember   store a memory
  recall     search the store (substring + fuzzy) and render a context block
  index      rebuild MEMORY.md
  distill    distill a transcript/text file into memories (uses the LLM)
  status     show config + store stats
  install    merge hooks into Claude Code settings.json
  uninstall  remove engram hooks
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, distill, install, llm, store
from .profile import format_context_block
from .schema import VALID_TYPES, MemoryRecord


def _cmd_remember(args: argparse.Namespace) -> int:
    rec = MemoryRecord(
        title=args.title or args.text[:80],
        content=args.text,
        type=args.type,
        confidence=args.confidence,
        provenance="explicit_statement",
        source="cli",
        tags=args.tag or [],
    )
    action, path = store.upsert(rec)
    store.rebuild_index()
    print(f"{action}: {path}")
    return 0


def _cmd_recall(args: argparse.Namespace) -> int:
    top = store.search(args.query, limit=args.limit)
    block = format_context_block(top, heading=args.query)
    print(block or "(no matching memories)")
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    mems = store.search(args.question, limit=args.limit)
    if not mems:
        print("(no relevant memories found)")
        return 0
    context = "\n".join(f"- [{m.type}] {m.content}" for m in mems)
    out = llm.chat(
        messages=[
            {"role": "system", "content": "Answer the question using ONLY the "
             "provided project memories. If they don't cover it, say so."},
            {"role": "user", "content": f"Memories:\n{context}\n\nQuestion: {args.question}"},
        ],
        temperature=0.1,
    )
    print(out or f"(LLM unavailable — relevant memories)\n{context}")
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    handoff = distill.make_handoff(text)
    if not handoff:
        print("(nothing to checkpoint)")
        return 0
    path = store.write_handoff(handoff)
    print(f"handoff written: {path}")
    return 0


def _cmd_handoff(_: argparse.Namespace) -> int:
    print(store.read_handoff() or "(no handoff yet)")
    return 0


def _cmd_index(_: argparse.Namespace) -> int:
    print(f"rebuilt: {store.rebuild_index()}")
    return 0


def _cmd_distill(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not llm.available():
        print("warning: LLM endpoint unreachable — using heuristic fallback",
              file=sys.stderr)
    res = distill.distill_and_store(text, source="cli-distill")
    print(f"distilled: {res}")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    mems = store.load_all()
    active = [m for m in mems if m.status == "active"]
    print(f"memory dir : {config.memory_dir()}")
    print(f"index      : {config.index_path()}")
    print(f"memories   : {len(active)} active / {len(mems)} total")
    if config.LLM_BACKEND == "claude-cli":
        print(f"LLM backend: claude-cli ({config.CLAUDE_BIN})")
    else:
        print(f"LLM backend: openai — {config.LLM_ENDPOINT} (model {config.LLM_MODEL})")
    print(f"LLM reachable: {llm.available()}")
    print(f"context budget: {config.CONTEXT_BUDGET} @ {int(config.CONTEXT_PCT*100)}%")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    agent = args.agent
    if agent == "opencode":
        paths = install.opencode_paths(global_scope=not args.local)
        mcp = install.install_opencode_mcp(paths["config"])
        plugin = install.write_opencode_plugin(paths["plugins"])
        agents = install.append_agents_md(paths["agents"])
        print(f"opencode.json mcp: {mcp or '(already present)'} ({paths['config']})")
        print(f"plugin: {plugin}")
        print(f"AGENTS.md: {agents or '(block already present)'}")
        return 0

    path = Path(args.settings) if args.settings else install.default_settings_path(
        agent=agent, global_scope=not args.local
    )
    changes = install.install_hooks(path, agent=agent)
    print(f"settings: {path}")
    print("added:", changes or "(nothing — already installed)")
    if agent == "codex":
        print("codex MCP (config.toml):", install.install_codex_mcp_toml())
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else install.default_settings_path(
        agent=args.agent, global_scope=not args.local
    )
    removed = install.uninstall_hooks(path)
    print(f"removed from {path}: {removed or '(nothing)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engram", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("remember", help="store a memory")
    r.add_argument("text")
    r.add_argument("--type", default="fact", choices=sorted(VALID_TYPES))
    r.add_argument("--title", default="")
    r.add_argument("--confidence", type=float, default=0.85)
    r.add_argument("--tag", action="append")
    r.set_defaults(func=_cmd_remember)

    rc = sub.add_parser("recall", help="search the store")
    rc.add_argument("query")
    rc.add_argument("--limit", type=int, default=10)
    rc.set_defaults(func=_cmd_recall)

    an = sub.add_parser("answer", help="answer a question grounded in memory (LLM)")
    an.add_argument("question")
    an.add_argument("--limit", type=int, default=8)
    an.set_defaults(func=_cmd_answer)

    sub.add_parser("index", help="rebuild MEMORY.md").set_defaults(func=_cmd_index)

    cp = sub.add_parser("checkpoint", help="write a working-state handoff (LLM)")
    cp.add_argument("file", nargs="?", help="transcript/text file (default: stdin)")
    cp.set_defaults(func=_cmd_checkpoint)

    sub.add_parser("handoff", help="print the current handoff").set_defaults(
        func=_cmd_handoff)

    d = sub.add_parser("distill", help="distill a transcript/text into memories")
    d.add_argument("file", nargs="?", help="path to text file (default: stdin)")
    d.set_defaults(func=_cmd_distill)

    sub.add_parser("status", help="show config + stats").set_defaults(func=_cmd_status)

    ins = sub.add_parser("install", help="wire engram into a coding agent")
    ins.add_argument("--agent", choices=["claude", "codex", "opencode"], default="claude")
    ins.add_argument("--local", action="store_true", help="project scope instead of global")
    ins.add_argument("--settings", help="explicit settings.json path")
    ins.set_defaults(func=_cmd_install)

    uns = sub.add_parser("uninstall", help="remove engram hooks")
    uns.add_argument("--agent", choices=["claude", "codex", "opencode"], default="claude")
    uns.add_argument("--local", action="store_true")
    uns.add_argument("--settings")
    uns.set_defaults(func=_cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
