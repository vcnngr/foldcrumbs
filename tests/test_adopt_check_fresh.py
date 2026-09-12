"""adopt --check-fresh: stale-adoption detection (FL backlog, paper
2609.03340 "Fresh Memory, Stale Plans").

The ledger already attests root_id + memory_id + filename + adopted_at
for every adoption. check_fresh answers the question the ledger cannot:
is the SOURCE still alive and unchanged since we copied it? Read-only —
it never touches the local copy, never writes the ledger, never syncs.
Freshness is information, not automation (fleet-learning design: no
automatic sync, ever).
"""

import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

sys.path.insert(0, str(REPO / "tests"))

from test_adopt import _AdoptEnv  # noqa: E402

from foldcrumbs import adopt as adopt_mod  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class CheckFreshEnv(_AdoptEnv):
    """Adopt one memory from 'theirs', then mutate the source."""

    def setUp(self):
        super().setUp()
        self.src = self._theirs(title="Cache TTL",
                                content="Cache TTL is 300 seconds.",
                                type_="decision")
        res = adopt_mod.adopt(f"{self.theirs.id}:{self.src.filename()}",
                              cwd=self.proj, note="fresh-check fixture")
        self.assertTrue(res["ok"])
        self.copy_name = res["filename"]
        self.copy_id = res["id"]

    def _statuses(self):
        return {r["status"]: r for r in adopt_mod.check_fresh(cwd=self.proj)}


class TestCheckFreshHappy(CheckFreshEnv):
    def test_unchanged_source_is_fresh(self):
        rows = adopt_mod.check_fresh(cwd=self.proj)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["status"], "fresh")
        self.assertEqual(r["memory_id"], self.copy_id)
        self.assertEqual(r["source_root"], self.theirs.id)

    def test_read_only_no_writes(self):
        copy_path = self.my_dir / self.copy_name
        before = copy_path.read_bytes()
        ledger_path = self._ledger_path()
        led_before = ledger_path.read_bytes()
        adopt_mod.check_fresh(cwd=self.proj)
        self.assertEqual(copy_path.read_bytes(), before)
        self.assertEqual(ledger_path.read_bytes(), led_before)


class TestCheckFreshStale(CheckFreshEnv):
    def test_source_superseded_is_stale(self):
        newer = self._theirs(title="Cache TTL v2",
                             content="Cache TTL is 600 seconds now.",
                             type_="decision")
        from foldcrumbs import store as store_mod
        import importlib
        import os
        saved = dict(os.environ)
        os.environ["FOLDCRUMBS_DIR"] = str(self.their_dir)
        from foldcrumbs import config as _c
        importlib.reload(_c)
        try:
            self.assertTrue(store_mod.supersede(self.src.filename(),
                                                newer.filename()))
        finally:
            os.environ.clear()
            os.environ.update(saved)
            importlib.reload(_c)
        r = self._statuses()["source_dead"]
        self.assertEqual(r["memory_id"], self.copy_id)
        self.assertIn("superseded", r["detail"])

    def test_source_edited_after_adoption_is_changed(self):
        # rewrite the source with a newer updated_at (maintenance edit)
        p = self.their_dir / self.src.filename()
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        rec.content = "Cache TTL is 450 seconds (corrected)."
        rec.updated_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        rec.source_path = rec.filename()
        p.write_text(rec.to_markdown(), encoding="utf-8")
        r = self._statuses()["source_changed"]
        self.assertEqual(r["memory_id"], self.copy_id)

    def test_source_deleted_is_gone(self):
        (self.their_dir / self.src.filename()).unlink()
        r = self._statuses()["source_gone"]
        self.assertEqual(r["memory_id"], self.copy_id)
        self.assertIn("no longer", r["detail"])

    def test_expired_source_is_dead(self):
        p = self.their_dir / self.src.filename()
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        rec.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        rec.source_path = rec.filename()
        p.write_text(rec.to_markdown(), encoding="utf-8")
        r = self._statuses()["source_dead"]
        self.assertIn("expired", r["detail"])


class TestCheckFreshRootGone(CheckFreshEnv):
    def test_unregistered_root_is_unreachable(self):
        from foldcrumbs import federation
        federation.unregister(self.theirs.id)
        r = self._statuses()["source_unreachable"]
        self.assertEqual(r["memory_id"], self.copy_id)
        self.assertIn("root", r["detail"].lower())

    def test_missing_directory_is_unreachable(self):
        import shutil
        shutil.rmtree(self.their_dir)
        r = self._statuses()["source_unreachable"]
        self.assertIn("unavailable", r["detail"])


class TestCheckFreshLedger(CheckFreshEnv):
    def test_empty_ledger_is_empty_list(self):
        self._ledger_path().write_text("{}", encoding="utf-8")
        self.assertEqual(adopt_mod.check_fresh(cwd=self.proj), [])

    def test_corrupt_ledger_raises(self):
        # fail-closed posture of read_ledger is inherited, not re-implemented
        self._ledger_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(adopt_mod.AdoptError):
            adopt_mod.check_fresh(cwd=self.proj)

    def test_local_copy_retired_still_reports_source_state(self):
        # the local copy's own status is OUR business (outcome/supersede);
        # check_fresh reports the SOURCE state regardless — but marks that
        # the local copy is no longer active, so a stale source under a
        # retired copy is not actionable noise
        import os
        import importlib
        from foldcrumbs import config as _c
        from foldcrumbs import store as store_mod
        saved = dict(os.environ)
        os.environ["FOLDCRUMBS_DIR"] = str(self.my_dir)
        importlib.reload(_c)
        try:
            store_mod.set_status(self.copy_name, "archived")
        finally:
            os.environ.clear()
            os.environ.update(saved)
            importlib.reload(_c)
        rows = adopt_mod.check_fresh(cwd=self.proj)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "fresh")
        self.assertTrue(rows[0]["local_retired"])


class TestCheckFreshCliMcp(unittest.TestCase):
    """CLI flag + MCP arg wiring."""

    def test_cli_flag_registered(self):
        from foldcrumbs import cli as cli_mod
        p = cli_mod.build_parser()
        args = p.parse_args(["adopt", "--check-fresh"])
        self.assertTrue(args.check_fresh)

    def test_mcp_schema_declares_check_fresh_and_limit(self):
        from foldcrumbs import mcp_server
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "adopt")
        props = tool["inputSchema"]["properties"]
        self.assertIn("check_fresh", props)
        # FL-3 P1 backlog: limit was undocumented in the catalog
        self.assertIn("limit", props)


if __name__ == "__main__":
    unittest.main()
