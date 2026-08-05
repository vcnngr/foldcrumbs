"""Tests for expiring memories — the EXPIRING class (stdlib unittest).

The contract under test:
* a memory carries an optional ``expires_at`` (round-trips through frontmatter);
* past it, the memory is invisible everywhere an archived one is — recall,
  the index, dedup — while its file stays untouched;
* nothing expires implicitly: no date, no unparseable date, no expiry;
* a bare date means the END of that day;
* the decay sweep archives lapsed memories (dry-run by default), and says so;
* expiry is set only by explicit user intent (CLI ``--expires``).
"""

import contextlib
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: this module is routinely run on its own, and without the
# sandbox its stores would resolve to the developer's real ~/.claude.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import cli, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


def _utc(year, month, day, hour=23, minute=59, second=59):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


class TestExpirySchema(unittest.TestCase):
    """Frontmatter round-trip and the live is_expired predicate."""

    def test_roundtrip(self):
        r = MemoryRecord(title="Trial", content="The trial ends soon.",
                         type="fact", expires_at=_utc(2026, 9, 1))
        back = MemoryRecord.from_markdown(r.to_markdown())
        self.assertEqual(back.expires_at, _utc(2026, 9, 1))
        self.assertIn("expires_at: 2026-09-01T23:59:59+00:00", r.to_markdown())

    def test_absent_date_means_never_expires(self):
        r = MemoryRecord(title="Permanent", content="No date here.", type="fact")
        self.assertIsNone(r.expires_at)
        self.assertFalse(r.is_expired)
        back = MemoryRecord.from_markdown(r.to_markdown())
        self.assertIsNone(back.expires_at)
        self.assertNotIn("expires_at", back.to_markdown())

    def test_unparseable_date_degrades_to_no_expiry(self):
        r = MemoryRecord(title="X", content="Y", type="fact",
                         expires_at=_utc(2026, 9, 1))
        text = r.to_markdown().replace(
            "expires_at: 2026-09-01T23:59:59+00:00", "expires_at: next tuesday")
        back = MemoryRecord.from_markdown(text)
        self.assertIsNone(back.expires_at, "a hand-edited date must not break the record")
        self.assertFalse(back.is_expired)

    def test_is_expired_is_live_and_inclusive(self):
        past = MemoryRecord(title="P", content="c", expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        future = MemoryRecord(title="F", content="c", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        self.assertTrue(past.is_expired)
        self.assertFalse(future.is_expired)
        # Exactly at the boundary: the memory has had its full day.
        edge = MemoryRecord(title="E", content="c",
                            expires_at=datetime.now(timezone.utc))
        self.assertTrue(edge.is_expired)


class TestParseExpiry(unittest.TestCase):
    """CLI parsing: ISO dates, datetimes, relative offsets."""

    def test_bare_date_means_end_of_day(self):
        dt = cli.parse_expiry("2026-09-01")
        self.assertEqual((dt.hour, dt.minute, dt.second), (23, 59, 59))
        self.assertEqual(dt.date(), datetime(2026, 9, 1).date())
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_explicit_datetime_is_kept(self):
        dt = cli.parse_expiry("2026-09-01T08:30:00")
        self.assertEqual((dt.hour, dt.minute), (8, 30))

    def test_relative_days_weeks_months(self):
        now = datetime.now(timezone.utc)
        d30 = cli.parse_expiry("30d")
        w2 = cli.parse_expiry("2w")
        m6 = cli.parse_expiry("6m")
        self.assertAlmostEqual((d30 - now).days, 30, delta=1)
        self.assertAlmostEqual((w2 - now).days, 14, delta=1)
        self.assertAlmostEqual((m6 - now).days, 180, delta=1)
        # Relative offsets also land on end-of-day.
        self.assertEqual((d30.hour, d30.minute), (23, 59))

    def test_garbage_raises_with_the_value(self):
        for bad in ("next tuesday", "2026-13-45", "d30", ""):
            with self.assertRaises(ValueError, msg=bad):
                cli.parse_expiry(bad)


class TestExpiryVisibility(TmpStore):
    """An expired memory is invisible everywhere an archived one is."""

    def _put(self, title, body, expires_at=None, days_old=0):
        rec = MemoryRecord(title=title, content=body, type="fact",
                           expires_at=expires_at, confidence=0.95)
        if days_old:
            rec.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
            rec.updated_at = rec.created_at
        store.write_memory(rec)
        return rec

    def test_expired_memory_disappears_from_recall(self):
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        self._put("Deploy day", "Deployment runs on Tuesdays.")
        hits = store.search("trial license", federated=False)
        self.assertEqual(hits, [], "an expired memory was still recalled")
        self.assertEqual([m.title for m in
                          store.search("deployment tuesdays", federated=False)],
                         ["Deploy day"])
        # The file is untouched — expiry is visibility, not deletion.
        self.assertTrue((Path(self.dir) / lapsed.filename()).is_file())
        self.assertEqual(store.get(lapsed.filename()).status, "active")

    def test_not_yet_expired_still_answers(self):
        self._put("Trial terms", "The trial license runs until September.",
                  expires_at=datetime.now(timezone.utc) + timedelta(days=30))
        self.assertEqual([m.title for m in
                          store.search("trial license", federated=False)],
                         ["Trial terms"])

    def test_expired_memory_leaves_the_index(self):
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        index = store.rebuild_index().read_text()
        self.assertNotIn(lapsed.filename(), index)

    def test_expired_memory_is_not_a_dedup_target(self):
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        # Near-identical text: with a live duplicate this would validate, not
        # create. An expired one must not absorb the new memory.
        fresh = MemoryRecord(title="Trial terms", content="The trial license expires in June.",
                             type="fact")
        action, _ = store.upsert(fresh)
        self.assertEqual(action, "created",
                         "an expired memory absorbed a new one as a duplicate")
        self.assertEqual(store.get(lapsed.filename()).validation_count, 0)

    def test_end_of_day_semantics_in_recall(self):
        # Expires today, end-of-day: must still be visible at any time today.
        now = datetime.now(timezone.utc)
        eod = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if eod <= now:                      # running in the last second of UTC
            eod += timedelta(days=1)
        self._put("Trial terms", "The trial license is active.", expires_at=eod)
        self.assertEqual([m.title for m in
                          store.search("trial license", federated=False)],
                         ["Trial terms"], "the deadline day itself was lost")


class TestExpirySweep(TmpStore):
    """decay archives lapsed memories — dry-run first, and says why."""

    def _put(self, title, body, expires_at=None):
        rec = MemoryRecord(title=title, content=body, type="fact",
                           expires_at=expires_at, confidence=0.95)
        store.write_memory(rec)
        return rec

    def test_lapsed_memory_is_a_decay_candidate_despite_high_trust(self):
        from foldcrumbs import audit
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        res = audit.decay()
        self.assertIn(lapsed.filename(), res["candidates"])
        self.assertIn(lapsed.filename(), res["expired"])
        # High-trust and fresh would otherwise never be a candidate.
        self.assertEqual(store.get(lapsed.filename()).status, "active",
                         "a dry run changed the store")

    def test_dry_run_then_apply_archives_it(self):
        from foldcrumbs import audit
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        # Already invisible (expiry hides it the moment the date passes);
        # still active on disk — the sweep is what makes that durable.
        self.assertEqual(store.search("trial license", federated=False), [])
        self.assertEqual(store.get(lapsed.filename()).status, "active")
        res = audit.decay(apply=True)
        self.assertEqual(res["archived"], [lapsed.filename()])
        self.assertEqual(store.get(lapsed.filename()).status, "archived")
        self.assertTrue((Path(self.dir) / lapsed.filename()).is_file(),
                        "archiving deleted the file")

    def test_heal_index_drops_a_lapsed_memory_from_the_index(self):
        from foldcrumbs import audit
        lapsed = self._put("Trial terms", "The trial license expires in June.",
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        store.rebuild_index()
        self.assertNotIn(lapsed.filename(),
                         config_index_text(), "fixture: not in index to begin with")
        # Simulate an index written before the date passed: inject the link.
        p = store.rebuild_index()
        p.write_text(p.read_text() +
                     f"\n- [{lapsed.title}]({lapsed.filename()}) — stale link\n")
        self.assertTrue(audit.heal_index(),
                        "a lapsed memory left in the index did not trigger a heal")
        self.assertNotIn(lapsed.filename(), p.read_text())

    def test_cli_decay_marks_lapsed_as_expired(self):
        import argparse
        self._put("Trial terms", "The trial license expires in June.",
                  expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_decay(argparse.Namespace(apply=False))
        out = buf.getvalue()
        self.assertIn("(expired)", out)
        self.assertIn("--apply", out)

    def test_remember_with_expires_writes_the_field(self):
        import argparse
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_remember(argparse.Namespace(
                text="The trial runs until September.", title="Trial terms",
                type="fact", confidence=0.85, tag=None, expires="2026-09-01"))
        recs = store.load_all()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].expires_at,
                         datetime(2026, 9, 1, 23, 59, 59, tzinfo=timezone.utc))

    def test_remember_rejects_a_bad_date_without_writing(self):
        import argparse
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_remember(argparse.Namespace(
                text="Never stored.", title="X", type="fact",
                confidence=0.85, tag=None, expires="next tuesday"))
        self.assertEqual(rc, 1)
        self.assertEqual(store.load_all(), [])
        self.assertIn("not a date", buf.getvalue())


def config_index_text():
    from foldcrumbs import config
    p = config.index_path()
    return p.read_text() if p.exists() else ""


if __name__ == "__main__":
    unittest.main()
