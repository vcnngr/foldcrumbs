"""Auto-prune must not delete legitimate prose memories (data-loss bug).

Reported 2026-09-18 via the owner: auto-prune deleted an architecture
memory TWICE because it contained a markdown table — while the comment
above _HARD_ARTIFACT_RE says the deletion subset must only contain
patterns that are "never legitimate durable prose". Tables and code
fences appear in genuine memories (foldcrumbs' own AGENTS.md is full of
tables). Presence of a pattern is not prevalence of tool output.

Contract pinned here (deletion bias: false-negative = junk stays,
cheap and visible; false-positive = data loss, unrecoverable):
- prose memory CONTAINING a table or a code fence → survives prune
- pure tool-output dump (table-only / fence-only / ≥80% artifact
  lines) → still pruned (the existing test_auto_prune_on_persist
  contract: a lone table row IS junk)
- glyph/boilerplate single-liners → pruned (behavior preserved)
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
        return rec


class TestProseWithTablesSurvives(_PruneStore):
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
        # same defect class: fences are in _HARD_ARTIFACT_RE too, and a
        # durable memory quoting a command is legitimate prose
        rec = self._write(
            "Deploy command",
            "Deployments run with:\n"
            "```\nmake deploy TARGET=prod\n```\n"
            "Never deploy on Fridays without the on-call lead.")
        removed = audit.prune_artifacts()
        self.assertEqual(removed, [])
        self.assertTrue((Path(self.dir) / rec.filename()).exists())

    def test_prose_with_glyph_survives(self):
        rec = self._write(
            "Migration finished",
            "The postgres migration completed successfully.\n"
            "All 42 tables moved ✓ and checksums verified.\n"
            "Next step: drop the legacy schema after the freeze.")
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertTrue((Path(self.dir) / rec.filename()).exists())


class TestRealArtifactsStillPruned(_PruneStore):
    def test_pure_table_row_still_pruned(self):
        # existing contract (test_auto_prune_on_persist): a lone table
        # row with no prose IS tool-output junk
        rec = self._write("junk", "| col a | col b | col c |", type_="error")
        removed = audit.prune_artifacts()
        self.assertIn(rec.filename(), removed)
        self.assertFalse((Path(self.dir) / rec.filename()).exists())

    def test_pure_fence_dump_still_pruned(self):
        rec = self._write("dump", "```\nERROR: connection refused\nexit 1\n```",
                          type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())

    def test_tool_dump_with_one_prose_line_still_pruned(self):
        # ≥80% artifact lines: one intro line does not launder a dump
        rec = self._write(
            "results",
            "Here are the results:\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n"
            "| 7 | 8 |\n| 9 | 10 |\n| 11 | 12 |",
            type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())

    def test_fenced_dump_with_prose_line_still_pruned(self):
        # lines INSIDE the fence count as structural: one intro line does
        # not launder a dump (5/6 structural = 0.83 >= 0.8)
        rec = self._write(
            "error dump",
            "Captured output:\n"
            "```\nTraceback (most recent call last):\n  File x.py\n"
            "ValueError: bad\n```",
            type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())

    def test_glyph_only_line_still_pruned(self):
        # behavior preserved: a bare status-glyph line is UI output
        rec = self._write("check", "✅", type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())

    def test_boilerplate_still_pruned(self):
        rec = self._write("caveat",
                          "do not respond to these messages — they are "
                          "local-command output only", type_="error")
        self.assertIn(rec.filename(), audit.prune_artifacts())


class TestPredicateDirectly(unittest.TestCase):
    def test_mixed_prose_table_below_ratio(self):
        self.assertFalse(distill._is_hard_artifact(
            "Durable prose line one.\n| a | b |\n|---|---|\nProse line two."))

    def test_pure_table(self):
        self.assertTrue(distill._is_hard_artifact("| a | b |"))

    def test_empty(self):
        self.assertFalse(distill._is_hard_artifact(""))
        self.assertFalse(distill._is_hard_artifact(None))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
