"""Tests for the reconciliation queue (foldcrumbs conflicts).

The contract under test:
* the contradiction pass has THREE verdicts now: supersede, coexist, flag —
  and an unsure/garbled answer is flagged, never guessed;
* flagged pairs persist machine-locally, dedup, and drop out once either side
  is retired;
* the queue surfaces claims this store has made on foreign memories, and
  claims other instances have made on ours;
* nothing in the queue path writes the store.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: this module is routinely run on its own, and the queue
# writes into STATE_DIR — without the sandbox it would be the developer's
# real ~/.foldcrumbs.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import conflicts, distill, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class TestVerdictParsing(unittest.TestCase):
    """supersede / coexist / flag / None — and the legacy spelling."""

    def test_new_spelling(self):
        self.assertEqual(distill.parse_supersede_verdict('{"verdict": "supersede"}'), "supersede")
        self.assertEqual(distill.parse_supersede_verdict('{"verdict": "coexist"}'), "coexist")
        self.assertEqual(distill.parse_supersede_verdict('{"verdict": "flag"}'), "flag")

    def test_legacy_spelling_still_works(self):
        self.assertEqual(distill.parse_supersede_verdict('{"supersedes": true}'), "supersede")
        self.assertEqual(distill.parse_supersede_verdict('{"supersedes": false}'), "coexist")

    def test_no_answer_is_not_a_flag(self):
        # An offline machine must not flood the queue.
        self.assertIsNone(distill.parse_supersede_verdict(None))
        self.assertIsNone(distill.parse_supersede_verdict(""))

    def test_garbled_answer_is_a_flag(self):
        # An answer that is neither verdict is confusion — surface it.
        self.assertEqual(distill.parse_supersede_verdict("hmm, not sure"), "flag")
        self.assertEqual(distill.parse_supersede_verdict('{"verdict": "maybe"}'), "flag")

    def test_case_insensitive(self):
        self.assertEqual(distill.parse_supersede_verdict('{"verdict": "SUPERSEDE"}'), "supersede")
        self.assertEqual(distill.parse_supersede_verdict('{"SUPERSEDES": TRUE}'), "supersede")


class TestFlaggedQueue(TmpStore):
    def _put(self, title, body, type_="fact"):
        rec = MemoryRecord(title=title, content=body, type=type_)
        store.write_memory(rec)
        return rec

    def test_flag_pair_persists_and_dedups(self):
        a = self._put("Old note", "The first statement.")
        b = self._put("New note", "The second statement.")
        conflicts.flag_pair(a.filename(), b.filename())
        conflicts.flag_pair(a.filename(), b.filename())
        self.assertEqual(len(conflicts.flagged_pairs()), 1)
        # Machine-local: inside the sandbox state dir, never the store.
        self.assertTrue(str(conflicts._path()).startswith(self._state))

    def test_a_pair_survives_a_reload_of_the_queue(self):
        a = self._put("Old note", "The first statement.")
        b = self._put("New note", "The second statement.")
        conflicts.flag_pair(a.filename(), b.filename())
        self.assertEqual(conflicts.flagged_pairs()[0]["old"], a.filename())
        self.assertEqual(conflicts.flagged_pairs()[0]["new"], b.filename())

    def test_a_pair_whose_new_side_is_gone_drops_out(self):
        old = self._put("Old note", "The first statement.")
        kept = self._put("New note", "The second statement.")
        conflicts.flag_pair(old.filename(), kept.filename())
        self.assertEqual(len(conflicts.flagged_pairs()), 1)
        store.forget(kept.filename(), hard=True)
        self.assertEqual(conflicts.flagged_pairs(), [],
                         "a pair survived the disappearance of one side")

    def test_a_pair_whose_old_side_is_gone_drops_out(self):
        kept = self._put("Old note", "The first statement.")
        fresh = self._put("New note", "The second statement.")
        conflicts.flag_pair(kept.filename(), fresh.filename())
        self.assertEqual(len(conflicts.flagged_pairs()), 1)
        store.forget(kept.filename(), hard=True)
        self.assertEqual(conflicts.flagged_pairs(), [])

    def test_corrupt_queue_degrades_to_empty(self):
        conflicts._path().parent.mkdir(parents=True, exist_ok=True)
        conflicts._path().write_text("{not json", encoding="utf-8")
        self.assertEqual(conflicts.flagged_pairs(), [])

    def test_format_queue_suggests_the_supersede_command(self):
        a = self._put("Old note", "The first statement.")
        b = self._put("New note", "The second statement.")
        conflicts.flag_pair(a.filename(), b.filename())
        out = conflicts.format_queue(conflicts.queue())
        self.assertIn(a.filename(), out)
        self.assertIn(b.filename(), out)
        self.assertIn(f"foldcrumbs supersede {a.filename()} --by {b.filename()}", out)

    def test_format_queue_empty(self):
        out = conflicts.format_queue(conflicts.queue())
        self.assertIn("no conflicts", out)


class TestAutoSupersedeFlag(TmpStore):
    """The distill path records a flag instead of guessing."""

    def _old(self):
        rec = MemoryRecord(title="PyPI publishing deferred",
                           content="Publishing to PyPI is deferred for now.",
                           type="decision")
        store.write_memory(rec)
        return rec

    def _new(self):
        return MemoryRecord(title="Published to PyPI",
                            content="Publishing to PyPI is done and released.",
                            type="fact")

    def _run(self, answer):
        # Mirror the real flow: persist() writes the fresh record first,
        # then asks about same-subject pairs — so the new side must be on
        # disk before the pass runs.
        fresh = self._new()
        store.write_memory(fresh)
        real_chat = distill.llm.chat
        distill.llm.chat = lambda *a, **k: answer
        try:
            return distill._auto_supersede([fresh])
        finally:
            distill.llm.chat = real_chat

    def test_flag_verdict_changes_nothing_in_the_store(self):
        old = self._old()
        n = self._run('{"verdict": "flag"}')
        self.assertEqual(n, 0)
        self.assertEqual(store.get(old.filename()).status, "active")
        q = conflicts.queue()
        self.assertEqual(len(q["flagged"]), 1)
        self.assertEqual(q["flagged"][0]["old"], old.filename())

    def test_garbled_answer_is_flagged_not_guessed(self):
        self._old()
        n = self._run("sorry, I cannot tell")
        self.assertEqual(n, 0)
        self.assertEqual(len(conflicts.queue()["flagged"]), 1)

    def test_no_answer_flags_nothing(self):
        old = self._old()
        n = self._run(None)
        self.assertEqual(n, 0)
        self.assertEqual(conflicts.queue()["flagged"], [])
        self.assertEqual(store.get(old.filename()).status, "active")

    def test_coexist_verdict_keeps_both(self):
        old = self._old()
        n = self._run('{"verdict": "coexist"}')
        self.assertEqual(n, 0)
        self.assertEqual(store.get(old.filename()).status, "active")
        self.assertEqual(conflicts.queue()["flagged"], [])

    def test_supersede_verdict_still_supersedes(self):
        old = self._old()
        n = self._run('{"verdict": "supersede"}')
        self.assertEqual(n, 1)
        self.assertEqual(store.get(old.filename()).status, "superseded")

    def test_legacy_true_still_supersedes(self):
        old = self._old()
        n = self._run('{"supersedes": true}')
        self.assertEqual(n, 1)
        self.assertEqual(store.get(old.filename()).status, "superseded")


class TestCliConflicts(TmpStore):
    def _put(self, title, body, type_="fact"):
        rec = MemoryRecord(title=title, content=body, type=type_)
        store.write_memory(rec)
        return rec

    def test_conflicts_command_shows_a_flagged_pair(self):
        import argparse
        from foldcrumbs import cli
        old = self._put("Old note", "The first statement.")
        kept = self._put("New note", "The second statement.")
        conflicts.flag_pair(old.filename(), kept.filename())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_conflicts(argparse.Namespace())
        out = buf.getvalue()
        self.assertIn(old.filename(), out)
        self.assertIn(kept.filename(), out)

    def test_conflicts_command_empty_store(self):
        import argparse
        from foldcrumbs import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_conflicts(argparse.Namespace())
        self.assertIn("no conflicts", buf.getvalue())

    def test_claims_out_are_listed(self):
        import argparse
        from foldcrumbs import cli
        rec = MemoryRecord(title="Published to PyPI", content="Done.", type="fact",
                           supersedes_external=["abcdef0123456789:decision_old.md"])
        store.write_memory(rec)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_conflicts(argparse.Namespace())
        out = buf.getvalue()
        self.assertIn("decision_old.md", out)
        self.assertIn(rec.filename(), out)


if __name__ == "__main__":
    unittest.main()
