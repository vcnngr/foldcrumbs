"""Auto-prune must not delete legitimate prose memories (data-loss bug).

Reported 2026-09-18 via the owner: auto-prune deleted an architecture
memory TWICE because it contained a markdown table — while the comment
above _HARD_ARTIFACT_RE says the deletion subset must only contain
patterns that are "never legitimate durable prose".

RT r1 (card t_c82395d2) RED, 2 P0 on the first fix attempt (prevalence
rule): F1 — a legitimate pure lookup-table memory (≥80% structural) was
still auto-unlinked; F2 — an accidentally unclosed fence made everything
after it "structural" and killed a legitimate playbook memory. Reviewer's
minimal correction, adopted verbatim: NO ambiguous markdown shape may
drive auto-unlink. Shapes are flagged (pollution report, doctor) and die
only under the explicit `prune --apply` — a human decision, dry-run
default. Auto-prune keeps only the unconditional boilerplate marker.

Contract pinned here (deletion bias asymmetric on purpose: false
negative = cheap visible junk; false positive = unrecoverable loss):
- AUTO-prune (prune_artifacts, runs on every distill): deletes ONLY the
  boilerplate marker ("do not respond to these messages")
- FLAG-grade (audit pollution + explicit prune candidates): boilerplate
  OR shape-artifact (≥80% structural lines: table rows/separators,
  BALANCED fence blocks, glyph-only lines). Unbalanced fence → the
  ambiguous tail never counts as structural (F2).
- prose memory containing a table / fence / glyph → survives everything
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
# Before foldcrumbs: config resolves STATE_DIR at import time.
from _sandbox import SANDBOX, is_inside  # noqa: E402

from foldcrumbs import audit, distill, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class _PruneStore(unittest.TestCase):
    def setUp(self):
        self._old = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX, prefix="prune_")
        self.dir = tempfile.mkdtemp(dir=self._tmp.name, prefix="store_")
        state = tempfile.mkdtemp(dir=self._tmp.name, prefix="state_")
        os.environ["FOLDCRUMBS_DIR"] = self.dir
        os.environ["FOLDCRUMBS_STATE_DIR"] = state
        assert is_inside(self.dir) and is_inside(state)
        import importlib
        from foldcrumbs import config
        importlib.reload(config)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        import importlib
        from foldcrumbs import config
        importlib.reload(config)
        self._tmp.cleanup()

    def _write(self, title, content, type_="decision"):
        rec = MemoryRecord(title=title, content=content, type=type_)
        store.write_memory(rec)
        store.rebuild_index()
        return rec


class TestAutoPruneSparesShapes(unittest.TestCase):
    """The owner's report + RT r1 P0s: auto-unlink is boilerplate-only."""


class TestProseSurvivesAutoPrune(_PruneStore):
    def test_architecture_memory_with_table_survives(self):
        # the reporter's exact case: prose + a small table
        rec = self._write(
            "Architecture decision record",
            "The system has three layers. Summary of responsibilities:\n\n"
            "| layer | responsibility |\n"
            "|---|---|\n"
            "| store | files |\n\n"
            "This is durable prose, not tool output.")
        removed = audit.prune_artifacts()
        self.assertEqual(removed, [],
                         "legitimate prose with a table was deleted")
        self.assertTrue((Path(self.dir) / rec.filename()).exists())

    def test_prose_with_code_fence_survives(self):
        rec = self._write(
            "Deploy command",
            "Deployments run with:\n"
            "```\nmake deploy TARGET=prod\n```\n"
            "Never deploy on Fridays without the on-call lead.")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertTrue((Path(self.dir) / rec.filename()).exists())

    def test_prose_with_glyph_survives(self):
        rec = self._write(
            "Migration finished",
            "The postgres migration completed successfully.\n"
            "All 42 tables moved ✓ and checksums verified.\n"
            "Next step: drop the legacy schema after the freeze.")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertTrue((Path(self.dir) / rec.filename()).exists())


