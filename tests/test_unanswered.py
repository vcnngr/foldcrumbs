"""Unanswered-query log: observation only, never changes a result."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from foldcrumbs import config, unanswered


class TestUnansweredLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.STATE_DIR
        config.STATE_DIR = Path(self._tmp.name) / "state"

    def tearDown(self):
        config.STATE_DIR = self._old
        self._tmp.cleanup()

    def test_record_appends_one_json_line_per_query(self):
        self.assertTrue(unanswered.record("quando è il deploy?"))
        self.assertTrue(unanswered.record("chi è Tobia?"))
        rows = unanswered.read()
        self.assertEqual([r["query"] for r in rows], ["chi è Tobia?", "quando è il deploy?"])

    def test_blank_query_is_not_a_question(self):
        self.assertFalse(unanswered.record("   "))
        self.assertFalse(unanswered.record(""))
        self.assertFalse(unanswered.log_path().exists())

    def test_query_is_truncated_and_timestamped(self):
        unanswered.record("x" * 1000, now=datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc))
        row = unanswered.read()[0]
        self.assertEqual(len(row["query"]), unanswered.MAX_QUERY_CHARS)
        self.assertEqual(row["at"], "2026-11-12T09:00:00+00:00")

    def test_top_gaps_counts_case_insensitively(self):
        for q in ["Deploy?", "deploy?", "DEPLOY?", "altro"]:
            unanswered.record(q)
        gaps = unanswered.top_gaps(5)
        self.assertEqual(gaps[0][1], 3)
        self.assertEqual(gaps[0][0].casefold(), "deploy?")
        self.assertEqual(gaps[1], ("altro", 1))

    def test_malformed_line_is_skipped_not_fatal(self):
        unanswered.record("valida")
        with unanswered.log_path().open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual([r["query"] for r in unanswered.read()], ["valida"])

    def test_unwritable_state_dir_never_raises(self):
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("not a dir")
        config.STATE_DIR = blocker
        self.assertFalse(unanswered.record("qualsiasi"))

    def test_read_of_missing_log_is_empty(self):
        self.assertEqual(unanswered.read(), [])
        self.assertEqual(unanswered.top_gaps(), [])

    def test_logging_never_changes_the_recall_result(self):
        """Contract: observation only. The served text must be identical with
        the log on and off. Measured on the real MCP tool, on an empty store."""
        from foldcrumbs import mcp_server
        args = {"query": "domanda senza risposta xyz123"}
        with_log = mcp_server.tool_recall(dict(args))
        self.assertTrue(unanswered.read(), "the empty recall should have been logged")
        before = len(unanswered.read())
        with mock.patch.object(unanswered, "record", return_value=False):
            without_log = mcp_server.tool_recall(dict(args))
        self.assertEqual(with_log, without_log)
        self.assertEqual(len(unanswered.read()), before)
        # Pinned expectation, not a self-comparison: a mutation that changes the
        # served text on the empty path must fail this test.
        self.assertEqual(with_log, "(no matching memories)")


if __name__ == "__main__":
    unittest.main()
