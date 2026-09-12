"""adopt --check-fresh: stale-adoption detection (FL backlog, paper
2609.03340 "Fresh Memory, Stale Plans").

The ledger already attests root_id + memory_id + filename + adopted_at
for every adoption. check_fresh answers the question the ledger cannot:
is the SOURCE still alive and unchanged since we copied it? Read-only —
it never touches the local copy, never writes the ledger, never syncs.
Freshness is information, not automation (fleet-learning design: no
automatic sync, ever).
"""

import json
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


class TestRtRound1P0(CheckFreshEnv):
    """RT GPT round 1 on 22cc6ba (card t_09bb77e5): F1-F3 probe-reproduced,
    plus P1 F4 (legacy updated_at invented by the parser)."""

    def test_f1_cli_check_fresh_writes_nothing(self):
        # F1: cli.main ran federation.ensure_registered() BEFORE the
        # report — recreating a missing registry shard. "never writes"
        # must hold for the whole CLI command, not just the function.
        from foldcrumbs import cli as cli_mod, federation
        reg_dir = federation.roots_dir()
        shard_files = sorted(reg_dir.glob("*.json")) \
            if reg_dir.is_dir() else []
        for shard in shard_files:
            shard.unlink()
        before = {p.name: p.read_bytes()
                  for p in reg_dir.rglob("*") if p.is_file()} \
            if reg_dir.is_dir() else {}
        copy_path = self.my_dir / self.copy_name
        copy_before = copy_path.read_bytes()
        led_before = self._ledger_path().read_bytes()
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_mod.main(["adopt", "--check-fresh"])
        self.assertEqual(rc, 0)
        after = {p.name: p.read_bytes()
                 for p in reg_dir.rglob("*") if p.is_file()} \
            if reg_dir.is_dir() else {}
        self.assertEqual(before, after,
                         f"CLI check-fresh wrote to the registry: "
                         f"{set(after) - set(before)}")
        self.assertEqual(copy_path.read_bytes(), copy_before)
        self.assertEqual(self._ledger_path().read_bytes(), led_before)

    def test_f2_ambiguous_source_id_is_uncertain(self):
        # F2: two records in the source root sharing the adopted id —
        # the verdict must NOT depend on filename ordering (first-match).
        dup = MemoryRecord.from_markdown(
            (self.their_dir / self.src.filename()).read_text(encoding="utf-8"))
        dup.status = "superseded"
        dup.source_path = "zzz-duplicate.md"
        (self.their_dir / "zzz-duplicate.md").write_text(
            dup.to_markdown(), encoding="utf-8")
        r1 = adopt_mod.check_fresh(cwd=self.proj)[0]
        # rename the duplicate: same ids/states/contents, different name
        (self.their_dir / "zzz-duplicate.md").rename(
            self.their_dir / "aaa-duplicate.md")
        dup.source_path = "aaa-duplicate.md"
        r2 = adopt_mod.check_fresh(cwd=self.proj)[0]
        self.assertEqual(r1["status"], r2["status"],
                         f"verdict flipped on filename order: "
                         f"{r1['status']} -> {r2['status']}")
        self.assertEqual(r1["status"], "source_unreachable")
        self.assertIn("ambiguous", r1["detail"])

    def test_f3_invalid_adopted_at_refuses_not_fresh(self):
        # F3: an unusable attested date can't support "fresh" — visible
        # refusal, never a silent default.
        led = self._ledger_path()
        data = json.loads(led.read_text(encoding="utf-8"))
        data[self.copy_id]["adopted_at"] = "not-a-date"
        led.write_text(json.dumps(data), encoding="utf-8")
        # bump the source so staleness is real and visible
        p = self.their_dir / self.src.filename()
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        rec.updated_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        rec.source_path = rec.filename()
        p.write_text(rec.to_markdown(), encoding="utf-8")
        with self.assertRaises(adopt_mod.AdoptError) as ctx:
            adopt_mod.check_fresh(cwd=self.proj)
        self.assertIn("adopted_at", str(ctx.exception))

    def test_f3_naive_adopted_at_normalized_utc(self):
        # naive timestamps: normalized to UTC (the schema convention),
        # never a TypeError traceback
        led = self._ledger_path()
        data = json.loads(led.read_text(encoding="utf-8"))
        data[self.copy_id]["adopted_at"] = "2026-01-01T00:00:00"
        led.write_text(json.dumps(data), encoding="utf-8")
        rows = adopt_mod.check_fresh(cwd=self.proj)  # must not raise
        self.assertEqual(rows[0]["status"], "source_changed")

    def test_f4_missing_updated_at_is_not_changed(self):
        # F4 (P1, fixed): a legacy source without updated_at gets one
        # INVENTED by the parser — that is not evidence of an edit.
        p = self.their_dir / self.src.filename()
        text = p.read_text(encoding="utf-8")
        stripped = "\n".join(
            ln for ln in text.splitlines()
            if not ln.startswith("updated_at:"))
        p.write_text(stripped, encoding="utf-8")
        r = adopt_mod.check_fresh(cwd=self.proj)[0]
        self.assertNotEqual(r["status"], "source_changed",
                            "invented timestamp read as an edit")
        self.assertIn("timestamp", r["detail"].lower())


if __name__ == "__main__":
    unittest.main()
