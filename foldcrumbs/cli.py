"""foldcrumbs CLI (stdlib argparse).

Commands:
  remember   store a memory
  recall     search the store (substring + fuzzy) and render a context block
  index      rebuild MEMORY.md
  distill    distill a transcript/text file into memories (uses the LLM)
  status     show config + store stats
  install    merge hooks into Claude Code settings.json
  migrate    move a legacy engram install to foldcrumbs
  uninstall  remove foldcrumbs hooks
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
    top = store.search(args.query, limit=args.limit,
                       types=args.type or None, tags=args.tag or None)
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


def _cmd_doctor(_: argparse.Namespace) -> int:
    from foldcrumbs import audit
    a = audit.audit()
    print(f"memories   : {a['active']} active / {a['total']} total")
    print(f"dead links : {len(a['dead_links'])}" + (f"  {a['dead_links']}" if a['dead_links'] else ""))
    print(f"orphans    : {len(a['orphans'])}" + (f"  {a['orphans']}" if a['orphans'] else ""))
    print(f"pollution  : {len(a['pollution'])}" + (f"  {a['pollution']}" if a['pollution'] else ""))
    print(f"low-trust  : {len(a['stale'])}" + (f"  {a['stale']}" if a['stale'] else ""))
    if a["dead_links"] or a["orphans"]:
        print("hint: run `foldcrumbs index` to rebuild, or `foldcrumbs doctor` after a distill.")
    if a["pollution"]:
        print("hint: run `foldcrumbs prune` (dry-run) then `foldcrumbs prune --apply`.")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    from foldcrumbs import audit
    res = audit.prune(apply=args.apply, include_stale=args.include_stale)
    if not res["candidates"]:
        print("nothing to prune.")
        return 0
    for name, reason in sorted(res["candidates"].items()):
        mark = "removed" if name in res["removed"] else ("would remove" if not args.apply else "kept")
        print(f"  [{reason}] {name} — {mark}")
    if not args.apply:
        print(f"\n{len(res['candidates'])} candidate(s). Re-run with --apply to delete.")
    else:
        print(f"\nremoved {len(res['removed'])} file(s); index rebuilt.")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """Merge memories from another store (dedup-aware, dry-run by default).

    --from accepts either a memory directory itself or a project working
    directory (resolved through the same convention as this instance). Covers
    the structural multi-instance gap: per-CLAUDE_CONFIG_DIR stores mean one
    instance can know everything while another starts empty.
    """
    src = Path(args.from_dir).expanduser()
    # A memory dir contains record files directly; anything else is treated as
    # a project dir and resolved to its memory dir.
    if not (src.is_dir() and any(src.glob("*.md"))):
        src = config.memory_dir(args.from_dir)
    if not src.is_dir():
        print(f"source {src} not found")
        return 1
    if src.resolve() == config.memory_dir().resolve():
        print(f"source and target are the same store ({src}) — nothing to do")
        return 1
    plan = store.import_store(src, apply=args.apply)
    total = sum(len(v) for v in plan.values())
    if total == 0:
        print(f"no memories found in {src}")
        return 0
    verb = "" if args.apply else "would be "
    for action in ("created", "validated", "skipped"):
        for name in plan[action]:
            print(f"  [{action}] {name}")
    print(f"\nfrom {src}:")
    print(f"  {len(plan['created'])} {verb}created, "
          f"{len(plan['validated'])} {verb}validated (near-duplicate), "
          f"{len(plan['skipped'])} skipped")
    if not args.apply:
        print("re-run with --apply to import.")
    else:
        print("index rebuilt.")
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    """Forget a memory by filename; with a query, list candidates instead.

    Deleting knowledge is the one operation dedup can't undo, so it follows the
    prune convention: dry-run by default, --apply to do it.
    """
    target = args.target
    if store.get(target) is None:
        hits = store.search(target, limit=5)
        if not hits:
            print(f"no memory named or matching '{target}'")
            return 1
        print(f"'{target}' is not a filename; did you mean one of these?")
        for m in hits:
            print(f"  {m.source_path or m.filename()} — {m.title}")
        print("re-run with the exact filename.")
        return 1
    verb = "remove (hard)" if args.hard else "mark deleted"
    if not args.apply:
        print(f"would {verb}: {target}  (re-run with --apply)")
        return 0
    action = store.forget(target, hard=args.hard)
    if action is None:
        print(f"failed to forget {target}")
        return 1
    print(f"{action}: {target}; index rebuilt."
          + ("" if args.hard else " File kept on disk — `foldcrumbs prune --apply` clears it."))
    return 0


def _cmd_supersede(args: argparse.Namespace) -> int:
    """Mark one memory as superseded by another (both exact filenames)."""
    for name in (args.old, args.by):
        if store.get(name) is None:
            print(f"no memory file named '{name}' — use the exact filename "
                  "linked in MEMORY.md")
            return 1
    if not store.supersede(args.old, args.by):
        print(f"failed to supersede {args.old}")
        return 1
    print(f"superseded: {args.old} -> {args.by}; index rebuilt. "
          "File kept on disk — `foldcrumbs prune --apply` clears it.")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    mems = store.load_all()
    active = [m for m in mems if m.status == "active"]
    print(f"memory dir : {config.memory_dir()}")
    print(f"index      : {config.index_path()}")
    print(f"memories   : {len(active)} active / {len(mems)} total")
    backend = config.llm_backend()
    if backend == "claude-cli":
        print(f"LLM backend: claude-cli ({config.claude_bin()})")
    elif backend == "codex":
        print(f"LLM backend: codex ({config.codex_bin()})")
    elif backend in config._NO_LLM_BACKENDS:
        print("LLM backend: none — keyword heuristic only")
    else:
        print(f"LLM backend: openai — {config.LLM_ENDPOINT} (model {config.LLM_MODEL})")
    print(f"LLM reachable: {llm.available()}")
    print(f"distill here : {'on' if config.distill_enabled() else 'off (read-only consumer)'}")
    print(f"context budget: {config.CONTEXT_BUDGET} @ {int(config.CONTEXT_PCT*100)}%")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    """Migrate a legacy engram install to foldcrumbs (non-destructive).

    1. State dir: copy ~/.engram -> ~/.foldcrumbs (backend choice, CLI bins,
       checkpoint flags) when the new dir doesn't exist yet. The source is never
       deleted, so a machine can be rolled back.
    2. Memory (opt-in): with --from <old-project-dir>, copy that project's memory
       store into the *current* project's memory dir (deterministic slug). Never
       overwrites a non-empty target unless --force; never deletes the source.

    Recall still needs no LLM; this only moves files. Back-compat in config.py
    means foldcrumbs already reads a legacy ~/.engram, so this is about making the
    move explicit, not about restoring function.
    """
    import shutil

    old_state = Path.home() / ".engram"
    new_state = Path.home() / ".foldcrumbs"
    if new_state.exists():
        print(f"state : {new_state} already exists — skipped")
    elif old_state.exists():
        shutil.copytree(old_state, new_state)
        print(f"state : copied {old_state} -> {new_state}")
    else:
        print("state : no ~/.engram to migrate")

    if args.from_dir:
        src = config.memory_dir(args.from_dir)
        dst = config.memory_dir()
        if not src.exists():
            print(f"memory: source {src} not found — nothing to copy")
        elif src.resolve() == dst.resolve():
            print(f"memory: source and target are the same ({dst}) — skipped")
        elif dst.exists() and any(dst.iterdir()) and not args.force:
            print(f"memory: target {dst} not empty — pass --force to merge")
            return 1
        else:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                target = dst / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            print(f"memory: copied {src} -> {dst}")
    else:
        print(f"memory: (skipped) pass --from <old-project-dir> to copy its store "
              f"into {config.memory_dir()}")
    print("done. reinstall hooks with `foldcrumbs install` if not already.")
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
    if agent == "claude":
        from . import surface
        cmd_dir = surface.commands_dir(global_scope=not args.local)
        actions = surface.install_commands(cmd_dir)
        summary = ", ".join(f"/{Path(n).stem} {a}" for n, a in sorted(actions.items()))
        print(f"commands: {cmd_dir} — {summary}")
        sk_dir = surface.skill_dir(global_scope=not args.local)
        print(f"skill: {sk_dir} — {surface.install_skill(sk_dir)}")
        print("(restart open sessions to pick up new commands/skill)")
    if agent == "codex":
        print("codex MCP (config.toml):", install.install_codex_mcp_toml())
    _configure_backend_at_install(args)
    return 0


def _configure_backend_at_install(args: argparse.Namespace) -> None:
    """Pick the LLM distillation backend during install.

    Explicit ``--backend`` wins. Otherwise prompt interactively when on a TTY;
    when non-interactive (piped/CI) leave the existing choice untouched and say
    how to set it later.
    """
    if getattr(args, "no_backend_prompt", False):
        return
    choice = getattr(args, "backend", None)
    if not choice:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("LLM backend: left as-is "
                  f"({config.llm_backend()}); set later with `foldcrumbs backend <name>`")
            return
        choice = install.prompt_backend()
    if not choice:
        return
    written = install.configure_backend(choice)
    print(f"LLM backend: {choice} -> wrote {', '.join(written)} in {config.STATE_DIR}")


def _cmd_backend(args: argparse.Namespace) -> int:
    """Set (or, with no argument, show) the machine-local LLM backend."""
    if not args.choice:
        backend = config.llm_backend()
        print(f"LLM backend: {backend}")
        print(f"reachable  : {llm.available()}")
        print("choices    :", ", ".join(k for k, _ in install.BACKEND_CHOICES))
        return 0
    written = install.configure_backend(
        args.choice, bin_path=args.bin, endpoint=args.endpoint, model=args.model)
    print(f"LLM backend: {args.choice} -> wrote {', '.join(written)} in {config.STATE_DIR}")
    print(f"reachable  : {llm.available()}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else install.default_settings_path(
        agent=args.agent, global_scope=not args.local
    )
    removed = install.uninstall_hooks(path)
    print(f"removed from {path}: {removed or '(nothing)'}")
    if args.agent == "claude":
        from . import surface
        gone = surface.uninstall_commands(surface.commands_dir(global_scope=not args.local))
        print(f"commands removed: {gone or '(nothing)'}")
        sk = surface.uninstall_skill(surface.skill_dir(global_scope=not args.local))
        print(f"skill removed: {sk}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="foldcrumbs", description=__doc__)
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
    rc.add_argument("--type", action="append", choices=sorted(VALID_TYPES),
                    help="only memories of this type (repeatable)")
    rc.add_argument("--tag", action="append",
                    help="only memories carrying this tag (repeatable)")
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

    mg = sub.add_parser("migrate", help="migrate a legacy engram install to foldcrumbs")
    mg.add_argument("--from", dest="from_dir", metavar="OLD_PROJECT_DIR",
                    help="also copy this project's memory store into the current one")
    mg.add_argument("--force", action="store_true",
                    help="merge into a non-empty target memory dir")
    mg.set_defaults(func=_cmd_migrate)

    im = sub.add_parser("import",
                        help="merge memories from another store (dry-run by default)")
    im.add_argument("--from", dest="from_dir", required=True, metavar="DIR",
                    help="source memory dir, or a project dir to resolve")
    im.add_argument("--apply", action="store_true",
                    help="actually import (default: dry-run)")
    im.set_defaults(func=_cmd_import)

    sub.add_parser("doctor", help="audit store: dead links, orphans, pollution"
                   ).set_defaults(func=_cmd_doctor)

    fg = sub.add_parser("forget", help="forget one memory by filename (dry-run by default)")
    fg.add_argument("target", help="memory filename as linked in MEMORY.md "
                    "(a search query lists candidates)")
    fg.add_argument("--apply", action="store_true", help="actually forget (default: dry-run)")
    fg.add_argument("--hard", action="store_true",
                    help="remove the file instead of marking it deleted")
    fg.set_defaults(func=_cmd_forget)

    sp = sub.add_parser("supersede", help="mark a memory as superseded by another")
    sp.add_argument("old", help="filename of the outdated memory")
    sp.add_argument("--by", required=True, help="filename of the memory that replaces it")
    sp.set_defaults(func=_cmd_supersede)

    pr = sub.add_parser("prune", help="delete pollution / superseded memories (dry-run by default)")
    pr.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    pr.add_argument("--include-stale", action="store_true",
                    help="also prune low-trust memories")
    pr.set_defaults(func=_cmd_prune)

    ins = sub.add_parser("install", help="wire foldcrumbs into a coding agent")
    ins.add_argument("--agent", choices=["claude", "codex", "opencode"], default="claude")
    ins.add_argument("--local", action="store_true", help="project scope instead of global")
    ins.add_argument("--settings", help="explicit settings.json path")
    ins.add_argument("--backend", choices=list(config.BACKENDS),
                     help="LLM distill backend (skip the interactive prompt)")
    ins.add_argument("--no-backend-prompt", action="store_true",
                     dest="no_backend_prompt",
                     help="don't ask about / change the LLM backend")
    ins.set_defaults(func=_cmd_install)

    bk = sub.add_parser("backend", help="show or set the LLM distill backend")
    bk.add_argument("choice", nargs="?", choices=list(config.BACKENDS),
                    help="backend to select (omit to show current)")
    bk.add_argument("--bin", help="explicit CLI path for claude-cli/codex")
    bk.add_argument("--endpoint", help="HTTP endpoint for the openai backend")
    bk.add_argument("--model", help="model id for the openai backend")
    bk.set_defaults(func=_cmd_backend)

    uns = sub.add_parser("uninstall", help="remove foldcrumbs hooks")
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
