"""G0 — graph view: derive a read-only graph from relations the store ALREADY has.

No new schema, no new fields, no writes: this module only reads the store and
renders what is already there. Built to answer "is the derived graph saying
anything useful about the real store BEFORE we introduce new schema" (design
REV-2, gate 3). If the answer is no, this is the whole series.

Edges derived:

  * ``superseded_by``  — old -> new, keyed on Memory.id (stable; filenames are
    derived from the mutable title and are display-only)
  * ``conflict``       — flagged pairs from the reconciliation queue, both
    sides still live (conflicts.flagged_pairs already prunes dead sides)
  * ``tag``            — WEAK edge: two memories sharing 2+ tags. Kept out of
    path queries entirely (design §BFS); rendered here so the reader can see
    clusters form, nothing more.
  * ``explicit``       — G1 relations written in frontmatter (relations_json),
    labelled with the predicate (caused_by, depends_on, ...). Memory targets
    only and only when both ends exist; external entities belong to
    `graph entities`. Malformed JSON degrades to no edge, never to an error.

Determinism is a design principle (REV-2 §5): same store, same bytes. Every
collection is sorted on stable keys before rendering — no dict-order leaks,
no set-iteration luck.
"""
from __future__ import annotations

import html
import os
from dataclasses import dataclass, field

from . import conflicts, relations, store

# The weak-tag edge is noise below this: sharing one tag is the norm for any
# coherent project, sharing two starts meaning the memories move together.
TAG_EDGE_MIN_SHARED = 2


@dataclass
class Node:
    """One memory as a graph node. Identity is the Memory.id, never the
    filename — a retitle renames the file but not the node."""

    id: str
    title: str
    type: str
    status: str
    tags: tuple[str, ...] = ()

    def label(self) -> str:
        return self.title or self.id[:8]


@dataclass
class Edge:
    """A directed relation. ``kind``: superseded_by | conflict | tag | explicit.

    ``weight`` matters only for weak tag edges (count of shared tags).
    ``label`` matters only for explicit G1 edges (the predicate, e.g.
    ``caused_by``); for every other kind it stays empty.
    """

    kind: str
    src: str   # node id
    dst: str   # node id
    weight: int = 1
    label: str = ""


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def by_id(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def strong_edges(self) -> list[Edge]:
        return [e for e in self.edges if e.kind != "tag"]

    def weak_edges(self) -> list[Edge]:
        return [e for e in self.edges if e.kind == "tag"]

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "superseded": sum(1 for e in self.edges if e.kind == "superseded_by"),
            "conflict": sum(1 for e in self.edges if e.kind == "conflict"),
            "tag": len(self.weak_edges()),
            "explicit": sum(1 for e in self.edges if e.kind == "explicit"),
        }


