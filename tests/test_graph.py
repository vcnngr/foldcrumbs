"""Tests for G0 — the read-only derived graph (foldcrumbs.graph).

The contract under test (design REV-2, gate 3):
* build() is pure — it reads the store, writes nothing;
* output is deterministic — same store, same bytes (no dict-order or
  set-iteration leaks);
* supersede edges are keyed on Memory.id (stable), and only drawn when both
  ends still exist (no dangling arrows);
* conflict edges come from the live reconciliation queue only;
* weak tag edges appear only at 2+ shared tags and are kept separate from
  strong edges;
* every renderer (text/mermaid/dot/html) emits a self-contained artifact —
  the HTML page carries no external references.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sandbox so we never touch the developer's real store.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import conflicts, graph, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class TestGraphBuild(TmpStore):
    def _put(self, title, body, type_="fact", tags=None):
        rec = MemoryRecord(title=title, content=body, type=type_,
                           tags=list(tags or []))
        store.write_memory(rec)
        return rec

    def test_empty_store_yields_empty_graph(self):
        g = graph.build()
        self.assertEqual(g.counts()["nodes"], 0)
        self.assertEqual(g.edges, [])

    def test_build_is_read_only(self):
        self._put("One", "First.")
        before = sorted(p.name for p in Path(self.dir).iterdir())
        graph.build()
        after = sorted(p.name for p in Path(self.dir).iterdir())
        self.assertEqual(before, after, "build() must not write anything")

    def test_supersede_edge_keyed_on_ids_both_ends(self):
        old = self._put("Old rule", "Do A.")
        new = self._put("New rule", "Do B.")
        self.assertTrue(store.supersede(old.filename(), new.filename()))
        g = graph.build()
        self.assertEqual(g.counts()["superseded"], 1)
        edge = [e for e in g.edges if e.kind == "superseded_by"][0]
        self.assertEqual(edge.src, old.id)
        self.assertEqual(edge.dst, new.id, "arrow must land on the new memory id")

    def test_supersede_edge_dropped_when_target_missing(self):
        old = self._put("Old rule", "Do A.")
        new = self._put("New rule", "Do B.")
        store.supersede(old.filename(), new.filename())
        # Remove the new side; the edge must not dangle.
        store.forget(new.filename(), hard=True)
        g = graph.build()
        self.assertEqual(g.counts()["superseded"], 0)

    def test_conflict_edge_from_live_queue(self):
        a = self._put("Auth token", "Use JWT.")
        b = self._put("Auth token 2", "Use session cookie.")
        conflicts.flag_pair(a.filename(), b.filename())
        g = graph.build()
        self.assertEqual(g.counts()["conflict"], 1)
        edge = [e for e in g.edges if e.kind == "conflict"][0]
        self.assertIn(edge.src, {a.id, b.id})
        self.assertIn(edge.dst, {a.id, b.id})
        self.assertNotEqual(edge.src, edge.dst)

    def test_conflict_pair_with_foreign_root_skipped(self):
        # old_root set means the old side lives in another store — out of
        # scope for the local graph.
        self._put("Local", "Here.")
        b = self._put("Remote twin", "There.")
        conflicts.flag_pair("remote.md", b.filename(), old_root="some-root")
        g = graph.build()
        self.assertEqual(g.counts()["conflict"], 0,
                         "foreign-root pairs must not appear in the local graph")

    def test_weak_tag_edge_needs_two_shared_tags(self):
        self._put("P", "x", tags=["a", "b"])
        self._put("Q", "y", tags=["a", "b"])
        self._put("R", "z", tags=["a"])  # shares only one tag with P/Q
        g = graph.build()
        tag_edges = [e for e in g.edges if e.kind == "tag"]
        self.assertEqual(len(tag_edges), 1, "only the P-Q pair shares 2 tags")
        self.assertEqual(tag_edges[0].weight, 2)

    def test_single_shared_tag_produces_no_edge(self):
        self._put("P", "x", tags=["a", "b"])
        self._put("Q", "y", tags=["a", "c"])
        g = graph.build()
        self.assertEqual(g.counts()["tag"], 0)

    def test_determinism_same_bytes(self):
        self._put("P", "x", tags=["a", "b"])
        self._put("Q", "y", tags=["a", "b"])
        self._put("Old", "o")
        self._put("New", "n")
        store.supersede("old.md", "new.md")
        first = graph.render_text(graph.build())
        second = graph.render_text(graph.build())
        self.assertEqual(first, second, "same store must render identical bytes")


class TestExplicitEdges(TmpStore):
    """G1 relations (relations_json frontmatter) must show up in the G0
    derived graph as 'explicit' edges labelled with the predicate."""

    def _put(self, title, body, type_="fact", tags=None):
        rec = MemoryRecord(title=title, content=body, type=type_,
                           tags=list(tags or []))
        store.write_memory(rec)
        return rec

    def _pair(self):
        from foldcrumbs import relations
        a = self._put("Supplier delay", "Late shipment.", type_="event")
        b = self._put("Release slipped", "Moved to next week.", type_="event")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="postmortem", prov="manual")
        return a, b

    def test_explicit_memory_edge_rendered(self):
        a, b = self._pair()
        g = graph.build()
        self.assertEqual(g.counts()["explicit"], 1)
        edge = [e for e in g.edges if e.kind == "explicit"][0]
        self.assertEqual((edge.src, edge.dst), (a.id, b.id),
                         "explicit edges are directed: source -> target")
        self.assertEqual(edge.label, "caused_by")

    def test_explicit_edges_are_strong_not_weak(self):
        self._pair()
        g = graph.build()
        kinds = {e.kind for e in g.strong_edges()}
        self.assertIn("explicit", kinds)
        self.assertNotIn("explicit", {e.kind for e in g.weak_edges()})

    def test_explicit_edge_dropped_when_target_missing(self):
        from foldcrumbs import relations
        a = self._put("Cause", "x", type_="event")
        b = self._put("Effect", "y", type_="event")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov="manual")
        store.forget(b.filename(), hard=True)
        g = graph.build()
        self.assertEqual(g.counts()["explicit"], 0,
                         "no dangling arrows, same rule as superseded_by")

    def test_explicit_external_target_out_of_scope(self):
        from foldcrumbs import relations
        a = self._put("Vendor call", "x", type_="event")
        relations.add_relation(a.id, "depends_on",
                               {"k": "x", "ns": "org", "l": "acme corp"},
                               evidence="e", prov="manual")
        g = graph.build()
        self.assertEqual(g.counts()["explicit"], 0,
                         "external entities belong to graph entities, not G0 edges")

    def test_explicit_malformed_relations_json_ignored(self):
        a = self._put("Broken rel", "x")
        # Malformed JSON straight into the frontmatter field, rewritten.
        a.relations_json = "{not json"
        store.write_memory(a)
        g = graph.build()  # must not raise
        self.assertEqual(g.counts()["explicit"], 0)

    def test_explicit_determinism(self):
        from foldcrumbs import relations
        ms = [self._put(f"M{i}", "x", type_="event") for i in range(4)]
        relations.add_relation(ms[0].id, "caused_by", {"k": "m", "id": ms[1].id},
                               evidence="e", prov="manual")
        relations.add_relation(ms[2].id, "precedes", {"k": "m", "id": ms[3].id},
                               evidence="e", prov="manual")
        first = graph.render_text(graph.build())
        second = graph.render_text(graph.build())
        self.assertEqual(first, second)
        self.assertIn("caused_by", first)
        self.assertIn("precedes", first)


class TestRenderers(TmpStore):
    def _seed(self):
        old = MemoryRecord(title="Old rule", content="Do A.",
                           tags=["x", "y"])
        store.write_memory(old)
        new = MemoryRecord(title="New rule", content="Do B.", tags=["x", "y"])
        store.write_memory(new)
        store.supersede(old.filename(), new.filename())
        return new

    def test_text_lists_edges(self):
        self._seed()
        out = graph.render_text(graph.build())
        self.assertIn("superseded_by", out)
        self.assertIn("nodes", out)

    def test_text_lists_explicit_predicate(self):
        from foldcrumbs import relations
        a = MemoryRecord(title="Cause X", content="x", type="event")
        b = MemoryRecord(title="Effect Y", content="y", type="event")
        store.write_memory(a)
        store.write_memory(b)
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov="manual")
        out = graph.render_text(graph.build())
        self.assertIn("caused_by", out)

    def test_mermaid_labels_explicit_predicate(self):
        from foldcrumbs import relations
        a = MemoryRecord(title="Cause X2", content="x", type="event")
        b = MemoryRecord(title="Effect Y2", content="y", type="event")
        store.write_memory(a)
        store.write_memory(b)
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov="manual")
        out = graph.render_mermaid(graph.build())
        self.assertIn("caused_by", out)

    def test_text_empty_store_says_so(self):
        out = graph.render_text(graph.build())
        self.assertIn("no relations", out)

    def test_mermaid_has_nodes_and_edges(self):
        self._seed()
        out = graph.render_mermaid(graph.build())
        self.assertTrue(out.startswith("graph LR"))
        self.assertIn("superseded by", out)

    def test_dot_valid_digraph(self):
        self._seed()
        out = graph.render_dot(graph.build())
        self.assertTrue(out.startswith("digraph"))
        self.assertIn("->", out)

    def test_html_is_self_contained(self):
        self._seed()
        page = graph.render_html(graph.build(), "proj")
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<script", page)
        self.assertNotIn("src=", page)
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn("supersede", page)

    def test_html_escapes_titles(self):
        # The schema sanitizer can mangle '<'/'>' in titles, so test escaping
        # with a character it reliably keeps: '&'.
        a = MemoryRecord(title="a & b", content="x", tags=["t1", "t2"])
        b = MemoryRecord(title="Plain", content="y", tags=["t1", "t2"])
        store.write_memory(a)
        store.write_memory(b)
        page = graph.render_html(graph.build(), "p")
        self.assertNotIn("a & b</td>", page)
        self.assertIn("a &amp; b", page)


if __name__ == "__main__":
    unittest.main()
