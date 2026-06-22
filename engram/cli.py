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
from difflib import SequenceMatcher
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
    query = args.query.lower()
    words = [w for w in __import__("re").findall(r"[a-z0-9]+", query) if len(w) > 2]
    scored: list[tuple[float, MemoryRecord]] = []
    for m in store.load_all():
        if m.status != "active":
            continue
        hay = f"{m.title}\n{m.content}\n{' '.join(m.tags)}".lower()
        if query in hay:
            score = 1.0
        elif words:
            # grep-like: fraction of query words present, plus a fuzzy nudge.
            overlap = sum(1 for w in words if w in hay) / len(words)
            score = overlap * 0.9 + SequenceMatcher(None, query, hay).ratio() * 0.1
        else:
            score = SequenceMatcher(None, query, hay).ratio()
        if score >= 0.22:
            scored.append((score, m))
    scored.sort(key=lambda t: t[0], reverse=True)
    top = [m for _, m in scored[: args.limit]]
    block = format_context_block(top, heading=args.query)
    print(block or "(no matching memories)")
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
    print(f"LLM endpoint: {config.LLM_ENDPOINT} (model {config.LLM_MODEL})")
    print(f"LLM reachable: {llm.available()}")
    print(f"context budget: {config.CONTEXT_BUDGET} @ {int(config.CONTEXT_PCT*100)}%")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else install.default_settings_path(
        global_scope=not args.local
    )
    changes = install.install(path)
    print(f"settings: {path}")
    print("added:", changes or "(nothing — already installed)")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else install.default_settings_path(
        global_scope=not args.local
    )
    removed = install.uninstall(path)
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

    sub.add_parser("index", help="rebuild MEMORY.md").set_defaults(func=_cmd_index)

    d = sub.add_parser("distill", help="distill a transcript/text into memories")
    d.add_argument("file", nargs="?", help="path to text file (default: stdin)")
    d.set_defaults(func=_cmd_distill)

    sub.add_parser("status", help="show config + stats").set_defaults(func=_cmd_status)

    ins = sub.add_parser("install", help="merge hooks into settings.json")
    ins.add_argument("--local", action="store_true", help="project .claude instead of global")
    ins.add_argument("--settings", help="explicit settings.json path")
    ins.set_defaults(func=_cmd_install)

    uns = sub.add_parser("uninstall", help="remove engram hooks")
    uns.add_argument("--local", action="store_true")
    uns.add_argument("--settings")
    uns.set_defaults(func=_cmd_uninstall)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
