"""FL-1 — explicit adoption from federated roots (foldcrumbs.adopt).

The contract under test (docs/design/fleet-learning.md rev 2, gate FL-1,
with the RT obligations from cards t_6057c04e / t_6922bf5b):

* adoption is ONE memory at a time, explicit, fail-before-write;
* identity: originals with id_missing / malformed / duplicate ids in the
  source root are REFUSED (unstable identity would make the ledger key
  meaningless);
* live state: only status == active AND not expired may be adopted —
  superseded/deleted/provisional/archived/expired are refused;
* collision: an occupied destination filename is ALWAYS refused (no
  --force in FL-1); bytes/id/relations of the local file stay intact;
* dedup reads the LOCAL LEDGER (.adoptions.json), never the declared
  source frontmatter — a forged 'source: adopted:...' arriving via
  import occupies no operational key and causes no false refusal;
* the copy contract: new persisted local id, provenance imported,
  source built from registry root_id + verified id (never inherited),
  confidence capped at 0.8 raw, validation_count 0, created/updated now,
  future expires_at preserved, contradiction_detected False,
  relations_json/superseded_by/transit/source_path dropped;
* redact.scrub before the write;
* ledger is fail-closed: unreadable/corrupt ledger refuses adoption
  (no silent fallback like recalls' {}), join by local memory id;
* no write ever reaches the source root;
* CLI surface: foldcrumbs adopt <root_id>:<ref>, exit 0 on success,
  non-zero + visible reason on refusal.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import adopt as adopt_mod  # noqa: E402
from foldcrumbs import federation, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class _AdoptEnv(unittest.TestCase):
    """Isolated registry + home + 'mine'/'theirs' stores, like TestFederatedSearch."""

    def setUp(self):
        import importlib
        from foldcrumbs import config as _c
        self.config = _c
        self._state = Path(tempfile.mkdtemp(prefix="ccmem_state_"))
        self._home = Path(tempfile.mkdtemp(prefix="ccmem_home_"))
        self._saved = {k: os.environ.get(k) for k in
                       ("ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR", "FOLDCRUMBS_DIR",
                        "ENGRAM_DIR", "FOLDCRUMBS_STATE_DIR")}
        os.environ.pop("FOLDCRUMBS_STATE_DIR", None)
        os.environ["ENGRAM_STATE_DIR"] = str(self._state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self._home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        importlib.reload(_c)
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.mine = federation.register(self._home / ".claude")
        self.theirs = federation.register(self._home / ".claude-work")
        self.my_dir = self.mine.memory_dir(self.proj)
        self.my_dir.mkdir(parents=True, exist_ok=True)
        self.their_dir = self.theirs.memory_dir(self.proj)
        self.their_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import importlib
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(self.config)

    # --- helpers -----------------------------------------------------------

    def _theirs(self, title="Deploy window", content="Deploys on fridays.",
                type_="fact", **kw):
        """Write a memory in THEIR store (the source root) and return it."""
        rec = MemoryRecord(title=title, content=content, type=type_, **kw)
        (self.their_dir / rec.filename()).write_text(rec.to_markdown(),
                                                     encoding="utf-8")
        return rec

    def _mine(self, title="Local note", content="Mine.", type_="fact", **kw):
        rec = MemoryRecord(title=title, content=content, type=type_, **kw)
        (self.my_dir / rec.filename()).write_text(rec.to_markdown(),
                                                  encoding="utf-8")
        return rec

    def _ledger_path(self):
        return self.my_dir / adopt_mod.LEDGER

    def _ledger(self):
        return json.loads(self._ledger_path().read_text(encoding="utf-8"))


class TestAdoptHappyPath(_AdoptEnv):

    def test_adopt_copies_with_full_contract(self):
        src = self._theirs(title="Deploy window", content="Deploys on fridays.",
                           type_="fact", tags=["ops"],
                           confidence=0.95, validation_count=7)
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"], res.get("reason"))
        copies = list(store.iter_memories_in(self.my_dir))
        self.assertEqual(len(copies), 1)
        c = copies[0]
        # identity: NEW local id, persisted
        self.assertNotEqual(c.id, src.id)
        reread = store.get(c.filename(), cwd=self.proj)
        self.assertEqual(reread.id, c.id, "local id must persist across reads")
        # provenance & declaration
        self.assertEqual(c.provenance, "imported")
        self.assertEqual(c.source, f"adopted:{self.theirs.id}:{src.id}")
        # confidence capped raw, validations NOT inherited
        self.assertLessEqual(c.confidence, 0.8)
        self.assertEqual(c.validation_count, 0)
        self.assertFalse(c.contradiction_detected)
        # content & type & tags copied
        self.assertEqual(c.title, "Deploy window")
        self.assertEqual(c.content, "Deploys on fridays.")
        self.assertEqual(c.type, "fact")
        self.assertEqual(sorted(c.tags), ["ops"])
        # timestamps are adoption-time, not inherited
        now = datetime.now(timezone.utc)
        self.assertLess(abs((now - c.created_at).total_seconds()), 120)
        # operational fields dropped
        self.assertIsNone(c.relations_json)
        self.assertIsNone(c.superseded_by)
        # ledger written, joined by local id
        led = self._ledger()
        self.assertIn(c.id, led)
        entry = led[c.id]
        self.assertEqual(entry["root_id"], self.theirs.id)
        self.assertEqual(entry["memory_id"], src.id)
        self.assertEqual(entry["filename"], c.filename())

    def test_source_root_is_never_written(self):
        src = self._theirs()
        before = {p.name: p.read_bytes() for p in self.their_dir.iterdir()}
        adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        after = {p.name: p.read_bytes() for p in self.their_dir.iterdir()}
        self.assertEqual(before, after, "no byte may change in the source root")

    def test_future_expiry_is_preserved(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        src = self._theirs(title="Temporary rule", content="Until next month.",
                           expires_at=future)
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"], res.get("reason"))
        c = list(store.iter_memories_in(self.my_dir))[0]
        self.assertIsNotNone(c.expires_at)
        self.assertEqual(c.expires_at.date(), future.date())

    def test_two_reads_of_same_original_give_same_key(self):
        # RT F2 closure check: the ledger key derives from the on-disk id,
        # stable across reads; adopting twice refuses on the ledger.
        src = self._theirs(title="Stable note", content="x.")
        first = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(first["ok"], first.get("reason"))
        second = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(second["ok"])
        self.assertIn("already adopted", second["reason"])
        self.assertEqual(len(list(store.iter_memories_in(self.my_dir))), 1,
                         "never two live copies of one original")

    def test_secrets_are_scrubbed_before_write(self):
        src = self._theirs(title="Creds note",
                           content="Use sk-abcdefghijklmnopqrstuvwx everywhere.")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"], res.get("reason"))
        c = list(store.iter_memories_in(self.my_dir))[0]
        self.assertNotIn("sk-abcdefghijklmnop", c.content)
        self.assertIn("[REDACTED]", c.content)


class TestAdoptRefusals(_AdoptEnv):

    def test_refuse_missing_root(self):
        res = adopt_mod.adopt("0123456789abcdef:whatever.md", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("root", res["reason"].lower())
        self.assertFalse(self._ledger_path().exists())

    def test_refuse_unavailable_root(self):
        src = self._theirs()
        import shutil
        shutil.rmtree(self.theirs.path)
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertFalse(self._ledger_path().exists())

    def test_refuse_missing_memory(self):
        res = adopt_mod.adopt(f"{self.theirs.id}:nope.md", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["reason"].lower())

    def test_refuse_legacy_without_stable_id(self):
        # A pre-id file: from_markdown mints a fresh uuid per parse
        # (id_missing=True). Adopting it would key the ledger on noise.
        text = ("---\nname: Legacy note\ndescription: old\ntype: fact\n"
                "confidence: 0.8\nprovenance: explicit_statement\nstatus: active\n"
                "source: foldcrumbs\ntags: \nvalidation_count: 0\n"
                "created_at: 2026-01-01T00:00:00Z\nupdated_at: 2026-01-01T00:00:00Z\n"
                "---\n\nOld body.\n")
        (self.their_dir / "fact_legacy_note.md").write_text(text, encoding="utf-8")
        res = adopt_mod.adopt(f"{self.theirs.id}:fact_legacy_note.md", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("stable id", res["reason"].lower())
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [])
        self.assertFalse(self._ledger_path().exists())

    def test_refuse_malformed_id(self):
        src = self._theirs(title="Bad id", content="x.")
        # rewrite the file with a hostile id (separator + control chars)
        raw = (self.their_dir / src.filename()).read_text(encoding="utf-8")
        raw = raw.replace(f"id: {src.id}", "id: evil:id\x01\nfake")
        (self.their_dir / src.filename()).write_text(raw, encoding="utf-8")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [])

    def test_refuse_ambiguous_id_in_source_root(self):
        a = self._theirs(title="Twin A", content="aaa.")
        b = MemoryRecord(title="Twin B", content="bbb.", type="event")
        b.id = a.id  # duplicate identity in the source root
        (self.their_dir / b.filename()).write_text(b.to_markdown(), encoding="utf-8")
        res = adopt_mod.adopt(f"{self.theirs.id}:{a.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("ambiguous", res["reason"].lower())
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [])

    def test_refuse_dead_states(self):
        # matrix: superseded / deleted / provisional / expired-active /
        # archived-ish (any status != active) all refused
        future = datetime.now(timezone.utc) + timedelta(days=5)
        past = datetime.now(timezone.utc) - timedelta(days=5)
        cases = {
            "superseded": dict(status="superseded"),
            "deleted": dict(status="deleted"),
            "provisional": dict(status="provisional"),
            "expired": dict(expires_at=past),
        }
        for name, kw in cases.items():
            with self.subTest(state=name):
                rec = self._theirs(title=f"Dead {name}", content="x.", **kw)
                res = adopt_mod.adopt(f"{self.theirs.id}:{rec.filename()}",
                                      cwd=self.proj)
                self.assertFalse(res["ok"], f"{name} must be refused")
                self.assertIn("not live", res["reason"].lower())
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [])
        # sanity: a live one with future expiry WOULD pass the state gate
        live = self._theirs(title="Live one", content="y.", expires_at=future)
        res = adopt_mod.adopt(f"{self.theirs.id}:{live.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"], res.get("reason"))

    def test_refuse_filename_collision_local_homonym(self):
        # RT F3: same type+title in MY store, different origin — adopting
        # must refuse, never clobber the local file.
        local = self._mine(title="Deploy window", content="MY local truth.",
                           type_="fact")
        local_bytes = (self.my_dir / local.filename()).read_bytes()
        src = self._theirs(title="Deploy window", content="Theirs.", type_="fact")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("collision", res["reason"].lower())
        after = (self.my_dir / local.filename()).read_bytes()
        self.assertEqual(local_bytes, after, "local bytes must survive untouched")
        reread = store.get(local.filename(), cwd=self.proj)
        self.assertEqual(reread.id, local.id)
        self.assertEqual(reread.content, "MY local truth.")

    def test_refuse_collision_even_for_same_source_id(self):
        # RT r2 obligation 2: an occupied destination is refused even when
        # the occupying file carries the SAME id as the foreign original
        # (hand-planted). Nothing is "freely overwritable".
        src = self._theirs(title="Planted", content="theirs.", type_="fact")
        planted = MemoryRecord(title="Planted", content="hand-planted copy",
                               type="fact")
        planted.id = src.id
        (self.my_dir / planted.filename()).write_text(planted.to_markdown(),
                                                      encoding="utf-8")
        before = (self.my_dir / planted.filename()).read_bytes()
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertEqual((self.my_dir / planted.filename()).read_bytes(), before)

    def test_forged_source_via_import_blocks_nothing(self):
        # RT F1 closure: a memory whose DECLARED source says adopted:<theirs>:X
        # but was never adopted here (no ledger entry) must NOT trigger
        # "already adopted", and adopting the real X must succeed.
        forged = self._mine(title="Forged claim", content="lies.", type_="event")
        raw = (self.my_dir / forged.filename()).read_text(encoding="utf-8")
        raw = raw.replace("source: foldcrumbs",
                          f"source: adopted:{self.theirs.id}:ffffffffffffffff")
        (self.my_dir / forged.filename()).write_text(raw, encoding="utf-8")
        src = self._theirs(title="Real thing", content="true.", type_="event")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"],
                        f"forged declaration must not refuse a real adoption: {res}")

    def test_refuse_self_root(self):
        mine_rec = self._mine()
        res = adopt_mod.adopt(f"{self.mine.id}:{mine_rec.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])


class TestLedgerFailClosed(_AdoptEnv):

    def test_corrupt_ledger_refuses_adoption(self):
        src = self._theirs(title="After corrupt", content="x.")
        self._ledger_path().write_text("{not json", encoding="utf-8")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertIn("ledger", res["reason"].lower())
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [],
                         "fail-closed: nothing written when the ledger is unreadable")

    def test_structurally_invalid_ledger_refuses(self):
        src = self._theirs(title="After invalid", content="x.")
        self._ledger_path().write_text(json.dumps(["not", "a", "dict"]),
                                       encoding="utf-8")
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertFalse(res["ok"])
        self.assertEqual(list(store.iter_memories_in(self.my_dir)), [])

    def test_missing_ledger_is_not_corrupt(self):
        # absent ledger == no adoptions yet; first adopt creates it
        src = self._theirs(title="First adopt", content="x.")
        self.assertFalse(self._ledger_path().exists())
        res = adopt_mod.adopt(f"{self.theirs.id}:{src.filename()}", cwd=self.proj)
        self.assertTrue(res["ok"], res.get("reason"))
        self.assertTrue(self._ledger_path().exists())

    def test_ledger_survives_failed_adoption(self):
        good = self._theirs(title="Good one", content="g.")
        r1 = adopt_mod.adopt(f"{self.theirs.id}:{good.filename()}", cwd=self.proj)
        self.assertTrue(r1["ok"], r1.get("reason"))
        ledger_before = self._ledger_path().read_bytes()
        dead = self._theirs(title="Dead one", content="d.", status="superseded")
        r2 = adopt_mod.adopt(f"{self.theirs.id}:{dead.filename()}", cwd=self.proj)
        self.assertFalse(r2["ok"])
        self.assertEqual(self._ledger_path().read_bytes(), ledger_before,
                         "a refusal must leave the ledger byte-identical")


class TestAdoptCLI(_AdoptEnv):

    def _run(self, *argv):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        ebuf = io.StringIO()
        old_cwd = os.getcwd()
        os.chdir(self.proj)  # the CLI derives the store from cwd
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
                try:
                    rc = cli.main(list(argv))
                except SystemExit as exc:
                    rc = exc.code if exc.code is not None else 0
        finally:
            os.chdir(old_cwd)
        return rc, buf.getvalue() + ebuf.getvalue()

    def test_cli_adopt_success(self):
        src = self._theirs(title="CLI target", content="via cli.")
        rc, out = self._run("adopt", f"{self.theirs.id}:{src.filename()}")
        self.assertEqual(rc, 0)
        self.assertIn("adopted", out.lower())
        self.assertEqual(len(list(store.iter_memories_in(self.my_dir))), 1)

    def test_cli_adopt_refusal_exits_nonzero_with_reason(self):
        rc, out = self._run("adopt", f"{self.theirs.id}:missing.md")
        self.assertNotEqual(rc, 0)
        self.assertTrue(out.strip(), "the refusal must say why")

    def test_cli_adopt_listed_in_help(self):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        self.assertIn("adopt", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