def build(cwd: str | os.PathLike[str] | None = None) -> Graph:
    """Collect every memory (superseded ones included: they are the very
    points of the supersede chain) plus the reconciliation queue, and derive
    the edge set. Pure read; deterministic output for a given store."""
    mems = store.load_all(cwd)
    # Conflicts are keyed by name; map name -> memory so edges land on ids.
    # store.get() resolves by filename/title for active records; superseded
    # ones fall back to a linear scan.
    by_name: dict[str, list] = {}
    for m in mems:
        by_name.setdefault(m.filename(), []).append(m)
        by_name.setdefault(m.title, []).append(m)

    nodes = [
        Node(id=m.id, title=m.title, type=m.type, status=m.status,
             tags=tuple(sorted(m.tags)))
        for m in sorted(mems, key=lambda r: r.id)
    ]
    ids = {m.id for m in mems}
    edges: list[Edge] = []

    # 1) supersede chains: old -> new, only when both ends still exist.
    for m in mems:
        if m.superseded_by and m.superseded_by in ids:
            edges.append(Edge("superseded_by", m.id, m.superseded_by))

    # 2) reconciliation queue: old/new are names; resolve to ids when both
    # sides are present in this store. Foreign-root sides are out of scope
    # for the local graph (they belong to another store's own graph).
    for p in conflicts.flagged_pairs(cwd):
        old = by_name.get(p.get("old", ""), [])
        new = by_name.get(p.get("new", ""), [])
        if not p.get("old_root") and old and new:
            a, b = sorted({old[0].id, new[0].id})
            if a != b:
                edges.append(Edge("conflict", a, b))

    # 3) weak tag co-occurrence, deterministic pair order.
    tagged = [m for m in sorted(mems, key=lambda r: r.id) if len(m.tags) >= TAG_EDGE_MIN_SHARED]
    for i, a in enumerate(tagged):
        for b in tagged[i + 1:]:
            shared = len(set(a.tags) & set(b.tags))
            if shared >= TAG_EDGE_MIN_SHARED:
                edges.append(Edge("tag", a.id, b.id, weight=shared))

    # 4) explicit G1 relations from frontmatter (relations_json). Memory
    # targets only, and only when the target still exists — same no-dangling
    # rule as superseded_by. External entities (k:"x") belong to
    # `graph entities`, not to this edge set. relations.parse() is tolerant:
    # malformed JSON degrades to [], never to an error.
    for m in mems:
        for r in relations.parse(m.relations_json):
            t = r.get("t")
            # parse() only checks truthiness of "t": hand-written frontmatter
            # can carry any JSON value here. A non-dict target is one
            # malformed relation — skip it, never blind the whole graph (RT F1).
            if not isinstance(t, dict) or t.get("k") != "m":
                continue
            dst = str(t.get("id") or "")
            if dst in ids and dst != m.id:
                edges.append(Edge("explicit", m.id, dst,
                                  label=str(r.get("p") or "")))

    edges.sort(key=lambda e: (e.kind, e.src, e.dst, e.label))
    return Graph(nodes=nodes, edges=edges)


# --- rendering ---------------------------------------------------------------

def _short(nid: str) -> str:
    return nid[:8]


def render_text(g: Graph) -> str:
    """Primary format: plain edge list, pipe-friendly and test-friendly."""
    c = g.counts()
    lines = [
        f"# graph: {c['nodes']} nodes | {c['superseded']} superseded_by | "
        f"{c['conflict']} conflict | {c['tag']} tag (weak) | "
        f"{c['explicit']} explicit"
    ]
    nodes = g.by_id()
    if not g.edges:
        lines.append("# (no relations derived from this store yet)")
        return "\n".join(lines) + "\n"
    for e in g.edges:
        a, b = nodes[e.src], nodes[e.dst]
        if e.kind == "tag":
            extra = f" (x{e.weight})"
        elif e.kind == "explicit":
            extra = f" ({e.label})" if e.label else ""
        else:
            extra = ""
        lines.append(f"{a.label()} --{e.kind}{extra}--> {b.label()}")
    return "\n".join(lines) + "\n"


def _esc_mermaid(label: str) -> str:
    # mermaid labels inside "..." must not contain a quote
    return label.replace('"', "'")


def _mermaid_edge_label(label: str) -> str:
    # Edge labels sit between pipes (==>|text|): a pipe or a newline in a
    # hand-written predicate would escape the label and inject syntax
    # (RT F2). Quote the label and flatten newlines — mermaid accepts
    # ==>|"text"| and treats the content as literal text.
    flat = " ".join(label.split())
    return f'"{_esc_mermaid(flat)}"'


def _esc_dot(label: str) -> str:
    # DOT quoted strings: escape backslashes FIRST, then quotes — a
    # predicate ending in a backslash would otherwise eat the closing
    # quote and produce an unparsable document (RT F3, Graphviz rc=1).
    return label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_mermaid(g: Graph) -> str:
    lines = ["graph LR"]
    for n in sorted(g.nodes, key=lambda x: x.id):
        lines.append(f'    {_short(n.id)}["{_esc_mermaid(n.label())}"]')
    for e in g.edges:
        if e.kind == "superseded_by":
            lines.append(f"    {_short(e.src)} -->|superseded by| {_short(e.dst)}")
        elif e.kind == "conflict":
            lines.append(f"    {_short(e.src)} -.->|conflict| {_short(e.dst)}")
        elif e.kind == "explicit":
            pred = _mermaid_edge_label(e.label or "related")
            lines.append(f"    {_short(e.src)} ==>|{pred}| {_short(e.dst)}")
        else:
            lines.append(f"    {_short(e.src)} -.-|tag x{e.weight}| {_short(e.dst)}")
    return "\n".join(lines) + "\n"


