"""One self-contained HTML dashboard over the live store — stdlib only.

Every panel renders a number that is true right now, computed by the same
functions the CLI uses — no invented telemetry, no decoration: whatever the
store holds is what appears, and each row that names a memory links to the
actual file on disk. A dashboard that could not answer "which memory is
this?" would be the exact opposite of what this project is about.

Data and rendering are separate on purpose: ``collect()`` returns a plain
JSON-serializable dict (testable headless, and the ``--json`` output), and
``render()`` turns it into one HTML page with no external references — no
CDN, no font fetch, no script src — so it works offline and never phones
home. The page opens in a browser; it is a report, not an app.

Panels for features that live on not-yet-merged branches (expiry, the
conflicts queue) are discovered at runtime and simply stay absent until
those modules exist — this file must build on any main, never crash on a
missing import.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from . import audit, config, federation, recalls, store


def _age_days(path: Path) -> float | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return round((datetime.now(timezone.utc).timestamp() - mtime) / 86400, 1)


def _collect_store(mems: list) -> dict:
    by_status: dict[str, int] = {}
    for m in mems:
        by_status[m.status] = by_status.get(m.status, 0) + 1
    d: dict = {
        "dir": str(config.memory_dir()),
        "total": len(mems),
        "by_status": by_status,
    }
    # Expiry panel (feature-detected: the field ships on its own branch).
    active = [m for m in mems if m.status == "active"]
    expired = [m for m in active if getattr(m, "is_expired", False)]
    upcoming = sorted(
        (m for m in active
         if getattr(m, "expires_at", None) is not None
         and not getattr(m, "is_expired", False)),
        key=lambda m: m.expires_at)
    if expired or upcoming:
        d["expiry"] = {
            "lapsed": [_label(m) for m in expired],
            "next": (_label(upcoming[0]), upcoming[0].expires_at.isoformat())
            if upcoming else None,
        }
    return d


def _label(m) -> str:
    return m.source_path or m.filename()


def _collect_decay(mems: list) -> dict:
    # Same predicate as `foldcrumbs decay` (dry-run): the point of the panel
    # is "what would the sweep archive if I ran it now", nothing else.
    res = audit.decay(apply=False)
    return {
        "candidates": [{"name": n, "trust": c}
                       for n, c in sorted(res["candidates"].items())],
        "expired": res.get("expired", []),
    }


def _collect_superseded(mems: list) -> list[dict]:
    """Retired memories that point at their replacement — the pruning backlog."""
    out = []
    for m in mems:
        if m.status == "superseded" and m.superseded_by:
            replacement = next(
                (x for x in mems if x.id == m.superseded_by), None)
            out.append({
                "old": _label(m),
                "new": _label(replacement) if replacement else m.superseded_by,
                "found": replacement is not None,
            })
    return out


def _collect_roots(cwd=None) -> list[dict]:
    from . import index_shard
    out = []
    for ref in federation.iter_roots():
        shard = index_shard.shard_path(ref.id, cwd)
        entries = age = None
        if shard is not None and shard.exists():
            age = _age_days(shard)
            try:
                entries = len(json.loads(
                    shard.read_text(encoding="utf-8")).get("entries", []))
            except (OSError, ValueError):
                entries = None
        out.append({
            "label": ref.label,
            "path": str(ref.path),
            "current": ref.is_current(),
            "entries": entries,
            "shard_age_days": age,
        })
    return out


def _collect_reinforcement(mems: list, cwd=None) -> dict:
    counts = recalls.counts(cwd)
    named = []
    for m in mems:
        if m.status != "active":
            continue
        n = counts.get(m.id, 0)
        named.append((n, _label(m)))
    named.sort(key=lambda t: (-t[0], t[1]))
    return {
        "total_recalls": sum(counts.values()),
        "top": [{"name": name, "count": n} for n, name in named[:8] if n > 0],
        "never_recalled": sum(1 for n, _ in named if n == 0),
    }


def _collect_trust(mems: list) -> dict:
    """Confidence distribution of active memories, by bucket and by type."""
    buckets = [0] * 5
    by_type: dict[str, list[float]] = {}
    for m in mems:
        if m.status != "active":
            continue
        c = m.compute_confidence()
        buckets[min(int(c * 5), 4)] += 1
        by_type.setdefault(m.type, []).append(c)
    return {
        "buckets": buckets,   # 0-.2 ... .8-1
        "by_type": {t: {"count": len(v),
                        "avg": round(sum(v) / len(v), 2)}
                    for t, v in sorted(by_type.items())},
    }


def _collect_recent(mems: list, limit: int = 10) -> list[dict]:
    """The newest active memories — the browsable face of the store.

    Without this, a healthy store (nothing to decay, supersede or flag) would
    render aggregates only, and there would be nothing on the page that names
    an actual memory. Same order the index uses: created_at, newest first.
    """
    active = [m for m in mems if m.status == "active"]
    active.sort(key=lambda m: (m.created_at, m.filename()), reverse=True)
    return [{"name": _label(m), "title": m.title, "type": m.type,
             "created_at": m.created_at.isoformat()}
            for m in active[:limit]]


def _collect_anti_rot(cwd=None) -> dict:
    handoff = config.memory_dir(cwd) / config.HANDOFF_NAME
    checkpoints = 0
    try:
        checkpoints = len(list(config.STATE_DIR.glob("state-*.json")))
    except OSError:
        pass
    semantic = None
    try:
        from . import embeddings
        semantic = {"on": config.SEMANTIC,
                    "cache": embeddings.cache_size() if config.SEMANTIC else 0}
    except Exception:
        pass
    return {
        "budget": config.CONTEXT_BUDGET,
        "pct": config.CONTEXT_PCT,
        "handoff_age_days": _age_days(handoff),
        "checkpoint_flags": checkpoints,
        "semantic": semantic,
    }


def _collect_conflicts(cwd=None) -> dict | None:
    """Feature-detected: the queue module ships on its own branch."""
    try:
        from . import conflicts as conflicts_mod
    except ImportError:
        return None
    q = conflicts_mod.queue(cwd)
    return {k: len(v) for k, v in q.items()}


def collect(cwd: str | None = None) -> dict:
    """The whole dashboard as plain data. Nothing here writes anything."""
    mems = store.load_all(cwd)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "store": _collect_store(mems),
        "decay": _collect_decay(mems),
        "superseded": _collect_superseded(mems),
        "roots": _collect_roots(cwd),
        "reinforcement": _collect_reinforcement(mems, cwd),
        "recent": _collect_recent(mems),
        "trust": _collect_trust(mems),
        "anti_rot": _collect_anti_rot(cwd),
        "conflicts": _collect_conflicts(cwd),
    }


def _file_link(path_str: str) -> str:
    """Link to a real file when it is one; plain text otherwise.

    Store-relative names (the usual case: filenames as shown in MEMORY.md)
    are resolved against the memory directory, so the link actually points
    at the memory — a dead link would be the opposite of the point.
    """
    p = Path(path_str)
    if not p.exists() and not p.is_absolute():
        p = config.memory_dir() / path_str
    if p.exists():
        return f'<a href="file://{html.escape(str(p.resolve()))}">{html.escape(p.name)}</a>'
    return html.escape(path_str)


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"<tr><td>{html.escape(str(k))}</td>"
                   f"<td>{v}</td></tr>" for k, v in pairs)


# --- rendering (v2) --------------------------------------------------------
#
# One hero, then an asymmetric bento grid with a visual hierarchy — not a
# wall of equal boxes. The heartbeat at the center is not decoration: its
# tempo is derived from the store's real recall activity, so a heavily-used
# memory store visibly pulses faster than a dormant one. CSS animations only
# (the page stays script-free), and everything the page shows is still a
# number computed live from the store.

_CSS = """
:root{--bg:#0a0d12;--panel:#10151c;--panel2:#131a23;--line:#1d2733;
--txt:#c9d4de;--dim:#71808f;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;
--bad:#f85149;--glow:#58a6ff33}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--txt);margin:0;padding:32px 28px 48px;
font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.wrap{max-width:1120px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:20px}
.top h1{font-size:15px;letter-spacing:.16em;margin:0;color:#e6edf3}
.top h1 b{color:var(--acc)}
.top .gen{color:var(--dim);font-size:11px}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;position:relative;overflow:hidden}
.panel::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
background:var(--acc)}
.panel.ok::before{background:var(--ok)}
.panel.warn::before{background:var(--warn)}
.panel.bad::before{background:var(--bad)}
.panel h2{font-size:10px;letter-spacing:.22em;text-transform:uppercase;
color:var(--dim);margin:0 0 12px;display:flex;justify-content:space-between;
align-items:center}
.badge{font-size:9px;letter-spacing:.14em;padding:2px 8px;border-radius:20px;
border:1px solid currentColor}
.badge.ok{color:var(--ok)}.badge.warn{color:var(--warn)}.badge.bad{color:var(--bad)}
.big{font-size:30px;font-weight:600;color:#e6edf3;line-height:1.1}
.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:12px}
td{padding:3px 8px 3px 0;vertical-align:top}
td:last-child{text-align:right;color:var(--acc)}
a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline;color:#79c0ff}
code{background:#161d26;border:1px solid var(--line);border-radius:4px;
padding:0 5px;font-size:11px;color:#a5d6ff}
/* hero */
.hero{grid-column:span 12;display:flex;align-items:center;gap:34px;
background:linear-gradient(135deg,var(--panel) 0%,var(--panel2) 100%)}
.pulse{position:relative;width:118px;height:118px;flex:0 0 118px}
.pulse .core{position:absolute;inset:34px;border-radius:50%;
background:radial-gradient(circle,#79c0ff 0%,var(--acc) 55%,transparent 72%);
animation:beat var(--beat,2.8s) ease-in-out infinite}
.pulse .ring{position:absolute;inset:0;border-radius:50%;
border:1px solid var(--glow);animation:ripple var(--beat,2.8s) ease-out infinite}
.pulse .ring.r2{animation-delay:calc(var(--beat,2.8s) / -2)}
@keyframes beat{0%,100%{transform:scale(.86);opacity:.75}
45%{transform:scale(1.06);opacity:1}}
@keyframes ripple{0%{transform:scale(.55);opacity:.9}
100%{transform:scale(1.18);opacity:0}}
.hero .stats{flex:1;display:flex;gap:44px;flex-wrap:wrap;align-items:center}
.stat .v{font-size:34px;font-weight:600;color:#e6edf3}
.stat .k{font-size:10px;letter-spacing:.2em;text-transform:uppercase;
color:var(--dim);margin-top:2px}
.hero .path{font-size:11px;color:var(--dim);word-break:break-all;margin-top:10px}
/* spans */
.s3{grid-column:span 3}.s4{grid-column:span 4}.s5{grid-column:span 5}
.s6{grid-column:span 6}.s8{grid-column:span 8}.s12{grid-column:span 12}
@media(max-width:900px){.s3,.s4,.s5,.s6,.s8{grid-column:span 12}
.hero{flex-direction:column;align-items:flex-start}}
/* trust bars */
.tbar{display:flex;align-items:center;gap:8px;margin:3px 0}
.tbar .lbl{width:52px;color:var(--dim);font-size:11px;flex:0 0 52px}
.tbar .tr{flex:1;height:9px;background:#161d26;border-radius:5px;overflow:hidden}
.tbar .fill{height:100%;background:linear-gradient(90deg,#2ea043,#3fb950);
border-radius:5px}
.tbar .n{width:26px;text-align:right;font-size:11px;color:var(--txt);
flex:0 0 26px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;
margin-right:7px;vertical-align:middle}
.dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.dot.off{background:#3d4450}
footer{margin-top:26px;color:var(--dim);font-size:11px;text-align:center;
letter-spacing:.06em}
"""


def _pulse_seconds(data: dict) -> float:
    """Heartbeat tempo from real recall activity — alive, not decorative.

    A dormant store breathes slowly (6 s); each recorded recall quickens it,
    down to a floor of 1.6 s. Same store, same tempo — deterministic, and the
    number it depends on is the one the Recall panel prints.
    """
    total = data["reinforcement"]["total_recalls"]
    return max(1.6, 6.0 - min(total, 44) * 0.1)


def _health(data: dict) -> tuple[str, str]:
    """Store health → (badge class, label), from real conditions only."""
    cq = data.get("conflicts") or {}
    if cq.get("flagged") or cq.get("contested_here"):
        return "bad", "attention"
    if data["decay"]["candidates"] or data["store"].get("expiry", {}).get("lapsed"):
        return "warn", "needs a sweep"
    return "ok", "current"


def render(data: dict) -> str:
    """One self-contained page: inline CSS, no scripts, no external refs."""
    st = data["store"]
    by = st["by_status"]
    active = by.get("active", 0)
    beat = _pulse_seconds(data)
    badge_cls, badge_txt = _health(data)
    ar = data["anti_rot"]
    handoff = (f"{ar['handoff_age_days']}d" if ar["handoff_age_days"] is not None
               else "none")
    sem = ar.get("semantic") or {}
    panels: list[str] = []

    # HERO — the living center: total, active, recalls, federation, tempo
    panels.append(f"""<div class="panel hero" style="--beat:{beat:.1f}s">
<div class="pulse"><div class="ring"></div><div class="ring r2"></div>
<div class="core"></div></div>
<div style="flex:1">
<div class="stats">
<div class="stat"><div class="v">{st['total']}</div><div class="k">memories</div></div>
<div class="stat"><div class="v">{active}</div><div class="k">active</div></div>
<div class="stat"><div class="v">{data['reinforcement']['total_recalls']}</div>
<div class="k">recalls</div></div>
<div class="stat"><div class="v">{len(data['roots'])}</div>
<div class="k">federated roots</div></div>
<div class="stat"><div class="v">{beat:.1f}s</div><div class="k">pulse</div></div>
</div>
<div class="hero path">{html.escape(st['dir'])}</div>
</div>
<span class="badge {badge_cls}" style="align-self:flex-start">{badge_txt}</span>
</div>""")

    # RECALL — reinforcement (span 5)
    rf = data["reinforcement"]
    if rf["top"]:
        body = "".join(
            f"<tr><td>{_file_link(t['name'])}</td><td>{t['count']}</td></tr>"
            for t in rf["top"])
        panels.append(f"""<div class="panel ok s5"><h2>Recall — reinforcement
<span class="badge ok">live</span></h2>
<div class="dim">{rf['never_recalled']} memories never recalled</div>
<table>{body}</table></div>""")
    else:
        panels.append(f"""<div class="panel s5"><h2>Recall — reinforcement</h2>
<div class="big">{rf['total_recalls']}</div>
<div class="dim">no recalls recorded yet</div></div>""")

    # FEDERATION — roots (span 7)
    roots = data["roots"]
    if roots:
        lines = []
        for r in roots:
            dot = '<span class="dot on"></span>' if r["current"] else \
                  '<span class="dot off"></span>'
            entries = "" if r["entries"] is None else f"{r['entries']} entries"
            age = ("" if r["shard_age_days"] is None
                   else f' · shard {r["shard_age_days"]}d old')
            lines.append(
                f"<tr><td>{dot}{html.escape(r['label'])}<br>"
                f'<span class="dim">{html.escape(r["path"])}</span></td>'
                f"<td>{entries}{age}</td></tr>")
        panels.append(f"""<div class="panel s8"><h2>Federation — parallel roots
<span class="badge ok">{len(roots)} roots</span></h2>
<table>{''.join(lines)}</table></div>""")
    else:
        panels.append("""<div class="panel s8"><h2>Federation — parallel roots</h2>
<div class="big">0</div><div class="dim">no roots registered</div></div>""")

    # TRUST — histogram (span 4)
    buckets = data["trust"]["buckets"]
    peak = max(buckets) or 1
    bars = "".join(
        f'<div class="tbar"><span class="lbl">{lo}–{hi}</span>'
        f'<div class="tr"><div class="fill" style="width:{int(b / peak * 100)}%">'
        f"</div></div><span class='n'>{b}</span></div>"
        for b, (lo, hi) in zip(buckets, [(".0", ".2"), (".2", ".4"), (".4", ".6"),
                                         (".6", ".8"), (".8", "1.0")]))
    types = "".join(
        f"<tr><td>{html.escape(t)}</td><td>{v['count']} · avg {v['avg']}</td></tr>"
        for t, v in data["trust"]["by_type"].items())
    panels.append(f"""<div class="panel s4"><h2>Trust</h2>{bars}
<table style="margin-top:10px">{types}</table></div>""")

    # DECAY (span 4)
    cands = data["decay"]["candidates"]
    expired_names = set(data["decay"]["expired"])
    if cands:
        body = "".join(
            f"<tr><td>{_file_link(c['name'])}</td>"
            f"<td>{'expired' if c['name'] in expired_names else c['trust']}</td></tr>"
            for c in cands)
        panels.append(f"""<div class="panel warn s4"><h2>Decay
<span class="badge warn">{len(cands)}</span></h2>
<div class="dim">would be archived by <code>decay --apply</code></div>
<table>{body}</table></div>""")
    else:
        panels.append("""<div class="panel ok s4"><h2>Decay
<span class="badge ok">clean</span></h2>
<div class="big">0</div><div class="dim">nothing to archive — the store is current</div>
</div>""")

    # ANTI-ROT (span 4)
    sem_line = (f"<tr><td>semantic channel</td>"
                f"<td>{'on' if sem.get('on') else 'off'} · cache {sem.get('cache', 0)}"
                "</td></tr>" if sem else "")
    panels.append(f"""<div class="panel s4"><h2>Anti-rot</h2><table>{_rows([
        ('context budget', str(ar['budget'])),
        ('checkpoint at', f"{int(ar['pct'] * 100)}%"),
        ('handoff age', handoff),
        ('checkpoint flags', str(ar['checkpoint_flags'])),
    ])}{sem_line}</table></div>""")

    # SUPERSEDED (span 4)
    chains = data["superseded"]
    if chains:
        body = "".join(
            f"<tr><td>{_file_link(c['old'])} → "
            f"{_file_link(c['new']) if c['found'] else html.escape(c['new'])}</td>"
            "<td></td></tr>"
            for c in chains)
        panels.append(f"""<div class="panel warn s4"><h2>Superseded
<span class="badge warn">{len(chains)}</span></h2>
<div class="dim">replaced memories kept on disk (<code>prune</code> clears)</div>
<table>{body}</table></div>""")
    else:
        panels.append("""<div class="panel ok s4"><h2>Superseded
<span class="badge ok">none</span></h2>
<div class="big">0</div><div class="dim">no retired memories pending prune</div></div>""")

    # EXPIRY (span 4, feature-detected)
    exp = st.get("expiry")
    if exp:
        nxt = (f"{_file_link(exp['next'][0])} on {exp['next'][1][:10]}"
               if exp["next"] else "—")
        cls = "warn" if exp["lapsed"] else "ok"
        panels.append(f"""<div class="panel {cls} s4"><h2>Expiry
<span class="badge {cls}">{len(exp['lapsed'])} lapsed</span></h2>
<table>{_rows([('next to expire', nxt)])}</table></div>""")

    # CONFLICTS (span 4, feature-detected)
    cq = data.get("conflicts")
    if cq is not None and any(cq.values()):
        panels.append(f"""<div class="panel bad s4"><h2>Conflicts
<span class="badge bad">open</span></h2><table>{_rows([
            ('ambiguous pairs', str(cq['flagged'])),
            ('claims out', str(cq['claims_out'])),
            ('contested here', str(cq['contested_here'])),
        ])}</table>
<div class="dim" style="margin-top:6px"><code>foldcrumbs conflicts</code></div>
</div>""")

    # RECENT — the browsable face of the store (full width)
    if data["recent"]:
        body = "".join(
            f"<tr><td>{_file_link(r['name'])}<br>"
            f'<span class="dim">{html.escape(r["title"])}</span></td>'
            f'<td><span class="dim">{html.escape(r["type"])}</span><br>'
            f"{r['created_at'][:10]}</td></tr>"
            for r in data["recent"])
        panels.append(f"""<div class="panel ok s12"><h2>Latest memories
<span class="badge ok">{len(data['recent'])}</span></h2>
<table>{body}</table></div>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>foldcrumbs dashboard</title>
<style>{_CSS}</style></head><body><div class="wrap">
<div class="top"><h1><b>FOLDCRUMBS</b> // MEMORY DASHBOARD</h1>
<div class="gen">generated {html.escape(data['generated_at'])} — every number
live from the store · file names link to the real files</div></div>
<div class="grid">{''.join(panels)}</div>
<footer>foldcrumbs — one folder of typed memory · no engine · no vector DB
· the pulse tempo follows your store's real recall activity</footer>
</div></body></html>"""

