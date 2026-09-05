"""foldcrumbs CLI (stdlib argparse).

Commands:
  remember   store a memory
  recall     search the store (substring + fuzzy) and render a context block
  index      rebuild MEMORY.md
  distill    distill a transcript/text file into memories (uses the LLM)
  ingest     ingest a document file or URL into memories (provenance: imported)
  status     show config + store stats
  roots      list/add/remove the memory roots federated into the shared view
  install    merge hooks into Claude Code settings.json
  migrate    move a legacy engram install to foldcrumbs
  uninstall  remove foldcrumbs hooks
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config, distill, embeddings, federation, install, llm, profiles, store
from .profile import format_context_block
from .schema import VALID_TYPES, MemoryRecord


def _version_string() -> str:
    """Single source of truth for the printed version.

    Read from the package (foldcrumbs.__init__.__version__) so the CLI never
    drifts from what pip installed. Importing the package only pulls in this
    attribute — no store/config side effects.
    """
    from . import __version__
    return f"foldcrumbs {__version__}"


def _cmd_version(_: argparse.Namespace) -> int:
    print(_version_string())
    return 0


def parse_expiry(value: str):
    """Parse ``--expires``: an ISO date/datetime or a relative ``Nd`` offset.

    A bare date means "true until the END of that day" — a deadline of
    September 1st is still true on September 1st. Relative offsets (``30d``,
    ``2w``, ``6m``) count from now, same end-of-day rule for the bare forms.
    Raises ValueError with the offending value so argparse can report it.
    """
    from datetime import datetime, timedelta, timezone
    v = value.strip().lower()
    units = {"d": 1, "w": 7, "m": 30}
    if len(v) >= 2 and v[-1] in units and v[:-1].isdigit():
        delta = timedelta(days=int(v[:-1]) * units[v[-1]])
        when = datetime.now(timezone.utc) + delta
        return datetime(when.year, when.month, when.day,
                        23, 59, 59, tzinfo=timezone.utc)
    # Explicit date or datetime. A bare date is midnight, which would make the
    # memory expire as the day BEGINS — move it to the day's end instead.
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"not a date I can parse: {value!r} "
                         "(try 2026-09-01, 2026-09-01T12:00 or 30d)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if value.strip().count(":") == 0 and len(value.strip()) <= 10:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


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
    if args.expires:
        try:
            rec.expires_at = parse_expiry(args.expires)
        except ValueError as exc:
            print(f"refused: {exc}")
            return 1
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
    # Attribute foreign memories: without this the model can answer as if
    # another instance's conclusion were this store's own.
    context = "\n".join(
        f"- [{m.type}] {m.content}"
        + (f" (from {m.origin_root}, read-only)" if m.is_foreign else "")
        for m in mems
    )
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


def _cmd_ingest(args: argparse.Namespace) -> int:
    from . import ingest as ingest_mod
    try:
        if not llm.available():
            print("warning: LLM endpoint unreachable — using heuristic fallback",
                  file=sys.stderr)
        res = ingest_mod.ingest(args.source)
    except ingest_mod.IngestError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"ingested: {res}")
    return 0


def _cmd_adopt(args: argparse.Namespace) -> int:
    from . import adopt as adopt_mod
    if args.search:
        try:
            cands = adopt_mod.search_candidates(args.search, args.from_root,
                                                limit=args.limit or 10)
        except adopt_mod.AdoptError as exc:
            print(f"adopt: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if not cands:
            print("no live candidates in that root")
            return 0
        for c in cands:
            print(f"  {c['filename']}  [{c['type']}]  {c['title']}")
        print(f"\nadopt one with: foldcrumbs adopt {args.from_root[:8]}…:<filename>")
        return 0
    if not args.ref:
        print("adopt: expected <root_id>:<memory-file> (see `foldcrumbs roots`)",
              file=sys.stderr)
        raise SystemExit(2)
    res = adopt_mod.adopt(args.ref, note=args.note or "",
                          as_type=getattr(args, "as_type", None))
    if not res["ok"]:
        print(f"adopt refused: {res['reason']}", file=sys.stderr)
        raise SystemExit(1)
    print(f"adopted: {res['filename']}  ({res['source']})")
    return 0


def _outcome_ref(ref: str) -> str:
    """Resolve id/title/filename to the REAL on-disk filename for outcome.

    RT round-1 F4: the help promises id/title resolution; store.get alone
    only takes filenames. _resolve_memory_ref already does exact-id,
    exact-title and unique-stem resolution with visible ambiguity errors —
    reuse it, and fall back to the raw ref so store.get can report
    not-found uniformly.
    """
    from . import relations as _rel
    try:
        mem = _resolve_memory_ref(ref)
    except _rel.InvalidRelation:
        return ref   # let set_outcome report not-found uniformly
    return mem.source_path or mem.filename()


def _cmd_outcome(args: argparse.Namespace) -> int:
    from . import outcome as outcome_mod
    if getattr(args, "list", False):
        rows = outcome_mod.list_outcomes()
        if not rows:
            print("no outcomes recorded yet — "
                  "`foldcrumbs outcome <memory> good|bad`")
            return 0
        for r in rows:
            mark = "✓" if r["outcome"] == "good" else "✗"
            src = f"  [adopted from {r['adopted_from']}]" \
                if r.get("adopted_from") else ""
            note = f"  — {r['note']}" if r["note"] else ""
            print(f"  {mark} {r['outcome']:4}  {r['filename']}{src}{note}")
        return 0
    res = outcome_mod.set_outcome(_outcome_ref(args.memory), args.verdict,
                                  note=args.note or "")
    if not res["ok"]:
        print(f"outcome refused: {res['reason']}", file=sys.stderr)
        raise SystemExit(1)
    if res["outcome"] == "good":
        print(f"recorded good — validation_count={res['validation_count']} "
              f"(effective-weight paths: answer/audit; search ranking is "
              f"relevance-based and unchanged)")
    else:
        print("recorded bad — contradiction detected; effective weight is "
              "penalized (never below its non-contradicted value: a "
              "penalty does not promote). The flag survives disk; only "
              "`supersede` clears the history.")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    from foldcrumbs import audit
    a = audit.audit()
    print(f"memories   : {a['active']} active / {a['total']} total")
    print(f"dead links : {len(a['dead_links'])}" + (f"  {a['dead_links']}" if a['dead_links'] else ""))
    print(f"orphans    : {len(a['orphans'])}" + (f"  {a['orphans']}" if a['orphans'] else ""))
    print(f"retired    : {len(a['retired_links'])}"
          + (f"  {a['retired_links']}" if a['retired_links'] else ""))
    print(f"pollution  : {len(a['pollution'])}" + (f"  {a['pollution']}" if a['pollution'] else ""))
    print(f"low-trust  : {len(a['stale'])}" + (f"  {a['stale']}" if a['stale'] else ""))
    if a["dead_links"] or a["orphans"] or a["retired_links"]:
        print("hint: run `foldcrumbs index` to rebuild, or `foldcrumbs doctor` after a distill.")
    if a["pollution"]:
        print("hint: run `foldcrumbs prune` (dry-run) then `foldcrumbs prune --apply`.")
    from . import conflicts as conflicts_mod
    q = conflicts_mod.queue()
    if q["flagged"] or q["claims_out"] or q["contested_here"]:
        print(f"conflicts  : {len(q['flagged'])} ambiguous, "
              f"{len(q['claims_out'])} claims out, "
              f"{len(q['contested_here'])} contested — `foldcrumbs conflicts`")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "list"

    if action == "add":
        try:
            ref = profiles.add(args.name, args.kind, args.path)
        except (ValueError, federation.FederationConflict,
                profiles.AmbiguousProfile) as exc:
            print(f"refused: {exc}")
            return 1
        if ref is None:
            print("could not register that profile (unwritable, or a "
                  "conflicting root is already there)")
            return 1
        print(f"profile {ref.label} ({args.kind}) → {ref.path}")
        line = profiles.env_line(ref.label)
        if line:
            print(f"to use it: {line}")
        return 0

    if action == "import":
        res = profiles.import_agent(args.agent, apply=args.apply,
                                    prefix=args.prefix)
        if not res["found"]:
            print(f"no {args.agent} profiles found on this machine.")
            return 0
        for name in res["found"]:
            planned = res["plan"][name]
            if planned in res["skipped"]:
                mark = "  (already registered)"
            elif planned in res["failed"]:
                mark = f"  (FAILED: {res['failed'][planned]})"
            else:
                mark = ""
            print(f"  {planned}{mark}")
        if res["applied"]:
            print(f"registered {len(res['added'])} profile(s); "
                  f"{len(res['skipped'])} already there; "
                  f"{len(res['failed'])} failed.")
            if res["failed"]:
                print("failed profiles were NOT registered — see reasons above.")
            else:
                print("`foldcrumbs profile env <name>` prints what to set.")
        else:
            new = [n for n in res["plan"].values() if n not in res["skipped"]]
            print(f"{len(new)} profile(s) would be registered, each with a "
                  "memory of its own. Run with --apply to do it.")
            if res["failed"]:
                print(f"warning: {len(res['failed'])} would FAIL (bad names) — "
                      "fix the prefix before applying.")
        return 1 if res["failed"] else 0

    if action == "env":
        try:
            line = profiles.env_line(args.name)
        except profiles.AmbiguousProfile as exc:
            print(f"refused: {exc}")
            return 1
        if line is None:
            print(f"no profile named {args.name!r}")
            return 1
        print(line)
        return 0

    if action == "remove":
        try:
            gone = profiles.remove(args.name)
        except profiles.AmbiguousProfile as exc:
            print(f"refused: {exc}")
            return 1
        if not gone:
            print(f"no profile named {args.name!r}")
            return 1
        print(f"removed profile {args.name} — its memories are untouched")
        return 0

    rows = profiles.listing()
    if not rows:
        print("no profiles registered (run `foldcrumbs profile add <name>`)")
        return 0
    for r in rows:
        marks = []
        if r["current"]:
            marks.append("this instance")
        if r["in_use"]:
            marks.append("in use here")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {r['name']:<20} {r['kind']:<10} {r['path']}{suffix}")
    return 0


def _cmd_decay(args: argparse.Namespace) -> int:
    from foldcrumbs import audit
    res = audit.decay(apply=args.apply)
    if not res["candidates"]:
        print("nothing has decayed below the trust threshold.")
        return 0
    lapsed = set(res["expired"])
    for name, conf in sorted(res["candidates"].items()):
        why = "expired" if name in lapsed else f"trust {conf}"
        print(f"  {name}  ({why})")
    if res["applied"]:
        print(f"archived {len(res['archived'])} memory(ies). "
              "Files kept — `foldcrumbs restore <file>` brings one back.")
    else:
        print(f"{len(res['candidates'])} memory(ies) would be archived "
              "(not deleted). Run `foldcrumbs decay --apply` to do it.")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from foldcrumbs import store
    if store.set_status(args.name, "active"):
        print(f"restored {args.name}.")
        return 0
    print(f"nothing to restore for {args.name} "
          "(unknown file, or it is already active).")
    return 1


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
        # Local only: a foreign memory can be read but never forgotten from
        # here, so offering one as a candidate would suggest an action that
        # cannot succeed.
        hits = store.search(target, limit=5, federated=False)
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


def _cmd_conflicts(_: argparse.Namespace) -> int:
    """Show the reconciliation queue: ambiguous pairs, foreign claims."""
    from . import conflicts as conflicts_mod
    q = conflicts_mod.queue()
    print(conflicts_mod.format_queue(q))
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Render the dashboard as one self-contained HTML page."""
    import json as _json
    import tempfile
    import webbrowser
    from . import dashboard
    data = dashboard.collect()
    if args.json:
        print(_json.dumps(data, indent=1, ensure_ascii=False))
        return 0
    page = dashboard.render(data)
    if args.out:
        out = Path(args.out).expanduser()
        out.write_text(page, encoding="utf-8")
        print(f"dashboard written to {out}")
        path = out.resolve()
    else:
        # A temp file keeps the store and the repo free of generated artifacts;
        # the page is a report, not something to keep around.
        fd, tmp = tempfile.mkstemp(prefix="foldcrumbs-dashboard-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page)
        path = Path(tmp)
        print(f"dashboard: {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())
    return 0


def _resolve_memory_ref(ref: str):
    """Resolve a user-supplied reference to one local memory.

    Tries, in order: exact id, exact title, unique filename-stem. Ambiguity
    and absence are errors with the candidates listed — the graph layer never
    guesses which memory you meant.
    """
    from . import relations
    mems = store.load_all()
    by_id = {m.id: m for m in mems if not m.is_foreign}
    if ref in by_id:
        return by_id[ref]
    by_title = {}
    for m in mems:
        if not m.is_foreign:
            by_title.setdefault(m.title, []).append(m)
    if ref in by_title and len(by_title[ref]) == 1:
        return by_title[ref][0]
    by_stem = {}
    for m in mems:
        if not m.is_foreign:
            by_stem.setdefault(Path(m.filename()).stem, []).append(m)
    if ref in by_stem and len(by_stem[ref]) == 1:
        return by_stem[ref][0]
    hints = [m.title for m in mems
             if ref.lower() in m.title.lower()][:5]
    msg = f"no memory matches {ref!r}"
    if hints:
        msg += f" — did you mean: {', '.join(repr(h) for h in hints)}?"
    raise relations.InvalidRelation(msg)


def _cmd_graph_doctor_action(action: str, args: argparse.Namespace) -> int:
    """Human decisions on the proposal queue and legacy arcs (G2).

    promote/reject/reopen act on ONE proposal by id — the queue never
    decides for you. promote-legacy attests one legacy arc (memory id +
    predicate + target memory id) as manual. All four are explicit human
    attestations; nothing here runs automatically.
    """
    from . import proposals as proposals_mod, relations

    if action in ("promote", "reject", "reopen"):
        proposal_id = getattr(args, "proposal_id", "") or ""
        if not proposal_id:
            print("error: a proposal id is required "
                  "(see `foldcrumbs graph proposals`)", file=sys.stderr)
            return 2
        fn = {"promote": proposals_mod.promote,
              "reject": proposals_mod.reject,
              "reopen": proposals_mod.reopen}[action]
        try:
            res = fn(proposal_id)
        except proposals_mod.ProposalError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        except proposals_mod.ProposalLockBusy as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        note = res.get("note", "")
        if res["action"] == "noop":
            print(f"nothing to do — proposal is {res['status']}"
                  + (f" ({note})" if note else ""))
            return 0
        if action == "promote":
            print(f"promoted — the arc is now in the store with prov=manual "
                  f"(audit trail: proposal {proposal_id} stays in the queue "
                  f"as promoted).")
        else:
            print(f"{action} — proposal is now {res['status']}.")
        return 0

    if action == "promote-legacy":
        mem = _resolve_memory_ref(args.memory)
        dst = _resolve_memory_ref(args.to_memory)
        try:
            ok = relations.promote_legacy_arc(
                mem.id, args.predicate, {"k": "m", "id": dst.id})
        except relations.InvalidRelation as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if ok:
            print(f"attested — {mem.title!r} --{args.predicate}--> "
                  f"{dst.title!r} is now prov=manual.")
        else:
            print("no matching legacy arc (already attested, or the triple "
                  "does not exist).")
        return 0
    print(f"error: unknown doctor action {action!r}", file=sys.stderr)
    return 2


def _cmd_graph(args: argparse.Namespace) -> int:
    """Derive a read-only graph from the relations the store already has,
    or walk/inspect the explicit relations (G1 subcommands)."""
    from . import graph, relations

    mode = getattr(args, "graph_mode", None) or "view"
    if mode == "path":
        src = _resolve_memory_ref(args.src)
        dst = _resolve_memory_ref(args.dst)
        res = relations.find_path(src.id, dst.id, depth=args.depth,
                                  max_nodes=args.max_nodes,
                                  include_inferred=args.include_inferred)
        status = res["status"]
        if status == "FOUND":
            scope = ("manual arcs + inferred overlay"
                     if args.include_inferred else "manual arcs only")
            print(f"FOUND — {len(res['path'])} steps ({scope}):")
            for step in res["path"]:
                edge = step.get("edge")
                # D3-bis: a transit step is never silent about being
                # superseded (marker in the JSON payload AND here).
                mark = (" (superseded — transit)"
                        if step.get("status") == "superseded" else "")
                if edge:
                    ev = edge.get("e", "")
                    # The edge is stored in one direction; a path may walk
                    # it the other way. Say so — evidence is directional.
                    arrow = "--" if step.get("forward", True) else "<--"
                    tail = f"  [{arrow} {edge['p']}, conf {edge['c']}"
                    prov = edge.get("prov")
                    if prov and prov != "manual":
                        tail += f", {prov}"
                    if edge.get("_overlay"):
                        tail += ", pending proposal"
                    tail += f"; evidence: {ev}" if ev else "; no evidence"
                    print(f"  → {step['title']}{mark} "
                          f"({step['file']}){tail}]")
                else:
                    print(f"  • {step['title']}{mark} ({step['file']})")
            return 0
        if status == "NOT_FOUND_EXHAUSTIVE":
            note = res.get("note", "")
            print(f"NOT_FOUND_EXHAUSTIVE — search completed, no connection. "
                  f"Visited {res['reached']} memories.")
            if note:
                print(f"note: {note}")
            return 1
        # TRUNCATED:<reason> — not proof of absence, and we say so.
        print(f"{status} — visited {res['reached']} memories before the "
              f"budget ran out. {res['note']}")
        return 1
    if mode == "transit":
        # D3-bis: the ONLY attestation path for the reserved `transit` key.
        # Human act, explicit, on one superseded memory at a time.
        mem = _resolve_memory_ref(args.memory)
        on = args.transit_action == "on"
        try:
            res = relations.set_transit(mem.id, on=on)
        except relations.InvalidRelation as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        except relations.RelationLockBusy as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        if res["action"] == "noop":
            print(f"nothing to do — transit is already "
                  f"{'on' if res['transit'] else 'off'} for {mem.title!r}.")
        elif on:
            print(f"attested — {mem.title!r} may now act as a transit node "
                  f"in `graph path` (superseded memories are still never "
                  f"endpoints).")
        else:
            print(f"withdrawn — {mem.title!r} is no longer transit-eligible.")
        return 0
    if mode == "doctor":
        action = getattr(args, "doctor_action", None) or "report"
        if action != "report":
            return _cmd_graph_doctor_action(action, args)
        from . import proposals as proposals_mod
        rep = relations.doctor()
        legacy = relations.legacy_arcs()
        qrep = proposals_mod.doctor()
        clean = (not rep["dangling"] and not rep["bad_predicates"]
                 and not legacy and not qrep["promoted_missing_arc"])
        print(f"graph doctor: "
              f"{'clean' if clean else 'findings below'} — "
              f"{len(rep['dangling'])} dangling, "
              f"{len(rep['bad_predicates'])} unknown predicates, "
              f"{len(legacy)} legacy arcs (no prov), "
              f"proposals: {qrep['counts']['pending']} pending / "
              f"{qrep['counts']['promoted']} promoted / "
              f"{qrep['counts']['rejected']} rejected")
        for d in rep["dangling"]:
            print(f"dangling: {d['memory']!r} --{d['predicate']}--> missing "
                  f"memory id {d['missing_target']}")
        for b in rep["bad_predicates"]:
            print(f"unknown predicate: {b['memory']!r} uses {b['predicate']!r}")
        for pid in qrep["promoted_missing_arc"]:
            print(f"ERROR: proposal {pid} is promoted but its arc is missing "
                  "from the store — impossible by construction; manual "
                  "inspection required (E4-bis)")
        if legacy:
            print("legacy arcs (no prov; not walked by default — attest one "
                  "by one with `graph doctor promote-legacy`):")
            for a in legacy[:20]:
                print(f"  {a['memory_id']} --{a['predicate']}--> "
                      f"{a['target_id']}")
            if len(legacy) > 20:
                print(f"  … {len(legacy) - 20} more")
        return 0 if clean else 1
    if mode == "entities":
        ents = relations.external_entities()
        if not ents:
            print("no external entities referenced.")
            return 0
        for e in ents:
            where = ", ".join(e["memories"][:4])
            more = "" if len(e["memories"]) <= 4 else "…"
            print(f"[{e['ns']}] {e['label']}  ({e['count']} refs: "
                  f"{where}{more})")
        if args.similar:
            for a, b in relations.similar_entities():
                print(f"possibly the same entity: {a!r} ~ {b!r} "
                      "(suggestion only — you decide)")
        return 0
    if mode == "proposals":
        from . import proposals as proposals_mod
        rows = proposals_mod.load_all()
        if not rows:
            print("no relation proposals. Distill with FOLDCRUMBS_G2=1 to "
                  "have the model suggest relations.")
            return 0
        by_status = getattr(args, "status", None)
        shown = [r for r in rows if not by_status
                 or r.get("status") == by_status]
        if not shown:
            print(f"no {by_status} proposals.")
            return 0
        # Newest first is what a human deciding promotions wants to see first.
        shown.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        titles = {m.id: m.title for m in store.load_all()}
        for r in shown:
            t = r.get("target") or {}
            sub = str(r.get("subject_id") or "")
            tgt = str(t.get("id") or "")
            ev = (r.get("evidence") or "").strip()
            print(f"[{r.get('status')}] {r.get('proposal_id')}")
            print(f"  {titles.get(sub, sub)!r}"
                  f" --{r.get('predicate')}--> "
                  f"{titles.get(tgt, tgt)!r}"
                  f"  (conf {r.get('confidence')}, {r.get('prov')})")
            if ev:
                print(f"  evidence: {ev}")
        print("\ndecide one at a time:\n"
              "  foldcrumbs graph doctor promote <proposal-id>\n"
              "  foldcrumbs graph doctor reject  <proposal-id>")
        return 0

    g = graph.build()
    project = Path.cwd().name
    if args.format == "mermaid":
        print(graph.render_mermaid(g))
    elif args.format == "dot":
        print(graph.render_dot(g))
    elif args.format == "html":
        page = graph.render_html(g, project)
        if args.out:
            out = Path(args.out).expanduser()
            out.write_text(page, encoding="utf-8")
            print(f"graph written to {out}")
            path = out.resolve()
        else:
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="foldcrumbs-graph-", suffix=".html")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(page)
            path = Path(tmp)
            print(f"graph: {path}")
        if not args.no_open:
            import webbrowser
            webbrowser.open(path.as_uri())
    else:  # default: text edge list
        print(graph.render_text(g), end="")
    return 0


def _cmd_relate(args: argparse.Namespace) -> int:
    """Attach one explicit relation between a memory and a target.

    The source is always a memory (by id/title/filename-stem). The target is
    either another memory (`--to-memory`) or an external entity
    (`--to-entity LABEL` with optional `--namespace`). Exactly one target
    form is required. Refusals (unknown predicate, missing/dangling target,
    locked memory) are printed with the reason and cost a non-zero exit.
    """
    from . import relations
    src = _resolve_memory_ref(args.memory)
    if args.to_memory and args.to_entity:
        print("error: pass exactly one of --to-memory / --to-entity",
              file=sys.stderr)
        return 2
    if args.to_memory:
        dst = _resolve_memory_ref(args.to_memory)
        target = {"k": "m", "id": dst.id}
    elif args.to_entity:
        target = {"k": "x", "ns": args.namespace or "general",
                  "l": args.to_entity}
    else:
        print("error: pass exactly one of --to-memory / --to-entity",
              file=sys.stderr)
        return 2
    try:
        added = relations.add_relation(
            src.id, args.predicate, target,
            evidence=args.evidence or "",
            confidence=args.confidence,
            prov="manual")          # E5: the human CLI attests explicitly
    except relations.InvalidRelation as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except relations.RelationLockBusy as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if added:
        print(f"relation added: {src.title!r} --{args.predicate}--> "
              f"{args.to_memory or args.to_entity}")
    else:
        print("relation already present — nothing written.")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    mems = store.load_all()
    active = [m for m in mems if m.status == "active"]
    expired = [m for m in active if m.is_expired]
    print(f"memory dir : {config.memory_dir()}")
    print(f"index      : {config.index_path()}")
    print(f"memories   : {len(active)} active / {len(mems)} total")
    if expired:
        print(f"expired    : {len(expired)} — invisible, awaiting `foldcrumbs decay`")
    upcoming = sorted((m for m in active
                       if m.expires_at is not None and not m.is_expired),
                      key=lambda m: m.expires_at)
    if upcoming:
        nxt = upcoming[0]
        print(f"next expiry: {nxt.filename()} on {nxt.expires_at.date().isoformat()}")
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
    if config.SEMANTIC:
        print(f"semantic recall: on — {config.EMBEDDING_ENDPOINT} "
              f"(model {config.EMBEDDING_MODEL or config.LLM_MODEL}, "
              f"cache {embeddings.cache_size()})")
    else:
        print("semantic recall: off (lexical only; opt in with FOLDCRUMBS_SEMANTIC=1)")
    print(f"distill here : {'on' if config.distill_enabled() else 'off (read-only consumer)'}")
    print(f"context budget: {config.CONTEXT_BUDGET} @ {int(config.CONTEXT_PCT*100)}%")
    roots = federation.iter_roots()
    print(f"federated roots: {len(roots)} ({federation.roots_dir()})")
    if not any(r.is_current() for r in roots):
        # Upgrading the package does not re-register: hooks run from the
        # runtime snapshot staged at install time, so an upgrade needs
        # `foldcrumbs install` anyway.
        print("           : this instance is not federated — run `foldcrumbs install`")
    for conflict in (federation.mode_conflict(), federation.state_dir_conflict()):
        if conflict:
            print(f"warning    : {conflict}")
    return 0


def _cmd_roots(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "list"
    if action == "add":
        try:
            ref = federation.register(
                Path(args.path) if args.path else None,
                mode=args.mode,
                label=args.label,
            )
        except federation.FederationConflict as exc:
            print(f"refused: {exc}")
            return 1
        if ref is None:
            print("could not register that root (unwritable, or no root to derive)")
            return 1
        print(f"registered {ref.label} ({ref.id}) → {ref.path}")
        return 0

    if action == "remove":
        # Look through iter_roots, not just the shards: the running instance
        # can appear without one (synthesised from its marker) and must still
        # be removable.
        known = {r.id: r for r in federation.iter_roots()}
        ref = known.get(args.root_id)
        if ref is None:
            print(f"no registered root with id {args.root_id!r}")
            return 1
        if not federation.unregister(args.root_id):
            print(f"could not remove {args.root_id}")
            return 1
        # The store is untouched on purpose: unregistering hides a root from the
        # shared view, it does not delete anyone's memory.
        print(f"unregistered {ref.label} ({ref.id}) — its store is untouched")
        return 0

    roots = federation.iter_roots()
    if not roots:
        print("no federated roots registered (run `foldcrumbs install`)")
        return 0
    for r in roots:
        marks = []
        if r.is_current():
            marks.append("current")
        if not r.available():
            marks.append("unavailable")
        if r.mode == "explicit":
            marks.append("explicit-dir")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        mem = r.memory_dir()
        skip = {config.INDEX_NAME, config.HANDOFF_NAME}
        count = (
            sum(1 for p in mem.glob("*.md") if p.name not in skip)
            if mem.is_dir() else 0
        )
        print(f"{r.label:<16} {r.id}  {r.path}{suffix}")
        print(f"{'':<16} this project: {mem} ({count} memories)")
    for conflict in (federation.mode_conflict(), federation.state_dir_conflict()):
        if conflict:
            print(f"\nwarning: {conflict}")
    return 0


#: Reserved frontmatter keys whose only legitimate writer is a local human
#: command: ``transit`` (D3-bis attestation) and the FL-2 outcome loop keys
#: (RT F6). Automatic entry paths (migrate) strip all of them — what
#: survives must come from this store.
_RESERVED_FRONTMATTER_KEYS = frozenset({
    "transit", "outcome", "outcome_at", "outcome_note",
    "contradiction_detected",
})


def _strip_reserved_keys(text: str) -> str:
    """Trust boundary: drop the reserved keys from one memory's frontmatter,
    mirroring the parser EXACTLY.

    Two parser behaviours this must match (GPT code-RT):
    - schema._split_frontmatter partitions each frontmatter line on the
      FIRST colon and strips the key, so ``transit:``, `` transit:``,
      ``transit :`` all parse as the key ``transit`` — matching only the
      literal prefix would let the spaced variants ride through.
    - a frontmatter WITHOUT its closing ``---`` is still parsed to EOF
      (body_start = len(parts)), so the strip must treat a missing closing
      delimiter the same way — never interpret as body what the parser
      treats as metadata.
    """
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        end = len(lines)          # parser: metadata runs to EOF
    kept = []
    for ln in lines[1:end]:
        if ":" in ln and ln.partition(":")[0].strip() in _RESERVED_FRONTMATTER_KEYS:
            continue
        kept.append(ln)
    return "\n".join([lines[0]] + kept + lines[end:])


# Back-compat alias: transit-only callers (and the D3-bis test suite) keep
# working; the boundary now covers the outcome keys too.
_strip_reserved_transit = _strip_reserved_keys


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
                elif item.suffix == ".md":
                    # D3-bis trust boundary: migrate is an automatic entry
                    # path, so the reserved `transit` key never rides in with
                    # a copied memory.
                    text = _strip_reserved_transit(item.read_text(
                        encoding="utf-8"))
                    target.write_text(text, encoding="utf-8")
                else:
                    shutil.copy2(item, target)
            print(f"memory: copied {src} -> {dst}")
    else:
        print(f"memory: (skipped) pass --from <old-project-dir> to copy its store "
              f"into {config.memory_dir()}")
    print("done. reinstall hooks with `foldcrumbs install` if not already.")
    return 0


def _register_root_at_install() -> None:
    """Self-register this instance's root so other instances can see its memory.

    Runs for every agent: the store is keyed on the Claude config dir even when
    the agent is Codex or OpenCode, so there is one root per *instance*, not one
    per agent. Silent no-op when the root can't be represented or written —
    federation is additive, and install must not fail over it.
    """
    ref = federation.register_current()
    if ref is None:
        return
    print(f"federated root: {ref.label} ({ref.id}) → {federation.roots_dir()}")
    for conflict in (federation.mode_conflict(), federation.state_dir_conflict()):
        if conflict:
            print(f"warning: {conflict}")


def _cmd_install(args: argparse.Namespace) -> int:
    agent = args.agent
    _register_root_at_install()
    if agent == "opencode":
        from . import surface
        paths = install.opencode_paths(global_scope=not args.local)
        mcp = install.install_opencode_mcp(paths["config"])
        plugin = install.write_opencode_plugin(paths["plugins"])
        agents = install.append_agents_md(paths["agents"])
        cmds = surface.install_opencode_commands(paths["config"])
        summary = ", ".join(f"/{n} {a}" for n, a in sorted(cmds.items()))
        print(f"opencode.json mcp: {mcp or '(already present)'} ({paths['config']})")
        print(f"plugin: {plugin}")
        print(f"AGENTS.md: {agents or '(block already present)'}")
        print(f"commands: {summary}")
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
        scope = "project" if args.local else "user"
        print(f"claude MCP: {install.install_claude_mcp(scope=scope)}")
        print("(restart open sessions to pick up new commands/skill/MCP)")
    if agent == "codex":
        from . import surface
        print("codex MCP (config.toml):", install.install_codex_mcp_toml())
        actions = surface.install_codex_prompts()
        summary = ", ".join(f"/prompts:{Path(n).stem} {a}"
                            for n, a in sorted(actions.items()))
        print(f"codex prompts: {surface.codex_prompts_dir()} — {summary}")
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
    if args.agent == "opencode":
        # OpenCode has no hooks in a settings.json — its footprint is the
        # opencode.json command/MCP entries (plus plugin/AGENTS.md, left for
        # the user since they may have edited them).
        from . import surface
        paths = install.opencode_paths(global_scope=not args.local)
        removed_cmds = surface.uninstall_opencode_commands(paths["config"])
        print(f"opencode commands removed: {removed_cmds or '(nothing)'}")
        return 0
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
        scope = "project" if args.local else "user"
        print(f"claude MCP: {install.uninstall_claude_mcp(scope=scope)}")
    if args.agent == "codex":
        from . import surface
        print(f"codex prompts removed: {surface.uninstall_codex_prompts() or '(nothing)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="foldcrumbs", description=__doc__)
    p.add_argument("--version", action="version", version=_version_string(),
                   help="show the installed version and exit")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version", help="show the installed version").set_defaults(
        func=_cmd_version)

    r = sub.add_parser("remember", help="store a memory")
    r.add_argument("text")
    r.add_argument("--type", default="fact", choices=sorted(VALID_TYPES))
    r.add_argument("--title", default="")
    r.add_argument("--confidence", type=float, default=0.85)
    r.add_argument("--tag", action="append")
    r.add_argument("--expires", default="",
                   help="true until this date: ISO (2026-09-01) or relative (30d, 2w, 6m)")
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

    ig = sub.add_parser("ingest",
                        help="ingest a document file or URL into memories")
    ig.add_argument("source", help="local file path or http(s) URL")
    ig.set_defaults(func=_cmd_ingest)

    ad = sub.add_parser("adopt",
                        help="adopt ONE memory from a federated root (explicit, never sync)")
    ad.add_argument("ref", nargs="?",
                    help="<root_id>:<memory-file> — ids via `foldcrumbs roots`")
    ad.add_argument("--search", help="list live candidates in a root (adopts nothing)")
    ad.add_argument("--from", dest="from_root", help="root id for --search")
    ad.add_argument("--limit", type=int, default=10, help="max candidates for --search")
    ad.add_argument("--note", help="adoption note stored in the ledger (evidence)")
    ad.add_argument("--as-type", dest="as_type", help="re-type the copy on adoption")
    ad.set_defaults(func=_cmd_adopt)

    oc = sub.add_parser("outcome",
                        help="record good|bad on a memory (the fleet outcome loop)")
    oc.add_argument("memory", nargs="?",
                    help="memory to judge: id, title or filename")
    oc.add_argument("verdict", nargs="?", choices=["good", "bad"],
                    help="good = it held (bumps validation), bad = it burned us")
    oc.add_argument("--note", help="evidence for the verdict (flattened to one line)")
    oc.add_argument("--list", action="store_true",
                    help="list recorded outcomes (adoptions annotated)")
    oc.set_defaults(func=_cmd_outcome)

    sub.add_parser("status", help="show config + stats").set_defaults(func=_cmd_status)

    rt = sub.add_parser("roots", help="list/add/remove federated memory roots")
    rt_sub = rt.add_subparsers(dest="action")
    rt_sub.add_parser("list", help="show every registered root (default)")
    rt_add = rt_sub.add_parser("add", help="register a root (default: this instance)")
    rt_add.add_argument("path", nargs="?", help="config dir to register")
    rt_add.add_argument("--label", help="display name (default: the dir's name)")
    rt_add.add_argument("--mode", choices=list(federation.VALID_MODES),
                        help="'config' (default for a named path) derives "
                             "projects/<cwd>/memory; 'explicit' pins one store")
    rt_rm = rt_sub.add_parser("remove", help="hide a root; its store is untouched")
    rt_rm.add_argument("root_id", help="root id as shown by `foldcrumbs roots`")
    rt.set_defaults(func=_cmd_roots)

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

    sub.add_parser("conflicts", help="show the reconciliation queue "
                   "(ambiguous pairs, claims on other instances, contested memories)"
                   ).set_defaults(func=_cmd_conflicts)

    db = sub.add_parser("dashboard", help="render the store as one "
                        "self-contained HTML page")
    db.add_argument("--json", action="store_true",
                    help="print the dashboard data as JSON instead of HTML")
    db.add_argument("--out", default="",
                    help="write the page to this path instead of a temp file")
    db.add_argument("--no-open", action="store_true",
                    help="do not open the page in a browser")
    db.set_defaults(func=_cmd_dashboard)
    gp = sub.add_parser("graph", help="derive a read-only graph from the "
                        "relations the store already has (supersede chains, "
                        "conflict queue, tag co-occurrence)")
    gp.add_argument("--format", choices=["text", "mermaid", "dot", "html"],
                    default="text",
                    help="text = edge list (default); mermaid/dot for graph "
                         "tools; html = self-contained report page")
    gp.add_argument("--out", default="",
                    help="with --format html: write to this path instead of "
                         "a temp file")
    gp.add_argument("--no-open", action="store_true",
                    help="with --format html: do not open the page")
    gp.set_defaults(func=_cmd_graph, graph_mode=None)
    gsub = gp.add_subparsers(dest="graph_mode")
    gpath = gsub.add_parser("path", help="walk strong edges from one memory "
                            "to another; result is FOUND / "
                            "NOT_FOUND_EXHAUSTIVE / TRUNCATED:<reason>")
    gpath.add_argument("src", help="start: memory id, title or filename stem")
    gpath.add_argument("dst", help="end: memory id, title or filename stem")
    gpath.add_argument("--depth", type=int, default=3,
                       help="max hops (default 3, hard cap 4)")
    gpath.add_argument("--max-nodes", type=int, default=500,
                       help="max memories to visit (default 500)")
    gpath.add_argument("--include-inferred", action="store_true",
                       help="ALSO walk agent/inferred/legacy arcs and pending "
                            "proposals. Default walks manual arcs only: "
                            "every use of this flag is an explicit, conscious "
                            "choice (design G2 amendment — no env var, ever)")
    gpath.set_defaults(func=_cmd_graph)
    gdoc = gsub.add_parser("doctor", help="report dangling memory targets, "
                           "unknown predicates, legacy arcs and the proposal "
                           "queue; or act on ONE proposal / legacy arc")
    gdoc_act = gdoc.add_subparsers(dest="doctor_action")
    for act, helptext in (
            ("promote", "promote ONE pending proposal: writes its arc into "
                        "the store with prov=manual (human attestation)"),
            ("reject", "reject ONE proposal: persistent suppression — the "
                       "triple is not re-proposed until a human reopens it"),
            ("reopen", "reopen ONE rejected proposal (human action only)")):
        pa = gdoc_act.add_parser(act, help=helptext)
        pa.add_argument("proposal_id", help="proposal id from "
                                            "`foldcrumbs graph proposals`")
    pleg = gdoc_act.add_parser(
        "promote-legacy", help="attest ONE legacy arc (no prov) as manual — "
                               "conscious human attestation, one by one")
    pleg.add_argument("memory", help="source memory: id, title or stem")
    pleg.add_argument("predicate",
                      help="the arc's predicate")
    pleg.add_argument("to_memory", help="target memory: id, title or stem")
    gdoc.set_defaults(func=_cmd_graph, doctor_action=None)
    gprop = gsub.add_parser("proposals", help="list the relation proposal "
                            "queue (pending / promoted / rejected)")
    gprop.add_argument("--status", choices=["pending", "promoted", "rejected"],
                       help="only this status (default: all)")
    gprop.set_defaults(func=_cmd_graph)
    gent = gsub.add_parser("entities", help="list external entities "
                           "referenced by relations")
    gent.add_argument("--similar", action="store_true",
                      help="also suggest possibly-duplicate entities "
                           "(suggestion only — no automatic merging)")
    gent.set_defaults(func=_cmd_graph)
    gtr = gsub.add_parser("transit", help="attest (or withdraw) ONE "
                          "superseded memory as transit-eligible for "
                          "`graph path` — the only human attestation for "
                          "walking through superseded nodes (D3-bis)")
    gtr.add_argument("memory", help="the superseded memory: id, title or "
                                    "filename stem")
    gtr.add_argument("transit_action", choices=["on", "off"],
                     help="on = attest transit eligibility; off = withdraw it")
    gtr.set_defaults(func=_cmd_graph)

    rl = sub.add_parser("relate", help="attach one explicit relation to a "
                        "memory (G1; writes are locked per memory)")
    rl.add_argument("memory", help="source memory: id, title or filename stem")
    from . import relations as _rel
    rl.add_argument("predicate",
                    help="one of: " + ", ".join(sorted(_rel.PREDICATES)))
    rl.add_argument("--to-memory", default="",
                    help="target: another memory (id, title or stem)")
    rl.add_argument("--to-entity", default="",
                    help="target: an external entity label")
    rl.add_argument("--namespace", default="general",
                    help="namespace for an external entity (default: general)")
    rl.add_argument("--evidence", default="",
                    help="exact supporting quote; without it the relation is "
                         "recorded as inferred (confidence ≤ 0.5)")
    rl.add_argument("--confidence", type=float, default=0.8)
    rl.set_defaults(func=_cmd_relate)

    pr = sub.add_parser("prune", help="delete pollution / superseded memories (dry-run by default)")
    pr.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    pr.add_argument("--include-stale", action="store_true",
                    help="also prune low-trust memories")
    pr.set_defaults(func=_cmd_prune)

    dc = sub.add_parser("decay", help="archive memories whose trust has decayed "
                                     "(dry-run by default)")
    dc.add_argument("--apply", action="store_true",
                    help="actually archive (default: dry-run)")
    dc.set_defaults(func=_cmd_decay)

    rs = sub.add_parser("restore", help="bring an archived memory back")
    rs.add_argument("name", help="filename of the archived memory")
    rs.set_defaults(func=_cmd_restore)

    pf = sub.add_parser("profile", help="named memory profiles (one per agent "
                                       "or node)")
    pf_sub = pf.add_subparsers(dest="action")
    pf_sub.add_parser("list", help="show every profile (default)")
    pf_add = pf_sub.add_parser("add", help="register a profile")
    pf_add.add_argument("name", help="what to call it")
    pf_add.add_argument("--kind", choices=[profiles.DEDICATED,
                                           profiles.SHARED],
                        default=profiles.DEDICATED,
                        help="'dedicated' keeps one memory dir for every "
                             "project; 'shared' keeps memory per project "
                             "under a config dir")
    pf_add.add_argument("--path", help="where its memory lives (required for "
                                       "a shared profile)")
    pf_imp = pf_sub.add_parser("import", help="give each profile of a "
                                              "multi-agent runtime a memory "
                                              "of its own (dry-run by default)")
    pf_imp.add_argument("--agent", default="hermes",
                        choices=sorted(profiles.AGENT_HOMES),
                        help="which runtime's profiles to read")
    pf_imp.add_argument("--prefix", help="prepend this to every name")
    pf_imp.add_argument("--apply", action="store_true",
                        help="actually register (default: dry-run)")
    pf_imp.set_defaults(func=_cmd_profile)

    pf_env = pf_sub.add_parser("env", help="print the environment line that "
                                          "makes a process use it")
    pf_env.add_argument("name")
    pf_rm = pf_sub.add_parser("remove", help="unregister a profile; its "
                                            "memories are untouched")
    pf_rm.add_argument("name")
    pf.set_defaults(func=_cmd_profile)

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
    # Cheap self-repair for an already-registered root whose shard went missing
    # (wiped state dir, restored backup). Never opts a root in on its own —
    # that stays an explicit act, so `roots remove` is not undone by the next
    # command. Best-effort: a broken registry must not break the CLI.
    try:
        federation.ensure_registered()
    except Exception:  # noqa: BLE001 - registry repair is never worth failing on
        pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
