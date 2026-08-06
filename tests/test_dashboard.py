"""Tests for the dashboard: data collection + self-contained rendering.

The contract under test:
* collect() is pure — it reads the store, writes nothing;
* every panel reflects real store state (nothing invented);
* render() produces one HTML page with no external references (no http://,
  no src=, no CDN) — the page must work offline and never phone home;
* memory names link to real files;
* the CLI command supports --json and --out without opening a browser.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: this module is routinely run on its own; without the
# sandbox the dashboard would read the developer's real store and roots.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import dashboard, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class TestCollect(TmpStore):
    def _put(self, title, body, type_="fact", confidence=0.85):
        rec = MemoryRecord(title=title, content=body, type=type_,
                           confidence=confidence)
        store.write_memory(rec)
        return rec

    def test_collect_counts_real_status(self):
        self._put("One", "First.")
        two = self._put("Two", "Second.")
        store.forget(two.filename())
        data = dashboard.collect()
        self.assertEqual(data["store"]["total"], 2)
        self.assertEqual(data["store"]["by_status"], {"active": 1, "deleted": 1})

    def test_collect_writes_nothing(self):
        self._put("One", "First.")
        before = sorted(p.name for p in Path(self.dir).iterdir())
        dashboard.collect()
        after = sorted(p.name for p in Path(self.dir).iterdir())
        self.assertEqual(before, after, "collect() must be read-only")

    def test_decay_panel_mirrors_the_sweep(self):
        stale = MemoryRecord(title="Old rule", content="Nobody follows this.",
                             type="fact", confidence=0.1,
                             provenance="inferred", validation_count=0)
        from datetime import datetime, timedelta, timezone
        stale.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        stale.updated_at = stale.created_at
        store.write_memory(stale)
        data = dashboard.collect()
        self.assertIn(stale.filename(),
                      [c["name"] for c in data["decay"]["candidates"]])

    def test_superseded_panel_shows_the_chain(self):
        old = self._put("Old decision", "We defer publishing.")
        new = self._put("New fact", "We published.")
        store.supersede(old.filename(), new.filename())
        data = dashboard.collect()
        self.assertEqual(len(data["superseded"]), 1)
        self.assertEqual(data["superseded"][0]["old"], old.filename())
        self.assertEqual(data["superseded"][0]["new"], new.filename())
        self.assertTrue(data["superseded"][0]["found"])

    def test_reinforcement_panel_counts_recalls(self):
        wanted = self._put("Lockfile", "The lockfile is committed.")
        self._put("Quiet", "Never recalled.")
        store.search("lockfile", federated=False)
        store.search("lockfile", federated=False)
        data = dashboard.collect()
        rf = data["reinforcement"]
        self.assertEqual(rf["total_recalls"], 2)
        self.assertEqual(rf["top"][0]["name"], wanted.filename())
        self.assertEqual(rf["never_recalled"], 1)

    def test_trust_buckets_cover_only_active(self):
        self._put("High", "Sure thing.", confidence=0.95)
        dead = self._put("Dead", "Gone.", confidence=0.95)
        store.forget(dead.filename())
        data = dashboard.collect()
        self.assertEqual(sum(data["trust"]["buckets"]), 1)

    def test_collect_is_json_serializable(self):
        self._put("One", "First.")
        json.dumps(dashboard.collect(), default=str)  # must not raise


class TestRender(TmpStore):
    def _put(self, title, body, type_="fact"):
        rec = MemoryRecord(title=title, content=body, type=type_)
        store.write_memory(rec)
        return rec

    def test_page_is_self_contained(self):
        rec = self._put("Deploy", "Deploys run on Tuesdays.")
        page = dashboard.render(dashboard.collect())
        # No external fetches of any kind: the page must work offline.
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("src=", page)
        self.assertNotIn("<script", page)
        self.assertIn("<style>", page)
        # The page names at least one real memory and links its file.
        self.assertIn(rec.filename(), page)

    def test_memory_names_link_to_real_files(self):
        rec = self._put("Deploy", "Deploys run on Tuesdays.")
        page = dashboard.render(dashboard.collect())
        real = (Path(self.dir) / rec.filename()).resolve()
        self.assertIn(f'file://{real}', page)

    def test_titles_are_escaped(self):
        # Ampersand survives clean_line and is a real injection probe.
        self._put("R&D rules", "Content here.")
        page = dashboard.render(dashboard.collect())
        self.assertNotIn("R&D rules", page)
        self.assertIn("R&amp;D rules", page)

    def test_empty_store_renders(self):
        page = dashboard.render(dashboard.collect())
        self.assertIn("foldcrumbs", page.lower())


class TestCliDashboard(TmpStore):
    def _put(self, title, body, type_="fact"):
        rec = MemoryRecord(title=title, content=body, type=type_)
        store.write_memory(rec)
        return rec

    def test_json_mode_prints_data_not_html(self):
        import argparse
        from foldcrumbs import cli
        self._put("One", "First.")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_dashboard(argparse.Namespace(
                json=True, out="", no_open=True))
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["store"]["by_status"]["active"], 1)

    def test_out_writes_the_page_and_skips_the_browser(self):
        import argparse
        from foldcrumbs import cli
        self._put("One", "First.")
        target = Path(self._state) / "dash.html"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_dashboard(argparse.Namespace(
                json=False, out=str(target), no_open=True))
        self.assertEqual(rc, 0)
        self.assertTrue(target.is_file())
        self.assertIn("<!DOCTYPE html>", target.read_text())

    def test_no_open_temp_file_does_not_open_a_browser(self):
        import argparse
        from foldcrumbs import cli
        import webbrowser
        opened = []
        real_open = webbrowser.open
        webbrowser.open = lambda url: opened.append(url)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli._cmd_dashboard(argparse.Namespace(
                    json=False, out="", no_open=True))
        finally:
            webbrowser.open = real_open
        self.assertEqual(opened, [], "--no-open must not open a browser")


if __name__ == "__main__":
    unittest.main()