def render_dot(g: Graph) -> str:
    lines = ["digraph foldcrumbs {", "    rankdir=LR;",
             '    node [shape=box, fontname="monospace"];']
    for n in sorted(g.nodes, key=lambda x: x.id):
        label = n.label().replace('"', '\\"')
        lines.append(f'    "{_short(n.id)}" [label="{label}"];')
    for e in g.edges:
        if e.kind == "explicit":
            pred = _esc_dot(e.label or "related")
            style = f' [penwidth=2, color=orange, label="{pred}"]'
        else:
            style = {"superseded_by": "", "conflict": ' [style=dashed, color=red]',
                     "tag": ' [style=dotted, color=gray]'}[e.kind]
        lines.append(f'    "{_short(e.src)}" -> "{_short(e.dst)}"{style};')
    lines.append("}")
    return "\n".join(lines) + "\n"


_CSS = """
body{background:#0b0e11;color:#c9d1d9;font-family:ui-monospace,Menlo,monospace;
margin:0;padding:24px;font-size:13px;line-height:1.5}
h1{font-size:17px;margin:0 0 4px}
.sub{color:#7d8590;margin-bottom:18px}
.panel{border:1px solid #21262d;border-radius:8px;padding:14px 16px;margin:14px 0}
.panel h2{font-size:13px;margin:0 0 10px;color:#58a6ff}
table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #21262d;padding:4px 8px;text-align:left;vertical-align:top}
th{color:#7d8590;font-weight:normal}
.kind{color:#8b949e}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
border:1px solid #30363d;margin-right:6px}
a{color:#58a6ff;text-decoration:none}
"""


def render_html(g: Graph, project: str = "") -> str:
    """Self-contained report page. Honest per design REV-2: this is a TABLE
    report, not an interactive visualization — no scripts, no external refs,
    so it opens offline and the documentation does not oversell it."""
    c = g.counts()
    nodes = g.by_id()
    e = html.escape

    def row_edge(ed: Edge) -> str:
        a, b = nodes[ed.src], nodes[ed.dst]
        if ed.kind == "tag":
            w = f" x{ed.weight}"
        elif ed.kind == "explicit" and ed.label:
            w = f" ({ed.label})"
        else:
            w = ""
        return (f"<tr><td>{e(a.label())}</td>"
                f"<td class='kind'>{e(ed.kind)}{e(w)}</td>"
                f"<td>{e(b.label())}</td>"
                f"<td>{e(a.type)} / {e(b.type)}</td></tr>")

    strong = [ed for ed in g.edges if ed.kind != "tag"]
    weak = g.weak_edges()
    strong_rows = "\n".join(row_edge(ed) for ed in strong) or \
        "<tr><td colspan='4' class='kind'>none derived yet</td></tr>"
    weak_rows = "\n".join(row_edge(ed) for ed in weak) or \
        "<tr><td colspan='4' class='kind'>none</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>foldcrumbs graph — {e(project)}</title>
<style>{_CSS}</style></head><body>
<h1>foldcrumbs graph</h1>
<div class="sub">{e(project)} — derived from relations the store already has
(supersede chains, reconciliation queue, explicit frontmatter relations, tag
co-occurrence). Read-only: this page writes nothing.</div>
<div class="panel"><h2>Summary</h2>
<span class="badge">{c['nodes']} memories</span>
<span class="badge">{c['superseded']} supersede edges</span>
<span class="badge">{c['conflict']} conflict edges</span>
<span class="badge">{c['explicit']} explicit relations</span>
<span class="badge">{c['tag']} weak tag edges</span>
</div>
<div class="panel"><h2>Strong edges (supersede + conflict + explicit relations)</h2>
<table><tr><th>from</th><th>relation</th><th>to</th><th>types</th></tr>
{strong_rows}</table></div>
<div class="panel"><h2>Weak tag edges (2+ shared tags — clustering hint only)</h2>
<table><tr><th>from</th><th>relation</th><th>to</th><th>types</th></tr>
{weak_rows}</table></div>
</body></html>"""
