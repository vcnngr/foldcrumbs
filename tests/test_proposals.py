"""Fixtures for G2 — the relation proposal queue (TDD: preregistered, E6).

Design docs/design/g2-extraction.md mandates these behaviours BEFORE code:

* E1 — dedup is total: store triples and queued triples in ANY status block a
  new proposal; reject is persistent suppression; reopen is human-only.
* E2 — queue writes happen under file_lock; concurrent submitters converge.
* E4 — the overlay feeds graph_path ONLY behind include_inferred; rejected /
  promoted / malformed rows never enter; the store stays byte-identical.
* E4-bis — promote is crash-safe: arc first (tagged), status second; a crash
  between the two converges on retry; doctor detects the impossible case.
* E5 — arcs without prov are legacy: counted, not default-traversable, and
  only a human attestation makes them manual.
* D1/D3 — default traversal is manual-only; filters restrict EDGES, never NODES.

Protocol tests use stubs (no live model). The semantic quality of the model's
output is measured separately at field-test time (E6).
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs.schema import MemoryRecord  # noqa: E402
from foldcrumbs import config, store, relations, proposals  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class G2Store(TmpStore):
    """TmpStore + helpers for G2 fixtures."""

    def _put(self, title, content="Body.", **kw):
        rec = MemoryRecord(title=title, content=content, **kw)
        store.write_memory(rec)
        return rec

    def _triple(self, subject_id, predicate, target_id, evidence="quote",
                confidence=0.5):
        return {"subject_id": subject_id, "predicate": predicate,
                "target_id": target_id, "evidence": evidence,
                "confidence": confidence}


# ── submit: validation + dedup + cap (E1, E2, D4) ─────────────────────────

class TestSubmitValidation(G2Store):

    def test_invalid_ids_dropped(self):
        a = self._put("A")
        stats = proposals.submit([self._triple(a.id, "caused_by", "ghost")])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["invalid"], 1)

    def test_unknown_predicate_dropped(self):
        a, b = self._put("A"), self._put("B")
        stats = proposals.submit([self._triple(a.id, "invents", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["invalid"], 1)

    def test_self_loop_dropped(self):
        a = self._put("A")
        stats = proposals.submit([self._triple(a.id, "caused_by", a.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["invalid"], 1)

    def test_valid_proposal_written(self):
        a, b = self._put("A"), self._put("B")
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 1)
        rows = proposals.load_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["prov"], "inferred")
        self.assertIsNotNone(rows[0]["proposal_id"])

    def test_confidence_capped_for_non_manual(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id, confidence=0.95)])
        self.assertEqual(proposals.load_all()[0]["confidence"], 0.5)

    def test_cap_per_session(self):
        mems = [self._put(f"M{i}") for i in range(12)]
        raw = [self._triple(mems[0].id, "caused_by", m.id) for m in mems[1:]]
        stats = proposals.submit(raw, cap=10)
        self.assertEqual(stats["written"], 10)
        self.assertEqual(stats["capped"], 1)


class TestDedupTotal(G2Store):
    """E1: dedup checks the STORE and the queue in ALL statuses."""

    def test_store_triple_blocks_proposal(self):
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="x", prov="manual")
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["dup_store"], 1)

    def test_pending_blocks_duplicate(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id)])
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["dup_queue"], 1)

    def test_rejected_blocks_duplicate_persistently(self):
        """Reject is persistent suppression: the triple stays suppressed."""
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id)])
        pid = proposals.load_all()[0]["proposal_id"]
        proposals.reject(pid)
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["dup_queue"], 1)

    def test_promoted_blocks_duplicate(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id)])
        pid = proposals.load_all()[0]["proposal_id"]
        proposals.promote(pid)
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["dup_store"], 1)


# ── promote / reject / reopen (E4, E4-bis) ────────────────────────────────

class TestPromote(G2Store):

    def _pending(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id,
                                       evidence="the cause", confidence=0.4)])
        row = proposals.load_all()[0]
        return a, b, row["proposal_id"]

    def test_promote_writes_manual_arc_tagged(self):
        a, b, pid = self._pending()
        res = proposals.promote(pid)
        self.assertEqual(res["action"], "ok")
        rec = next(m for m in store.load_all() if m.id == a.id)
        rels = relations.parse(rec.relations_json)
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["prov"], "manual")
        self.assertEqual(rels[0]["proposal_id"], pid)
        row = proposals.get(pid)
        self.assertEqual(row["status"], "promoted")
        self.assertIsNotNone(row["decided_at"])

    def test_promote_is_idempotent(self):
        _, _, pid = self._pending()
        proposals.promote(pid)
        res = proposals.promote(pid)
        self.assertEqual(res["action"], "noop")

    def test_promote_rejected_is_noop(self):
        _, _, pid = self._pending()
        proposals.reject(pid)
        res = proposals.promote(pid)
        self.assertEqual(res["action"], "noop")
        self.assertEqual(res["status"], "rejected")

    def test_reject_sets_status(self):
        _, _, pid = self._pending()
        res = proposals.reject(pid)
        self.assertEqual(res["action"], "ok")
        self.assertEqual(proposals.get(pid)["status"], "rejected")

    def test_reopen_only_from_rejected(self):
        _, _, pid = self._pending()
        res = proposals.reopen(pid)          # pending -> pending: noop
        self.assertEqual(res["action"], "noop")
        proposals.reject(pid)
        res = proposals.reopen(pid)
        self.assertEqual(res["action"], "ok")
        row = proposals.get(pid)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["decided_at"])

    def test_unknown_id_raises(self):
        with self.assertRaises(proposals.ProposalError):
            proposals.promote("does-not-exist")


class TestCrashRecovery(G2Store):
    """E4-bis: promote writes the arc FIRST (tagged), the status second.
    A crash between the two must converge on retry — never a duplicate,
    never a dangling pending."""

    def test_arc_written_status_lost_converges(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id,
                                       evidence="e", confidence=0.4)])
        row = proposals.load_all()[0]
        pid = row["proposal_id"]
        # Simulate the crash: the arc made it to the store, the JSONL didn't.
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", confidence=0.4,
                               prov="manual", proposal_id=pid)
        self.assertEqual(proposals.get(pid)["status"], "pending")
        # Retry: add_relation dedups (returns False), status converges.
        res = proposals.promote(pid)
        self.assertEqual(res["action"], "ok")
        self.assertEqual(proposals.get(pid)["status"], "promoted")
        rec = next(m for m in store.load_all() if m.id == a.id)
        self.assertEqual(len(relations.parse(rec.relations_json)), 1)

    def test_overlay_excludes_materialized_pending(self):
        """A pending row whose arc already exists must NOT appear in the
        overlay — the store copy is authoritative (no double edge)."""
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        pid = proposals.load_all()[0]["proposal_id"]
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov="manual", proposal_id=pid)
        self.assertEqual(proposals.overlay_edges(), [])

    def test_doctor_detects_promoted_without_arc(self):
        """The impossible case must be reported, never fixed silently."""
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        pid = proposals.load_all()[0]["proposal_id"]
        # Forge the impossible state directly in the queue file.
        path = proposals.queue_path()
        rows = proposals.load_all()
        rows[0]["status"] = "promoted"
        path.write_text(json.dumps(rows[0]) + "\n")
        rep = proposals.doctor()
        self.assertIn(pid, rep["promoted_missing_arc"])


# ── overlay + find_path containment (E4, D1, D3) ──────────────────────────

class TestOverlayContainment(G2Store):

    def _pair(self):
        return self._put("A"), self._put("B")

    def test_pending_not_walked_by_default(self):
        a, b = self._pair()
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_pending_walked_with_include_inferred(self):
        a, b = self._pair()
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        res = relations.find_path(a.id, b.id, include_inferred=True)
        self.assertEqual(res["status"], "FOUND")
        edge = res["path"][1]["edge"]
        self.assertTrue(edge.get("_overlay"))

    def test_rejected_never_walked(self):
        a, b = self._pair()
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        pid = proposals.load_all()[0]["proposal_id"]
        proposals.reject(pid)
        res = relations.find_path(a.id, b.id, include_inferred=True)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_store_byte_identical_during_query(self):
        """The overlay is read-only: querying must not touch the store."""
        a, b = self._pair()
        proposals.submit([self._triple(a.id, "caused_by", b.id, evidence="e")])
        before = sorted(
            (p.name, p.read_bytes()) for p in config.memory_dir().glob("*.md"))
        relations.find_path(a.id, b.id, include_inferred=True)
        after = sorted(
            (p.name, p.read_bytes()) for p in config.memory_dir().glob("*.md"))
        self.assertEqual(before, after)


class TestProvContainment(G2Store):
    """D1: default traversal is manual-only. agent/inferred/legacy are
    second-class until opted in per query."""

    def _arc(self, prov):
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov=prov)
        return a, b

    def test_manual_walked_by_default(self):
        a, b = self._arc("manual")
        self.assertEqual(relations.find_path(a.id, b.id)["status"], "FOUND")

    def test_agent_not_walked_by_default(self):
        a, b = self._arc("agent")
        self.assertEqual(relations.find_path(a.id, b.id)["status"],
                         "NOT_FOUND_EXHAUSTIVE")
        self.assertEqual(relations.find_path(a.id, b.id,
                         include_inferred=True)["status"], "FOUND")

    def test_inferred_not_walked_by_default(self):
        a, b = self._arc("inferred")
        self.assertEqual(relations.find_path(a.id, b.id)["status"],
                         "NOT_FOUND_EXHAUSTIVE")
        self.assertEqual(relations.find_path(a.id, b.id,
                         include_inferred=True)["status"], "FOUND")

    def test_agent_confidence_capped_even_with_evidence(self):
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="real quote", confidence=0.95,
                               prov="agent")
        rec = next(m for m in store.load_all() if m.id == a.id)
        self.assertEqual(relations.parse(rec.relations_json)[0]["c"], 0.5)

    def test_unknown_prov_rejected(self):
        a, b = self._put("A"), self._put("B")
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                                   evidence="e", prov="trusted")


class TestLegacyArcs(G2Store):
    """E5: arcs written before the taxonomy have no prov. They are counted,
    never silently relabelled manual, and attested one by one by a human."""

    def _legacy_arc(self):
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e")          # no prov -> legacy
        return a, b

    def test_legacy_counted_and_not_traversable(self):
        a, b = self._legacy_arc()
        legacy = relations.legacy_arcs()
        self.assertEqual(len(legacy), 1)
        self.assertEqual(relations.find_path(a.id, b.id)["status"],
                         "NOT_FOUND_EXHAUSTIVE")
        self.assertEqual(relations.find_path(a.id, b.id,
                         include_inferred=True)["status"], "FOUND")

    def test_promote_legacy_attests_manual(self):
        a, b = self._legacy_arc()
        ok = relations.promote_legacy_arc(a.id, "caused_by",
                                          {"k": "m", "id": b.id})
        self.assertTrue(ok)
        self.assertEqual(relations.legacy_arcs(), [])
        self.assertEqual(relations.find_path(a.id, b.id)["status"], "FOUND")

    def test_promote_legacy_no_match(self):
        a, b = self._legacy_arc()
        ok = relations.promote_legacy_arc(a.id, "depends_on",
                                          {"k": "m", "id": b.id})
        self.assertFalse(ok)
        self.assertEqual(len(relations.legacy_arcs()), 1)


# ── node universe / tri-state (D3, E3) ────────────────────────────────────

class TestNodeUniverse(G2Store):

    def test_superseded_endpoint_not_found_with_note(self):
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="e", prov="manual")
        store.mark_superseded_on_disk(b, "other-id")
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")
        self.assertIn("not traversable", res["note"])

    def test_missing_id_raises(self):
        a = self._put("A")
        with self.assertRaises(relations.InvalidRelation):
            relations.find_path(a.id, "no-such-memory")


# ── parse + distill hook (D4, D2 gate) ────────────────────────────────────

class TestParseG2(unittest.TestCase):

    def test_valid_array_parsed(self):
        answer = json.dumps([
            {"subject_id": "a", "predicate": "caused_by", "target_id": "b",
             "evidence": "quote", "confidence": 0.7}])
        out = None
        from foldcrumbs import distill
        out = distill.parse_g2_relations(answer)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["predicate"], "caused_by")

    def test_unknown_predicate_dropped(self):
        from foldcrumbs import distill
        answer = json.dumps([
            {"subject_id": "a", "predicate": "invents", "target_id": "b",
             "evidence": "q", "confidence": 0.5}])
        self.assertEqual(distill.parse_g2_relations(answer), [])

    def test_no_evidence_dropped(self):
        from foldcrumbs import distill
        answer = json.dumps([
            {"subject_id": "a", "predicate": "caused_by", "target_id": "b",
             "evidence": "", "confidence": 0.5}])
        self.assertEqual(distill.parse_g2_relations(answer), [])

    def test_self_loop_dropped(self):
        from foldcrumbs import distill
        answer = json.dumps([
            {"subject_id": "a", "predicate": "caused_by", "target_id": "a",
             "evidence": "q", "confidence": 0.5}])
        self.assertEqual(distill.parse_g2_relations(answer), [])

    def test_garbage_returns_empty(self):
        from foldcrumbs import distill
        self.assertEqual(distill.parse_g2_relations("not json"), [])
        self.assertEqual(distill.parse_g2_relations(None), [])
        self.assertEqual(distill.parse_g2_relations(""), [])


class TestDistillG2Gate(G2Store):
    """D2: extraction is opt-in. Without FOLDCRUMBS_G2=1 distill never
    proposes. With it, a stubbed model produces queued proposals."""

    def _seed_store(self):
        self._put("Decision A", "We chose X.")
        self._put("Fact B", "Y is true.")

    def _stub_answer(self):
        by_title = {m.title: m for m in store.load_all()}
        a, b = by_title["Decision A"], by_title["Fact B"]
        return json.dumps([
            {"subject_id": a.id, "predicate": "depends_on",
             "target_id": b.id, "evidence": "Y is true.",
             "confidence": 0.6}])

    def test_gate_off_no_extraction(self):
        import os
        self._seed_store()
        saved = os.environ.pop("FOLDCRUMBS_G2", None)
        try:
            from foldcrumbs import distill
            with mock.patch.object(distill, "_extract_relations") as ex:
                distill.distill_and_store("session notes")
            ex.assert_not_called()
            self.assertEqual(proposals.load_all(), [])
        finally:
            if saved is not None:
                os.environ["FOLDCRUMBS_G2"] = saved

    def test_gate_on_enqueues_proposals(self):
        import os
        self._seed_store()
        saved = os.environ.get("FOLDCRUMBS_G2")
        os.environ["FOLDCRUMBS_G2"] = "1"
        try:
            from foldcrumbs import distill, llm
            with mock.patch.object(llm, "chat",
                                   return_value=self._stub_answer()), \
                 mock.patch.object(distill, "_llm_extract", return_value=[]), \
                 mock.patch.object(distill, "heuristic_memories",
                                   return_value=[]):
                counts = distill.distill_and_store("session notes")
            self.assertEqual(counts.get("relations_proposed"), 1)
            rows = proposals.load_all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "pending")
            self.assertEqual(rows[0]["prov"], "inferred")
        finally:
            if saved is None:
                os.environ.pop("FOLDCRUMBS_G2", None)
            else:
                os.environ["FOLDCRUMBS_G2"] = saved

    def test_llm_failure_does_not_break_distill(self):
        import os
        self._seed_store()
        saved = os.environ.get("FOLDCRUMBS_G2")
        os.environ["FOLDCRUMBS_G2"] = "1"
        try:
            from foldcrumbs import distill, llm
            with mock.patch.object(llm, "chat",
                                   side_effect=RuntimeError("boom")), \
                 mock.patch.object(distill, "_llm_extract", return_value=[]), \
                 mock.patch.object(distill, "heuristic_memories",
                                   return_value=[]):
                counts = distill.distill_and_store("session notes")
            self.assertEqual(counts.get("relations_proposed"), 0)
        finally:
            if saved is None:
                os.environ.pop("FOLDCRUMBS_G2", None)
            else:
                os.environ["FOLDCRUMBS_G2"] = saved


# ── MCP relate tool (D1: agent prov, capped) ──────────────────────────────

class TestMcpRelate(G2Store):

    def test_relate_writes_agent_arc(self):
        from foldcrumbs import mcp_server
        for content, title in (("First fact.", "Mem One"),
                               ("Second fact.", "Mem Two")):
            mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": "remember", "arguments": {
                                   "content": content, "type": "fact",
                                   "title": title}}})
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                               "method": "tools/call",
                               "params": {"name": "relate", "arguments": {
                                   "memory": "Mem One", "predicate": "caused_by",
                                   "to_memory": "Mem Two",
                                   "evidence": "the first caused the second",
                                   "confidence": 0.9}}})
        text = r["result"]["content"][0]["text"]
        self.assertFalse(r["result"]["isError"])
        self.assertIn("provenance 'agent'", text)
        rec = next(m for m in store.load_all() if m.title == "Mem One")
        rel = relations.parse(rec.relations_json)[0]
        self.assertEqual(rel["prov"], "agent")
        self.assertEqual(rel["c"], 0.5)          # capped, not 0.9

    def test_relate_not_default_traversable(self):
        from foldcrumbs import mcp_server
        for content, title in (("First fact.", "Mem One"),
                               ("Second fact.", "Mem Two")):
            mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": "remember", "arguments": {
                                   "content": content, "type": "fact",
                                   "title": title}}})
        mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                           "method": "tools/call",
                           "params": {"name": "relate", "arguments": {
                               "memory": "Mem One", "predicate": "caused_by",
                               "to_memory": "Mem Two", "evidence": "e"}}})
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 3,
                               "method": "tools/call",
                               "params": {"name": "graph_path", "arguments": {
                                   "from": "Mem One", "to": "Mem Two"}}})
        self.assertIn("NOT_FOUND_EXHAUSTIVE", r["result"]["content"][0]["text"])
        r2 = mcp_server.handle({"jsonrpc": "2.0", "id": 4,
                               "method": "tools/call",
                               "params": {"name": "graph_path", "arguments": {
                                   "from": "Mem One", "to": "Mem Two",
                                   "include_inferred": True}}})
        self.assertIn("FOUND", r2["result"]["content"][0]["text"])


# ── Regression tests from GPT code red-team (t_0202c40d) ─────────────────
# Each class maps to one counterexample GPT executed against the merged
# implementation (5da0c04). These tests are born red against that commit.

class TestP01Serialization(G2Store):
    """P0-1: submit/promote and direct writers must serialize on one lock;
    promote must never declare success without the exact tagged arc."""

    def _pending(self):
        a, b = self._put("A"), self._put("B")
        proposals.submit([self._triple(a.id, "caused_by", b.id,
                                       evidence="the cause")])
        return a, b, proposals.load_all()[0]["proposal_id"]

    def test_promote_after_untagged_direct_write_refuses_visibly(self):
        """GPT's second counterexample: pending proposal + untagged direct
        arc on the same triple + promote. The old code marked promoted
        anyway (doctor then screamed promoted_missing_arc). The fix:
        refuse visibly, leave the queue row pending."""
        a, b, pid = self._pending()
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="direct", prov="agent")
        with self.assertRaises(proposals.ProposalError) as ctx:
            proposals.promote(pid)
        self.assertIn("could not be attested", str(ctx.exception))
        self.assertEqual(proposals.get(pid)["status"], "pending")
        self.assertEqual(proposals.doctor()["promoted_missing_arc"], [])

    def test_overlay_hides_pending_when_store_has_the_triple(self):
        """GPT: with include_inferred the same triple appeared once from the
        store and once from the overlay. Store copy is authoritative; the
        overlay stays silent for that triple."""
        a, b, pid = self._pending()
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="direct", prov="agent")
        self.assertEqual(proposals.overlay_edges(), [])

    def test_submit_and_direct_write_serialize(self):
        """Both writers take the queue lock; the triple lands exactly once,
        as either a queued proposal OR a store arc — never both."""
        a, b = self._put("A"), self._put("B")
        relations.add_relation(a.id, "caused_by", {"k": "m", "id": b.id},
                               evidence="direct", prov="agent")
        stats = proposals.submit([self._triple(a.id, "caused_by", b.id)])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["dup_store"], 1)
        self.assertEqual(proposals.load_all(), [])


class TestP02BooleanFlag(G2Store):
    """P0-2: include_inferred is fail-closed on type. ONLY boolean True
    enables non-manual traversal; strings, numbers, null are refusals."""

    def _agent_arc(self):
        from foldcrumbs import mcp_server
        for content, title in (("First fact.", "Mem One"),
                               ("Second fact.", "Mem Two")):
            mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": "remember", "arguments": {
                                   "content": content, "type": "fact",
                                   "title": title}}})
        mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                           "method": "tools/call",
                           "params": {"name": "relate", "arguments": {
                               "memory": "Mem One", "predicate": "caused_by",
                               "to_memory": "Mem Two", "evidence": "e"}}})

    def _path(self, flag):
        from foldcrumbs import mcp_server
        args = {"from": "Mem One", "to": "Mem Two"}
        if flag is not None:
            args["include_inferred"] = flag
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 9,
                               "method": "tools/call",
                               "params": {"name": "graph_path",
                                          "arguments": args}})
        return r["result"]["content"][0]["text"]

    def test_string_false_does_not_enable_traversal(self):
        """GPT's exact counterexample: the string "false" must never enable
        non-manual traversal. The fix is fail-closed on type — ANY non-bool
        is a visible refusal, which is strictly stronger than falling back
        to NOT_FOUND (it also surfaces a client misusing the schema)."""
        self._agent_arc()
        text = self._path("false")
        self.assertNotIn("FOUND —", text)   # never enables traversal
        self.assertIn("refused", text)       # visible refusal, not silent

    def test_string_true_is_refused_not_coerced(self):
        self._agent_arc()
        text = self._path("true")
        self.assertIn("refused", text)      # visible refusal, not FOUND

    def test_numbers_and_null_are_fail_closed(self):
        self._agent_arc()
        # null/absent falls back to the safe default (no opt-in) — same as
        # the flag being absent; numbers are non-bool and refused.
        self.assertIn("NOT_FOUND_EXHAUSTIVE", self._path(None))
        self.assertIn("refused", self._path(1))
        self.assertIn("refused", self._path(0))

    def test_boolean_true_still_works(self):
        self._agent_arc()
        self.assertIn("FOUND", self._path(True))
        self.assertIn("NOT_FOUND_EXHAUSTIVE", self._path(False))


class TestP03PerStoreQueue(G2Store):
    """P0-3: the queue is namespaced by the store it serves; promote works
    with an explicit cwd regardless of the process cwd."""

    def test_two_stores_do_not_see_each_other(self):
        """GPT's exact scenario: shared STATE_DIR, two different cwd. Each
        store gets its own queue; B never sees A's proposals; promote works
        with an explicit cwd even when the process cwd is elsewhere.

        Setup note: TmpStore points ENGRAM_DIR at one dir, collapsing every
        cwd onto one store — the opposite of what this test needs. So this
        test clears the DIR overrides and lets memory_dir derive from cwd.
        """
        import os
        import tempfile
        from importlib import reload
        from foldcrumbs import config as _c

        shared_state = tempfile.mkdtemp(prefix="g2_shared_state_")
        cfg_dir = tempfile.mkdtemp(prefix="g2_cfg_")
        env_keys = ("FOLDCRUMBS_DIR", "ENGRAM_DIR", "FOLDCRUMBS_STATE_DIR",
                    "ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR")
        saved = {k: os.environ.get(k) for k in env_keys}
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ["FOLDCRUMBS_STATE_DIR"] = shared_state
        os.environ["CLAUDE_CONFIG_DIR"] = cfg_dir
        reload(_c)
        try:
            dir_a = tempfile.mkdtemp(prefix="g2_proj_a_")
            dir_b = tempfile.mkdtemp(prefix="g2_proj_b_")
            self.assertNotEqual(_c.memory_dir(dir_a), _c.memory_dir(dir_b),
                                "test premise: two cwds = two stores")
            rec_a1 = MemoryRecord(title="Alpha One", content="a1")
            rec_a2 = MemoryRecord(title="Alpha Two", content="a2")
            store.write_memory(rec_a1, dir_a)
            store.write_memory(rec_a2, dir_a)
            stats = proposals.submit(
                [{"subject_id": rec_a1.id, "predicate": "caused_by",
                  "target_id": rec_a2.id, "evidence": "e",
                  "confidence": 0.5}],
                cwd=dir_a)
            self.assertEqual(stats["written"], 1)
            # Store B sees none of A's queue (GPT: it used to read A's row
            # and A's evidence).
            self.assertEqual(proposals.load_all(cwd=dir_b), [])
            self.assertEqual(proposals.counts(cwd=dir_b)["pending"], 0)
            # A still sees its own proposal.
            self.assertEqual(proposals.counts(cwd=dir_a)["pending"], 1)
            # Promote with an explicit cwd works even though the process cwd
            # is neither store (GPT: this used to raise InvalidRelation
            # because _known_ids read the process cwd).
            pid = proposals.load_all(cwd=dir_a)[0]["proposal_id"]
            res = proposals.promote(pid, cwd=dir_a)
            self.assertEqual(res["action"], "ok")
            rec = next(m for m in store.load_all(dir_a) if m.id == rec_a1.id)
            rels = relations.parse(rec.relations_json)
            self.assertEqual(len(rels), 1)
            self.assertEqual(rels[0]["prov"], "manual")
            self.assertEqual(rels[0]["proposal_id"], pid)
            # Doctor stays per-store: B has no findings about A's queue.
            self.assertEqual(proposals.doctor(cwd=dir_b)["counts"]["pending"],
                             0)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            reload(_c)

    def test_known_ids_cwd_aware(self):
        """add_relation with explicit cwd validates target ids against THAT
        store (GPT's promote-from-elsewhere failure) — with the DIR env
        overrides cleared so cwd really selects the store."""
        import os
        import tempfile
        from importlib import reload
        from foldcrumbs import config as _c

        shared_state = tempfile.mkdtemp(prefix="g2_shared_state2_")
        cfg_dir = tempfile.mkdtemp(prefix="g2_cfg2_")
        env_keys = ("FOLDCRUMBS_DIR", "ENGRAM_DIR", "FOLDCRUMBS_STATE_DIR",
                    "ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR")
        saved = {k: os.environ.get(k) for k in env_keys}
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ["FOLDCRUMBS_STATE_DIR"] = shared_state
        os.environ["CLAUDE_CONFIG_DIR"] = cfg_dir
        reload(_c)
        try:
            dir_a = tempfile.mkdtemp(prefix="g2_proj_a2_")
            rec1 = MemoryRecord(title="Beta One", content="b1")
            rec2 = MemoryRecord(title="Beta Two", content="b2")
            store.write_memory(rec1, dir_a)
            store.write_memory(rec2, dir_a)
            ok = relations.add_relation(
                rec1.id, "depends_on", {"k": "m", "id": rec2.id},
                evidence="e", prov="manual", cwd=dir_a)
            self.assertTrue(ok)
            # The arc landed in store A, nowhere else.
            rec = next(m for m in store.load_all(dir_a) if m.id == rec1.id)
            self.assertEqual(len(relations.parse(rec.relations_json)), 1)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            reload(_c)


class TestP1Envelope(G2Store):
    """P1-1: a proposal without evidence is invalid on the way in — the
    envelope validator matches what the overlay enforces on the way out."""

    def test_empty_evidence_rejected(self):
        a, b = self._put("A"), self._put("B")
        stats = proposals.submit(
            [self._triple(a.id, "caused_by", b.id, evidence="")])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["invalid"], 1)
        self.assertEqual(proposals.load_all(), [])

    def test_whitespace_evidence_rejected(self):
        a, b = self._put("A"), self._put("B")
        stats = proposals.submit(
            [self._triple(a.id, "caused_by", b.id, evidence="   ")])
        self.assertEqual(stats["written"], 0)
        self.assertEqual(stats["invalid"], 1)


if __name__ == "__main__":
    unittest.main()