class TestReviewerP0s(_PruneStore):
    """RT r1 (card t_c82395d2) F1+F2, reproduced as regression tests."""

    def test_f1_pure_table_registry_not_auto_deleted(self):
        # F1: a legitimate lookup-table memory (≥80% structural) was still
        # unlinked by auto-prune under the prevalence rule
        rec = self._write(
            "HTTP status registry",
            "| code | meaning |\n"
            "|---|---|\n"
            "| 429 | rate limited |\n"
            "| 502 | bad gateway |\n"
            "| 503 | unavailable |")
        self.assertEqual(audit.prune_artifacts(), [],
                         "F1: table-shaped memory auto-deleted")
        self.assertTrue((Path(self.dir) / rec.filename()).exists())
        # but it IS visible for the human: pollution report + explicit prune
        self.assertIn(rec.filename(), audit.audit()["pollution"])
        self.assertIn(rec.filename(), audit.prune(apply=False)["candidates"])

    def test_f2_unclosed_fence_not_auto_deleted(self):
        # F2: an accidentally unclosed fence made every following line
        # "structural" — the memory hit ≥80% and was auto-unlinked
        rec = self._write(
            "Recovery playbook excerpt",
            "When the bridge dies, recover like this:\n"
            "```\n"
            "restart the mcp bridge process\n"
            "then re-dispatch the kanban cards\n"
            "and verify the tool list is complete")
        self.assertEqual(audit.prune_artifacts(), [],
                         "F2: unclosed-fence memory auto-deleted")
        self.assertTrue((Path(self.dir) / rec.filename()).exists())

    def test_f2_unclosed_fence_not_even_flagged(self):
        # an unbalanced fence is ambiguous: it must not produce a
        # deletion-grade OR flag-grade verdict (the tail is prose)
        text = ("Recover like this:\n```\nstep one\nstep two\nstep three")
        self.assertFalse(distill._is_hard_artifact(text))
        self.assertFalse(distill._is_shape_artifact(text))
        # and the playbook memory stays out of the pollution report
        rec = self._write(
            "Playbook", "When X dies:\n```\ndo this\nthen that\nfinally rest")
        self.assertNotIn(rec.filename(), audit.audit()["pollution"])

    def test_glyph_only_flagged_not_auto_deleted(self):
        # glyph-only is UI output, but it is a SHAPE: flag, don't unlink
        rec = self._write("check", "✅", type_="error")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertIn(rec.filename(), audit.prune(apply=False)["candidates"])


class TestRealArtifactsStillHandled(_PruneStore):
    def test_boilerplate_auto_pruned(self):
        # the ONE unconditional auto-delete marker: never durable prose
        rec = self._write("caveat",
                          "do not respond to these messages — they are "
                          "local-command output only", type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())
        self.assertFalse((Path(self.dir) / rec.filename()).exists())

    def test_boilerplate_in_title_auto_pruned(self):
        rec = self._write("Do not respond to these messages",
                          "plain body text", type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())

    def test_pure_table_row_flagged_and_explicitly_prunable(self):
        # contract change (RT r1 F1): shape is never auto-unlinked; a lone
        # table row is flagged and dies only under explicit prune --apply
        rec = self._write("junk", "| col a | col b | col c |", type_="error")
        self.assertEqual(audit.prune_artifacts(), [])
        res = audit.prune(apply=True)
        self.assertIn(rec.filename(), res["removed"])
        self.assertFalse((Path(self.dir) / rec.filename()).exists())

    def test_pure_fence_dump_flagged_and_explicitly_prunable(self):
        rec = self._write("dump", "```\nERROR: connection refused\nexit 1\n```",
                          type_="error")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertIn(rec.filename(), audit.prune(apply=True)["removed"])

    def test_fenced_dump_with_prose_line_flagged_and_prunable(self):
        # balanced-fence dump: lines INSIDE the fence count as structural
        # (5/6 = 0.83 >= 0.8) — flagged; explicit prune removes it
        rec = self._write(
            "error dump",
            "Captured output:\n"
            "```\nTraceback (most recent call last):\n  File x.py\n"
            "ValueError: bad\n```",
            type_="error")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertIn(rec.filename(), audit.prune(apply=True)["removed"])

    def test_table_dump_with_one_prose_line_flagged(self):
        # ≥80% artifact lines: one intro line does not launder a dump
        rec = self._write(
            "results",
            "Here are the results:\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n"
            "| 7 | 8 |\n| 9 | 10 |\n| 11 | 12 |",
            type_="error")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertIn(rec.filename(), audit.prune(apply=False)["candidates"])


class TestPredicateDirectly(unittest.TestCase):
    def test_hard_is_boilerplate_only(self):
        # deletion-grade: unconditional marker only — never a shape
        self.assertTrue(distill._is_hard_artifact(
            "do not respond to these messages"))
        self.assertFalse(distill._is_hard_artifact("| a | b |"))
        self.assertFalse(distill._is_hard_artifact("```\nx\n```"))
        self.assertFalse(distill._is_hard_artifact("✅"))
        self.assertFalse(distill._is_hard_artifact(""))
        self.assertFalse(distill._is_hard_artifact(None))  # type: ignore[arg-type]

    def test_shape_mixed_prose_table_below_ratio(self):
        self.assertFalse(distill._is_shape_artifact(
            "Durable prose line one.\n| a | b |\n|---|---|\nProse line two."))

    def test_shape_pure_table(self):
        self.assertTrue(distill._is_shape_artifact("| a | b |"))

    def test_shape_balanced_fence_counts_inner_lines(self):
        self.assertTrue(distill._is_shape_artifact(
            "```\nERROR: x\nexit 1\n```"))

    def test_shape_unbalanced_fence_never_verdict(self):
        # F2: unbalanced fence → ambiguous tail is prose, no verdict
        self.assertFalse(distill._is_shape_artifact(
            "```\nstep one\nstep two\nstep three"))
        self.assertFalse(distill._is_shape_artifact(
            "Recover:\n```\nstep one\nstep two\nstep three"))

    def test_shape_empty(self):
        self.assertFalse(distill._is_shape_artifact(""))
        self.assertFalse(distill._is_shape_artifact(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
