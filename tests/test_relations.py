"""Fixtures for G1 — explicit relations (TDD: these exist BEFORE the code).

Design REV-2, gate 4 mandates four fixture classes that must pass before
production code ships:

1. round-trip relations_json  — parse → edit → write → parse, semantically
   identical; unknown frontmatter keys preserved.
2. multi-process fail-closed  — two processes add different relations to the
   same memory → both survive, or one fails VISIBLY (never silent loss).
3. tri-state path             — find_path returns FOUND / NOT_FOUND_EXHAUSTIVE
   / TRUNCATED:<reason>; never ambiguous.
4. invalid predicate/target   — unknown predicate or malformed target is
   rejected explicitly, never silently accepted.
"""

import multiprocessing as mp
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: import for the sandbox side effect, like every other
# test module — without it a stray run would read the developer's real store.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs.schema import MemoryRecord  # noqa: E402
from foldcrumbs import config, store, relations  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class RelStore(TmpStore):
    """TmpStore + helpers for relation fixtures."""

    def _put(self, title, content="Body.", **kw):
        rec = MemoryRecord(title=title, content=content, **kw)
        store.write_memory(rec)
        return rec

    def _by_id(self, mem_id):
        for rec in store.load_all():
            if rec.id == mem_id:
                return rec
        self.fail(f"memory {mem_id} not found in store")


# ── Fixture 1: round-trip relations_json ──────────────────────────────────

class TestRoundTrip(RelStore):
    """relations_json survives parse → edit → write → parse unchanged."""

    def test_relations_json_survives_roundtrip(self):
        rec = self._put("Anchor", "Stable fact.")
        rel_json = relations.canonical([{
            "p": "caused_by",
            "t": {"k": "m", "id": rec.id},
            "e": "the supplier problem caused the release delay",
            "c": 0.82,
            "d": "2026-08-08T10:00:00Z",
        }])
        rec.relations_json = rel_json
        store.write_memory(rec)

        loaded = self._by_id(rec.id)
        self.assertEqual(loaded.relations_json, rel_json)
        self.assertEqual(relations.parse(loaded.relations_json)[0]["p"],
                         "caused_by")

    def test_unknown_frontmatter_key_preserved(self):
        """A hand-added frontmatter key must not be erased by a rewrite."""
        rec = self._put("Anchor Two", "Body.")
        path = config.memory_dir() / rec.filename()
        text = path.read_text()
        text = text.replace("\n---\n",
                            "\ncustom_key: preserved_value\n---\n", 1)
        path.write_text(text)

        loaded = self._by_id(rec.id)
        loaded.content = "Updated body."
        store.write_memory(loaded)

        self.assertIn("custom_key: preserved_value", path.read_text())

    def test_relations_json_canonical_deterministic(self):
        """Same logical relations → same canonical JSON string."""
        r1 = relations.canonical([{
            "p": "depends_on", "t": {"k": "m", "id": "abc"},
            "e": "x", "c": 0.9, "d": "2026-01-01T00:00:00Z",
        }])
        r2 = relations.canonical([{
            "d": "2026-01-01T00:00:00Z", "c": 0.9,
            "e": "x", "t": {"k": "m", "id": "abc"}, "p": "depends_on",
        }])
        self.assertEqual(r1, r2)


# ── Fixture 2: multi-process fail-closed ──────────────────────────────────

def _worker_add_relation(mem_dir, state_dir, mem_id, predicate, evidence,
                         result_queue):
    """Add one relation under the memory lock; report the outcome."""
    import os
    os.environ["ENGRAM_DIR"] = str(mem_dir)
    os.environ["ENGRAM_STATE_DIR"] = str(state_dir)
    for k in ("FOLDCRUMBS_DIR", "FOLDCRUMBS_STATE_DIR"):
        os.environ.pop(k, None)
    import importlib
    from foldcrumbs import config as _c
    importlib.reload(_c)
    from foldcrumbs import relations as _rel
    try:
        ok = _rel.add_relation(
            mem_id, predicate,
            target={"k": "x", "ns": "test", "l": "entity"},
            evidence=evidence, confidence=0.8,
        )
        result_queue.put(("ok" if ok else "rejected", None))
    except Exception as exc:  # noqa: BLE001 — report any failure visibly
        result_queue.put(("error", str(exc)))


class TestMultiProcessFailClosed(RelStore):
    """Two processes add different relations to the same memory concurrently.
    Both must survive, or one must fail VISIBLY (never silent loss)."""

    def test_concurrent_add_no_silent_loss(self):
        rec = self._put("Concurrent", "Shared target.")
        mem_id = rec.id
        mem_dir = str(config.memory_dir())
        state_dir = str(config.STATE_DIR)
        ctx = mp.get_context("spawn")
        q = ctx.Queue()

        p1 = ctx.Process(target=_worker_add_relation,
                         args=(mem_dir, state_dir, mem_id, "caused_by",
                               "evidence A", q))
        p2 = ctx.Process(target=_worker_add_relation,
                         args=(mem_dir, state_dir, mem_id, "depends_on",
                               "evidence B", q))
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)
        self.assertFalse(p1.is_alive() or p2.is_alive(),
                         "worker hung on the memory lock")

        outcomes = []
        while not q.empty():
            outcomes.append(q.get())
        self.assertEqual(len(outcomes), 2, f"lost a worker report: {outcomes}")

        loaded = self._by_id(mem_id)
        rels = relations.parse(loaded.relations_json)
        ok_count = sum(1 for status, _ in outcomes if status == "ok")
        # Every "ok" outcome must correspond to a relation on disk: the only
        # acceptable outcomes are both-on-disk or one visibly rejected.
        self.assertEqual(len(rels), ok_count,
                         f"silent loss: {ok_count} ok outcomes, {len(rels)} "
                         f"relations on disk")
        self.assertGreaterEqual(ok_count, 1)


# ── Fixture 3: tri-state path ─────────────────────────────────────────────

class TestTriStatePath(RelStore):
    """find_path returns FOUND / NOT_FOUND_EXHAUSTIVE / TRUNCATED:<reason>."""

    def _chain(self, n):
        """Linear chain of n memories: m0 → m1 → … → m(n-1)."""
        recs = [self._put(f"Node {i}", f"Content {i}.") for i in range(n)]
        for i in range(n - 1):
            relations.add_relation(
                recs[i].id, "caused_by",
                target={"k": "m", "id": recs[i + 1].id},
                evidence=f"node {i} causes node {i + 1}",
                confidence=0.9,
            )
        return recs

    def test_found_path(self):
        recs = self._chain(3)
        result = relations.find_path(recs[0].id, recs[2].id)
        self.assertEqual(result["status"], "FOUND")
        self.assertEqual(len(result["path"]), 3)

    def test_not_found_exhaustive(self):
        recs = self._chain(3)
        orphan = self._put("Orphan", "Disconnected.")
        result = relations.find_path(recs[0].id, orphan.id)
        self.assertEqual(result["status"], "NOT_FOUND_EXHAUSTIVE")
        self.assertIn("reached", result)

    def test_truncated_by_depth(self):
        recs = self._chain(6)
        result = relations.find_path(recs[0].id, recs[5].id, depth=2)
        self.assertTrue(result["status"].startswith("TRUNCATED"),
                        f"expected TRUNCATED, got {result['status']}")

    def test_deterministic_order(self):
        recs = self._chain(4)
        r1 = relations.find_path(recs[0].id, recs[3].id)
        r2 = relations.find_path(recs[0].id, recs[3].id)
        self.assertEqual(r1, r2)


# ── Fixture 4: invalid predicate / target rejection ───────────────────────

class TestInvalidRejection(RelStore):
    """Unknown predicates and malformed targets are rejected explicitly."""

    def test_unknown_predicate_rejected(self):
        rec = self._put("Pred", "Body.")
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                rec.id, "invents_new_thing",
                target={"k": "m", "id": rec.id},
                evidence="x", confidence=0.8,
            )

    def test_empty_target_rejected(self):
        rec = self._put("Target", "Body.")
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                rec.id, "caused_by", target={},
                evidence="x", confidence=0.8,
            )

    def test_missing_memory_target_rejected(self):
        rec = self._put("Missing", "Body.")
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                rec.id, "caused_by",
                target={"k": "m", "id": "nonexistent-id-12345"},
                evidence="x", confidence=0.8,
            )

    def test_empty_label_rejected(self):
        rec = self._put("Label", "Body.")
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                rec.id, "caused_by",
                target={"k": "x", "ns": "test", "l": "   "},
                evidence="x", confidence=0.8,
            )

    def test_no_evidence_gets_low_confidence(self):
        rec = self._put("No Ev", "Body.")
        ok = relations.add_relation(
            rec.id, "supports",
            target={"k": "x", "ns": "test", "l": "some entity"},
            evidence="", confidence=0.9,
        )
        self.assertTrue(ok)
        loaded = self._by_id(rec.id)
        rels = relations.parse(loaded.relations_json)
        self.assertEqual(len(rels), 1)
        self.assertLessEqual(rels[0]["c"], 0.5)
        self.assertEqual(rels[0].get("prov"), "inferred")


# ── CLI end-to-end: relate + graph path/doctor/entities ───────────────────

class TestRelationsCLI(RelStore):
    """The commands the user and agents actually type, run in-process."""

    def _run(self, *argv):
        import contextlib
        import io
        from foldcrumbs import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_relate_path_and_doctor(self):
        self._put("Supplier delay", "Late delivery.")
        self._put("Release slipped", "Date moved.")

        code, out, _ = self._run(
            "relate", "Release slipped", "caused_by",
            "--to-memory", "Supplier delay",
            "--evidence", "the release slipped because the supplier was late")
        self.assertEqual(code, 0, out)
        self.assertIn("relation added", out)

        code, out, _ = self._run("graph", "path",
                                 "Supplier delay", "Release slipped")
        self.assertEqual(code, 0, out)
        self.assertIn("FOUND", out)
        self.assertIn("Supplier delay", out)

        code, out, _ = self._run("graph", "doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("clean", out)

    def test_relate_to_entity_and_entities_listing(self):
        self._put("Postgres migration", "Moved the DB.")
        code, out, _ = self._run(
            "relate", "Postgres migration", "depends_on",
            "--to-entity", "  Moonshot   AI ", "--namespace", "vendor",
            "--evidence", "vendor quota gates the migration")
        self.assertEqual(code, 0, out)

        code, out, _ = self._run("graph", "entities")
        self.assertEqual(code, 0, out)
        # Normalized label: trimmed, collapsed, lowercased.
        self.assertIn("[vendor] moonshot ai", out)

    def test_relate_refuses_unknown_predicate_visibly(self):
        self._put("Some memory", "Body.")
        code, _, err = self._run("relate", "Some memory", "invents_stuff",
                                 "--to-entity", "x")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)

    def test_path_no_connection_is_exhaustive_not_truncated(self):
        self._put("Island A", "Alone.")
        self._put("Island B", "Also alone.")
        code, out, _ = self._run("graph", "path", "Island A", "Island B")
        self.assertEqual(code, 1)
        self.assertIn("NOT_FOUND_EXHAUSTIVE", out)


if __name__ == "__main__":
    unittest.main()
