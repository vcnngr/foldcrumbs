"""Regression tests for foldcrumbs (stdlib unittest, no external deps).

Run: python3 -m unittest discover -s tests
"""

import contextlib
import errno
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Sandbox every store-locating variable before foldcrumbs is imported. Shared
# with the other test modules so a standalone run of any of them is covered
# too — see tests/_sandbox.py for why clearing them would not be enough.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import SANDBOX, is_inside  # noqa: E402

from foldcrumbs import distill, install, redact, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class TestSuiteIsolation(unittest.TestCase):
    """The suite must not be able to touch a real store, however it is run."""

    def test_the_suite_never_resolves_to_a_real_store(self):
        import importlib
        from foldcrumbs import config
        importlib.reload(config)
        for path in (config.STATE_DIR, config.memory_dir(), config.claude_config_dir()):
            self.assertTrue(is_inside(path),
                            f"{path} is outside the suite sandbox {SANDBOX}")

    def test_the_real_state_dir_is_never_written(self):
        # The concrete failure this guards: a class isolating only the legacy
        # names let install.configure_backend() overwrite the developer's real
        # llm-backend choice and stage a runtime snapshot beside it.
        real = Path.home() / ".foldcrumbs"
        before = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        install.configure_backend("codex", bin_path="/nowhere/codex")
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        self.assertEqual(before, after)


class TmpStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccmem_test_")
        self._state = tempfile.mkdtemp(prefix="ccmem_test_state_")
        # FOLDCRUMBS_* take precedence over the legacy ENGRAM_* names, so
        # setting only the latter leaves a developer who exported either one
        # pointed at their real store.
        self._saved = {k: os.environ.get(k) for k in
                       ("ENGRAM_DIR", "ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR",
                        "FOLDCRUMBS_DIR", "FOLDCRUMBS_STATE_DIR")}
        for k in ("FOLDCRUMBS_DIR", "FOLDCRUMBS_STATE_DIR"):
            os.environ.pop(k, None)
        os.environ["ENGRAM_DIR"] = self.dir
        # Isolate the federation registry too. Without this, search() —
        # federated by default — would consult the developer's real
        # ~/.foldcrumbs and read their actual stores during a test run.
        os.environ["ENGRAM_STATE_DIR"] = self._state
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self._state) / "config")
        import importlib
        from foldcrumbs import config as _c
        importlib.reload(_c)
        # Scratch dirs belong inside this sandbox. Several tests used
        # `self._state.parent`, which is the *system* temp dir: the paths were
        # then shared across tests and runs, and a leftover from an earlier one
        # decided the outcome of a later assertion.
        # Fail loudly rather than quietly touching real data.
        assert str(Path.home()) not in str(_c.STATE_DIR), "test escaped its sandbox"
        assert str(Path.home()) not in str(_c.memory_dir()), "test escaped its sandbox"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        from foldcrumbs import config as _c
        importlib.reload(_c)


class TestSchema(unittest.TestCase):
    def test_roundtrip(self):
        r = MemoryRecord(title="T", content="Body text here.", type="decision",
                         confidence=0.9, tags=["a", "b"])
        back = MemoryRecord.from_markdown(r.to_markdown())
        self.assertEqual(back.title, "T")
        self.assertEqual(back.type, "decision")
        self.assertEqual(back.confidence, 0.9)
        self.assertEqual(back.tags, ["a", "b"])

    def test_invalid_type_falls_back(self):
        self.assertEqual(MemoryRecord(title="x", content="y", type="bogus").type, "fact")

    def test_legacy_type_preserved(self):
        self.assertEqual(MemoryRecord(title="x", content="y", type="project").type,
                         "project")

    def test_supersede_zeroes_confidence(self):
        r = MemoryRecord(title="x", content="y", type="fact", confidence=0.9)
        r.mark_superseded("other-id")
        self.assertEqual(r.compute_confidence(), 0.0)


class TestRecordFieldOrder(unittest.TestCase):
    """The constructor signature is a released API: append, never insert."""

    # Exactly what v0.6.0 shipped, in order. A caller may pass these
    # positionally, so slipping a new field in between binds their arguments
    # to the wrong ones — silently, because the types happen to be compatible.
    RELEASED = [
        "title", "content", "type", "description", "id", "confidence",
        "provenance", "status", "tags", "source", "superseded_by",
        "validation_count", "contradiction_detected", "created_at",
        "updated_at", "source_path", "created_at_missing", "origin_root",
        "origin_root_id", "origin_path", "supersedes_external",
    ]

    def test_released_fields_keep_their_positions(self):
        import dataclasses
        names = [f.name for f in dataclasses.fields(MemoryRecord)]
        self.assertEqual(names[:len(self.RELEASED)], self.RELEASED,
                         "a field was inserted among the released ones")

    def test_a_positional_call_still_binds_the_same_fields(self):
        from datetime import datetime, timezone
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rec = MemoryRecord("T", "body", "fact", "d", "id-1", 0.9,
                           "inferred", "active", ["a"], "src", None, 2,
                           False, when, when, "f.md", False, "claude",
                           "0123456789abcdef", "/abs/f.md",
                           ["0123456789abcdef:x.md"])
        self.assertEqual(rec.supersedes_external, ["0123456789abcdef:x.md"])
        self.assertEqual(rec.origin_root, "claude")
        self.assertIsNone(rec.contested_by)      # appended, so still default
        self.assertFalse(rec.id_missing)         # appended after it
        self.assertFalse(rec.updated_at_missing)  # and after that

    def test_claims_survive_a_round_trip(self):
        rec = MemoryRecord(title="T", content="body",
                           supersedes_external=["0123456789abcdef:x.md"])
        back = MemoryRecord.from_markdown(rec.to_markdown())
        self.assertEqual(back.supersedes_external, ["0123456789abcdef:x.md"])


class TestStore(TmpStore):
    def test_dedup_validates(self):
        a = MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                         type="decision", confidence=0.9)
        self.assertEqual(store.upsert(a)[0], "created")
        b = MemoryRecord(title="Use stdlib only",
                         content="Hooks use only stdlib here now.",
                         type="decision", confidence=0.9)
        self.assertEqual(store.upsert(b)[0], "validated")
        self.assertEqual(len([m for m in store.load_all()]), 1)

    def test_index_grouped(self):
        store.upsert(MemoryRecord(title="R", content="rule", type="instruction"))
        store.upsert(MemoryRecord(title="F", content="fact", type="fact"))
        idx = store.rebuild_index().read_text()
        self.assertIn("## Rules", idx)
        self.assertIn("## Facts", idx)

    def test_index_links_to_real_file_not_derived_name(self):
        # A file imported under a non-canonical name (e.g. by another tool) must
        # still get a resolvable index link pointing at the real file on disk.
        weird = Path(self.dir) / "voice-clone.md"
        weird.write_text(
            "---\nname: Voice Clone App\ndescription: hook\ntype: project\n---\n\nbody\n",
            encoding="utf-8",
        )
        idx = store.rebuild_index().read_text()
        self.assertIn("(voice-clone.md)", idx)
        # And every link in the index resolves to an existing file.
        import re as _re
        for target in _re.findall(r"\]\(([^)]+\.md)\)", idx):
            self.assertTrue((Path(self.dir) / target).exists(), target)

    def test_degenerate_titles_get_distinct_files(self):
        a = MemoryRecord(title="", content="one", type="fact")
        b = MemoryRecord(title="", content="two", type="fact")
        self.assertEqual(a.title, "Untitled")
        self.assertNotEqual(a.filename(), b.filename())


class TestRecallReinforcement(TmpStore):
    """A memory that keeps being needed should separate itself from its peers."""

    def _put(self, title, body):
        rec = MemoryRecord(title=title, content=body, type="fact")
        store.write_memory(rec)
        return rec

    def test_a_recall_records_what_it_returned(self):
        from foldcrumbs import recalls
        rec = self._put("Lockfile", "The lockfile is committed.")
        self.assertEqual(recalls.counts(), {})
        hits = store.search("lockfile", federated=False)
        self.assertEqual([m.title for m in hits], ["Lockfile"])
        self.assertEqual(recalls.counts().get(rec.id), 1)
        store.search("lockfile", federated=False)
        self.assertEqual(recalls.counts().get(rec.id), 2)

    def test_only_what_was_returned_is_reinforced(self):
        # Counting candidates instead of results would reinforce the whole
        # store on every search, which is the same as reinforcing nothing.
        from foldcrumbs import recalls
        wanted = self._put("Lockfile", "The lockfile is committed.")
        other = self._put("Timezone", "All timestamps are UTC.")
        store.search("lockfile", federated=False)
        counted = recalls.counts()
        self.assertEqual(counted.get(wanted.id), 1)
        self.assertIsNone(counted.get(other.id),
                          "a memory nobody asked for was reinforced")

    def test_use_separates_equals_without_outranking_relevance(self):
        from foldcrumbs import recalls
        # Two memories the query matches equally well.
        used = self._put("Deploy notes A", "Deploy runs on Tuesday.")
        unused = self._put("Deploy notes B", "Deploy runs on Tuesday.")
        recalls.reinforce([used.id] * 10)
        hits = store.search("deploy runs", federated=False)
        self.assertEqual([m.filename() for m in hits],
                         [used.filename(), unused.filename()],
                         "the well-used memory did not come first")
        # And a strong match still beats a heavily-used weak one.
        exact = self._put("Rollback", "Rollback is one command: make undo.")
        recalls.reinforce([used.id] * 50)
        hits = store.search("rollback is one command", federated=False)
        self.assertEqual(hits[0].filename(), exact.filename(),
                         "use outranked a better match")

    def test_a_better_match_always_wins_however_used_the_other_is(self):
        # Folding the bonus into the score let a near match times its bonus
        # overtake an exact one — 0.9613 x 1.1 beats 1.0 — so a memory could
        # be outranked by a worse answer that simply got asked for more.
        from foldcrumbs import recalls
        # 1.0000 against 0.9655 — close enough that a 10% bonus overtook it,
        # far enough that one genuinely answers the question better.
        exact = self._put("Deploy exact", "deploy runs tuesday at nine")
        near = self._put("Deploy near", "deploy always runs on a tuesday morning")
        recalls.reinforce([near.id] * 100)
        hits = store.search("deploy runs tuesday", federated=False)
        self.assertEqual(len(hits), 2, "the fixture stopped matching both")
        self.assertEqual(hits[0].filename(), exact.filename(),
                         "a heavily-used near match outranked the exact one")

    def test_an_arbitrary_tiebreak_does_not_compound(self):
        # Equally-relevant memories are cut by the limit on filename order.
        # Reinforcing only the winner turned that coin flip into a permanent
        # lead — exposure compounding into rank, reflecting nothing but having
        # sorted first.
        from foldcrumbs import recalls
        a = self._put("Deploy A", "Deploy runs on Tuesday.")
        b = self._put("Deploy B", "Deploy runs on Tuesday.")
        for _ in range(5):
            store.search("deploy runs on tuesday", limit=1, federated=False)
        counted = recalls.counts()
        self.assertEqual(counted.get(a.filename()), counted.get(b.filename()),
                         f"the tiebreak compounded: {counted}")

    def test_a_memory_that_leaves_any_way_loses_its_count(self):
        # forget is not the only exit: supersede, the contradiction pass and
        # prune all retire a memory. Cleaning up at each call site is a rule
        # to remember four times and forget the fifth.
        from foldcrumbs import recalls
        old = self._put("Deadline", "Ship on Friday.")
        store.search("deadline ship", federated=False)
        self.assertIn(old.id, recalls.counts())
        new = self._put("Deadline moved", "Ship on Monday instead.")
        self.assertTrue(store.supersede(old.filename(), new.filename()))
        store.search("deadline ship", federated=False)
        self.assertNotIn(old.id, recalls.counts(),
                         "a superseded memory kept its count")

    def test_a_store_that_could_not_be_read_keeps_its_counts(self):
        # A caller that cannot vouch for its listing passes None. An unreadable
        # directory yields the same empty list as an empty store, and treating
        # the two alike would erase every count ever earned.
        from foldcrumbs import recalls
        rec = self._put("Lockfile", "The lockfile is committed.")
        store.search("lockfile", federated=False)
        self.assertIn(rec.id, recalls.counts())
        recalls.reinforce([], cwd=None, known=None)
        self.assertIn(rec.id, recalls.counts(),
                      "an unvouched listing erased the counts")

    def test_a_file_that_will_not_open_does_not_cost_the_others_their_counts(self):
        # End-to-end, through search() itself. The directory exists, so a
        # guard that only checked is_dir() called the listing complete — while
        # the memory that would not open was missing from it, and every count
        # belonging to a memory unreadable at that instant was erased.
        from foldcrumbs import recalls
        kept = self._put("Lockfile", "The lockfile is committed.")
        other = self._put("Timezone", "All timestamps are UTC.")
        store.search("lockfile", federated=False)
        store.search("timestamps utc", federated=False)
        before = recalls.counts()
        self.assertEqual(sorted(before), sorted([kept.id, other.id]))

        unreadable = Path(self.dir) / kept.filename()
        real_read = Path.read_text

        def refuses(self_path, *args, **kw):
            if Path(self_path) == unreadable:
                raise OSError(errno.EACCES, "permission denied")
            return real_read(self_path, *args, **kw)

        Path.read_text = refuses
        try:
            hits = store.search("timestamps utc", federated=False)
        finally:
            Path.read_text = real_read
        self.assertEqual([m.title for m in hits], ["Timezone"],
                         "the readable memory stopped being found")
        self.assertIn(kept.id, recalls.counts(),
                      "a momentarily unreadable memory lost its count")

    def test_the_scan_streams_rather_than_building_a_list(self):
        # The federated scan runs this on a thread it stops waiting for and
        # keeps whatever arrived before the deadline. Reading the whole
        # directory before yielding anything turns every timeout into an empty
        # result — a slow store stops contributing instead of contributing
        # less.
        for i in range(6):
            self._put(f"Memory {i}", f"Body {i}.")
        seen = []
        for rec in store.iter_memories_in(Path(self.dir)):
            seen.append(rec.title)
            if len(seen) == 2:
                break          # what a deadline does
        self.assertEqual(len(seen), 2,
                         "the scan read past the point the caller stopped")

    def test_an_abandoned_scan_never_calls_itself_complete(self):
        # Completeness starts False and is earned by reaching the end, so a
        # reader that gave up cannot mistake its partial view for the store.
        for i in range(4):
            self._put(f"Memory {i}", f"Body {i}.")
        report: dict = {}
        gen = store.iter_memories_in(Path(self.dir), report=report)
        next(gen)
        self.assertFalse(report.get("complete"),
                         "a scan called itself complete before finishing")
        gen.close()
        self.assertFalse(report.get("complete"),
                         "an abandoned scan reported completeness")
        _, complete = store.scan_store(Path(self.dir))
        self.assertTrue(complete, "a scan read to the end was not complete")

    def test_a_truncated_scan_is_not_complete(self):
        for i in range(5):
            self._put(f"Memory {i}", f"Body {i}.")
        records, complete = store.scan_store(Path(self.dir), max_files=2)
        self.assertEqual(len(records), 2)
        self.assertFalse(complete, "a capped scan claimed to be the whole store")

    def test_a_file_that_parses_to_nothing_does_not_make_a_scan_incomplete(self):
        # Only *unreadable* means incomplete. A file that opened has been
        # accounted for, however little it turned out to hold — and calling
        # that incomplete would stop the store from ever reconciling counts,
        # since from_markdown is tolerant enough that such files are common.
        self._put("Lockfile", "The lockfile is committed.")
        (Path(self.dir) / "not_a_memory.md").write_text(
            "just some prose someone dropped here", encoding="utf-8")
        records, complete = store.scan_store(Path(self.dir))
        self.assertIn("Lockfile", [m.title for m in records])
        self.assertTrue(complete, "a readable file was counted as unreadable")

    def test_a_file_that_is_not_text_does_not_blind_the_store(self):
        # UnicodeDecodeError is a ValueError, not an OSError. Splitting the
        # read from the parse let it escape the handler and abort the scan, so
        # a single junk file anywhere in the memory directory stopped recall
        # from finding anything at all.
        kept = self._put("Lockfile", "The lockfile is committed.")
        (Path(self.dir) / "binary.md").write_bytes(b"\xff\xfe\x00garbage\x80")
        records, complete = store.scan_store(Path(self.dir))
        self.assertIn(kept.title, [m.title for m in records],
                      "one undecodable file hid the whole store")
        self.assertTrue(complete,
                        "a file that was read counted as unreadable, which "
                        "switches reconciliation off for good")
        hits = store.search("lockfile committed", federated=False)
        self.assertEqual([m.title for m in hits], ["Lockfile"])

    def test_a_different_memory_on_the_same_file_starts_from_nothing(self):
        # Filenames are type + title, so a different memory can land on the
        # same file: "Deadline: ship Friday" becoming "Deadline: ship Monday"
        # scores below the dedup threshold and simply overwrites it. Keyed by
        # filename the new memory inherited a rank it never earned; keyed by
        # id it cannot, and the stale entry leaves at the next reconciliation.
        from foldcrumbs import recalls
        first = self._put("Deadline", "Ship on Friday.")
        for _ in range(7):
            store.search("deadline ship friday", federated=False)
        self.assertGreater(recalls.counts().get(first.id, 0), 1)
        replacement = MemoryRecord(title="Deadline",
                                   content="Ship on Monday instead, plans changed.",
                                   type="fact")
        self.assertEqual(replacement.filename(), first.filename(),
                         "the fixture no longer collides on the filename")
        store.write_memory(replacement)
        store.search("deadline ship monday", federated=False)
        counted = recalls.counts()
        self.assertEqual(counted.get(replacement.id), 1,
                         "the new memory inherited the old one's rank")
        self.assertNotIn(first.id, counted,
                         "the replaced memory's weight was left behind")

    def test_rewriting_the_same_memory_keeps_its_count(self):
        # The other half: touching a memory must not cost it its history.
        from foldcrumbs import recalls
        rec = self._put("Lockfile", "The lockfile is committed.")
        for _ in range(3):
            store.search("lockfile committed", federated=False)
        before = recalls.counts().get(rec.id)
        rec.content = "The lockfile is committed, always."
        store.write_memory(rec)
        self.assertEqual(recalls.counts().get(rec.id), before,
                         "editing a memory reset its own history")

    def test_a_read_only_consumer_writes_nothing(self):
        # A machine sharing the store over Syncthing while another does the
        # writing. Recall still works there; writing is what is switched off,
        # and a count is no exception — otherwise every recall churns a synced
        # file and invites conflicts.
        from foldcrumbs import config as _config, recalls
        self._put("Lockfile", "The lockfile is committed.")
        marker = _config.STATE_DIR / "no-distill"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        try:
            hits = store.search("lockfile committed", federated=False)
        finally:
            marker.unlink()
        self.assertEqual([m.title for m in hits], ["Lockfile"],
                         "recall stopped working on a read-only consumer")
        self.assertFalse((Path(self.dir) / recalls.SIDECAR).exists(),
                         "a read-only consumer wrote to the shared store")

    def test_reinforcement_does_not_wait_on_a_held_lock(self):
        # This runs on the read path. The default five-second wait is right
        # for a registration and absurd for a count nobody needs: a contended
        # sidecar would have held up every recall behind it.
        from foldcrumbs import federation, recalls
        rec = self._put("Lockfile", "The lockfile is committed.")
        waits = []
        real = federation.file_lock

        def noting(path, allow_unsupported=False, wait=None):
            if path.name == ".lock-recalls":
                waits.append(wait)
            return real(path, allow_unsupported=allow_unsupported, wait=wait)

        federation.file_lock = noting
        try:
            store.search("lockfile committed", federated=False)
        finally:
            federation.file_lock = real
        self.assertTrue(waits, "the sidecar was written without a lock")
        self.assertLess(waits[0], 1.0,
                        f"a recall would wait {waits[0]}s on a busy sidecar")
        self.assertGreater(recalls.counts().get(rec.id, 0), 0)

    def test_a_lost_lock_cannot_transfer_a_rank(self):
        # Clearing the old count when a file is taken over was tried first,
        # and it is a write that can fail: one lost lock silently handed the
        # new memory the old one's rank. Keyed by id there is nothing to
        # clear — the new record simply has no history, whatever the lock did.
        from foldcrumbs import federation, recalls
        first = self._put("Deadline", "Ship on Friday.")
        for _ in range(7):
            store.search("deadline ship friday", federated=False)
        self.assertGreater(recalls.counts().get(first.id, 0), 1)

        @contextlib.contextmanager
        def never_granted(path, allow_unsupported=False, wait=None):
            yield False           # every sidecar write fails from here on

        real = federation.file_lock
        federation.file_lock = never_granted
        try:
            replacement = MemoryRecord(
                title="Deadline", content="Ship on Monday instead, plans changed.",
                type="fact")
            self.assertEqual(replacement.filename(), first.filename(),
                             "the fixture no longer collides on the filename")
            store.write_memory(replacement)
        finally:
            federation.file_lock = real
        hits = store.search("deadline ship monday", federated=False)
        self.assertEqual(hits[0].id, replacement.id)
        self.assertEqual(recalls.counts().get(replacement.id), 1,
                         "the replacement started with a rank it never earned")

    def test_a_memory_written_before_ids_is_not_counted(self):
        # Such a file gets a fresh uuid on every load, so a count would be
        # filed under a key that never comes back: it could never accumulate,
        # and each recall would add an entry and reconcile away the last —
        # churning a file that may well be synced between machines.
        from foldcrumbs import recalls
        legacy = Path(self.dir) / "fact_legacy.md"
        legacy.write_text(
            "---\nname: Legacy\ndescription: hook\ntype: fact\n---\n\n"
            "Written before ids were serialized.\n", encoding="utf-8")
        first = [m for m in store.load_all() if m.title == "Legacy"][0]
        second = [m for m in store.load_all() if m.title == "Legacy"][0]
        self.assertTrue(first.id_missing)
        self.assertNotEqual(first.id, second.id,
                            "the fixture no longer models an id-less memory")
        for _ in range(4):
            hits = store.search("legacy written before", federated=False)
            self.assertEqual([m.title for m in hits], ["Legacy"],
                             "an id-less memory stopped being recalled")
        self.assertEqual(recalls.counts(), {},
                         "an id that changes on every read was counted anyway")

    def test_an_empty_store_is_an_answer_and_clears_the_counts(self):
        # The other half: when the store really has nothing active, no count
        # belongs to anything. Refusing to act on an empty answer — or bailing
        # out because this recall had nothing to add — left the weight behind.
        from foldcrumbs import recalls
        recalls.reinforce(["fact_ghost.md"] * 4)
        self.assertEqual(recalls.counts(), {"fact_ghost.md": 4})
        self.assertEqual(store.search("anything at all", federated=False), [])
        self.assertEqual(recalls.counts(), {},
                         "a weight survived a store with nothing in it")

    def test_a_reused_filename_does_not_inherit_the_old_weight(self):
        # Filenames are type + slug, so a new memory with the same title lands
        # on the same file. Inheriting the retired memory's count would hand a
        # brand-new memory a rank it never earned.
        from foldcrumbs import recalls
        first = self._put("Deadline", "Ship on Friday.")
        for _ in range(6):
            store.search("deadline ship friday", federated=False)
        self.assertGreater(recalls.counts().get(first.id, 0), 1)
        replacement = self._put("Deadline moved", "Ship on Monday instead.")
        self.assertTrue(store.supersede(first.filename(),
                                        replacement.filename()))
        # Same title, so the same file — retired and rewritten with no recall
        # in between, which is exactly when a leftover weight is inherited.
        again = self._put("Deadline", "Ship on Wednesday now.")
        self.assertEqual(again.filename(), first.filename(),
                         "the fixture no longer reuses the filename")
        store.search("deadline ship wednesday", federated=False)
        self.assertEqual(recalls.counts().get(again.id), 1,
                         "the new memory inherited the old one's weight")

    def test_a_zero_limit_reinforces_nothing(self):
        # search() returns nothing, so nothing was used. Reading the cutoff
        # from scored[limit - 1] made that scored[-1] — the *worst* match —
        # and reinforced candidates no caller ever saw.
        from foldcrumbs import recalls
        self._put("Lockfile", "The lockfile is committed.")
        self.assertEqual(store.search("lockfile", limit=0, federated=False), [])
        self.assertEqual(recalls.counts(), {},
                         "a recall that returned nothing reinforced something")

    def test_the_count_never_drags_in_what_did_not_match(self):
        from foldcrumbs import recalls
        rec = self._put("Unrelated", "Nothing to do with the question.")
        recalls.reinforce([rec.id] * 100)
        hits = store.search("kubernetes ingress annotations", federated=False)
        self.assertEqual([m.title for m in hits], [],
                         "reinforcement pulled in a memory that did not match")

    def test_forgetting_a_memory_forgets_its_count(self):
        from foldcrumbs import recalls
        rec = self._put("Lockfile", "The lockfile is committed.")
        store.search("lockfile", federated=False)
        self.assertIn(rec.id, recalls.counts())
        store.forget(rec.filename())
        self.assertNotIn(rec.id, recalls.counts(),
                         "the count outlived the memory it described")

    def test_the_sidecar_is_not_read_as_a_memory(self):
        from foldcrumbs import recalls
        self._put("Lockfile", "The lockfile is committed.")
        store.search("lockfile", federated=False)
        self.assertTrue((Path(self.dir) / recalls.SIDECAR).is_file())
        titles = [m.title for m in store.load_all()]
        self.assertEqual(titles, ["Lockfile"],
                         f"the sidecar leaked into the store: {titles}")

    def test_recall_survives_a_store_that_cannot_be_written(self):
        # Reinforcement is advisory. A read-only store must still answer.
        from foldcrumbs import recalls
        self._put("Lockfile", "The lockfile is committed.")
        real = recalls._write

        def refuses(target, data):
            raise OSError(errno.EROFS, "read-only file system")

        recalls._write = refuses
        try:
            hits = store.search("lockfile", federated=False)
        finally:
            recalls._write = real
        self.assertEqual([m.title for m in hits], ["Lockfile"],
                         "a failed count cost the caller its answer")


class TestFreshnessRanking(TmpStore):
    """Between two equally relevant answers, the more recent one goes first."""

    def _put(self, title, body, days_old=0):
        from datetime import datetime, timedelta, timezone
        rec = MemoryRecord(title=title, content=body, type="fact")
        if days_old:
            rec.created_at = (datetime.now(timezone.utc)
                              - timedelta(days=days_old))
        store.write_memory(rec)
        return rec

    def test_the_more_recent_of_two_equal_answers_comes_first(self):
        # Named so the filename tiebreak, which runs last, would put the old
        # one first: otherwise the test passes without freshness doing
        # anything at all.
        old = self._put("Deploy alpha", "Deploy runs on Tuesday.", days_old=400)
        new = self._put("Deploy zulu", "Deploy runs on Tuesday.")
        self.assertLess(old.filename(), new.filename(),
                        "the fixture no longer contradicts alphabetical order")
        hits = store.search("deploy runs on tuesday", federated=False)
        self.assertEqual([m.id for m in hits], [new.id, old.id],
                         "the year-old memory was shown first")

    def test_recency_never_outranks_a_better_match(self):
        # Age is a weak signal — a decision from last year can be exactly the
        # answer — so it must not promote a worse match, however fresh.
        exact = self._put("Deploy exact", "deploy runs tuesday at nine",
                          days_old=400)
        near = self._put("Deploy near", "deploy always runs on a tuesday morning")
        hits = store.search("deploy runs tuesday", federated=False)
        self.assertEqual({m.id for m in hits}, {exact.id, near.id},
                         "the fixture stopped matching both")
        self.assertEqual(hits[0].id, exact.id,
                         "a fresher near match outranked the exact one")

    def test_a_memory_with_no_date_is_neither_fresh_nor_stale(self):
        # Reading a missing date as the epoch would bury every memory written
        # before dates were serialized — the opposite of not knowing.
        from foldcrumbs import recalls
        undated = Path(self.dir) / "fact_undated.md"
        undated.write_text(
            "---\nname: Undated\ndescription: hook\ntype: fact\n---\n\n"
            "Deploy runs on Tuesday.\n", encoding="utf-8")
        rec = [m for m in store.load_all() if m.title == "Undated"][0]
        self.assertTrue(rec.created_at_missing)
        self.assertEqual(recalls.freshness(rec), 0.5)
        ancient = self._put("Deploy ancient", "Deploy runs on Tuesday.",
                            days_old=3000)
        self.assertLess(recalls.freshness(ancient), 0.5)
        hits = store.search("deploy runs on tuesday", federated=False)
        self.assertEqual([m.title for m in hits[:2]],
                         ["Undated", "Deploy ancient"],
                         "an undated memory was treated as ancient")

    def test_a_clock_skew_does_not_bury_a_memory(self):
        # A memory dated in the future is a wrong clock somewhere, not a
        # reason to rank it last.
        from foldcrumbs import recalls
        future = self._put("Deploy future", "Deploy runs on Tuesday.",
                           days_old=-30)
        self.assertEqual(recalls.freshness(future), 1.0)

    def test_use_still_separates_two_memories_of_the_same_age(self):
        # Freshness leads the tiebreak but does not own it: written together,
        # the one that has actually been needed goes first.
        from foldcrumbs import recalls
        a = self._put("Deploy A", "Deploy runs on Tuesday.")
        b = self._put("Deploy B", "Deploy runs on Tuesday.")
        recalls.reinforce([b.id] * 10)
        hits = store.search("deploy runs on tuesday", federated=False)
        self.assertEqual([m.id for m in hits], [b.id, a.id],
                         "use stopped separating memories of the same age")


class TestDecay(TmpStore):
    """Decay that acts: a store where nothing ever leaves competes with itself."""

    def _stale(self, title, body):
        # An old, weakly-sourced preference: the age penalty and the
        # provenance weight are both already in compute_confidence, and both
        # survive a round trip through the file. The pass reads that number —
        # it does not invent a decay of its own.
        from datetime import datetime, timedelta, timezone
        rec = MemoryRecord(title=title, content=body, type="preference",
                           confidence=0.3, provenance="inferred")
        old = datetime.now(timezone.utc) - timedelta(days=120)
        rec.created_at = rec.updated_at = old       # untouched since, too
        store.write_memory(rec)
        self.assertLess(store.get(rec.filename()).compute_confidence(),
                        0.3, "the fixture is no longer stale on reload")
        return rec

    def test_a_dry_run_names_candidates_without_touching_them(self):
        from foldcrumbs import audit
        stale = self._stale("Old rule", "Nobody follows this any more.")
        res = audit.decay()
        self.assertIn(stale.filename(), res["candidates"])
        self.assertEqual(res["archived"], [])
        self.assertEqual(store.get(stale.filename()).status, "active",
                         "a dry run changed the store")

    def test_applying_archives_without_deleting(self):
        from foldcrumbs import audit
        stale = self._stale("Old rule", "Nobody follows this any more.")
        body = (Path(self.dir) / stale.filename()).read_text()
        res = audit.decay(apply=True)
        self.assertEqual(res["archived"], [stale.filename()])
        self.assertTrue((Path(self.dir) / stale.filename()).is_file(),
                        "archiving deleted the file")
        self.assertIn("Nobody follows this any more.",
                      (Path(self.dir) / stale.filename()).read_text(),
                      "archiving lost the memory's content")
        self.assertNotEqual(body, (Path(self.dir) / stale.filename()).read_text())
        self.assertEqual(store.get(stale.filename()).status, "archived")

    def test_an_archived_memory_leaves_recall_and_the_index(self):
        from foldcrumbs import audit
        stale = self._stale("Deployment ritual", "Nobody follows this any more.")
        self.assertTrue(store.search("deployment ritual", federated=False))
        audit.decay(apply=True)
        self.assertEqual(store.search("deployment ritual", federated=False), [],
                         "an archived memory was still recalled")
        self.assertNotIn(stale.filename(), store.rebuild_index().read_text(),
                         "an archived memory was still indexed")

    def test_restoring_brings_it_back_whole(self):
        # Decaying out of relevance is not the same as having been wrong, and
        # only the second deserves to be unrecoverable.
        from foldcrumbs import audit
        stale = self._stale("Deployment ritual", "Nobody follows this any more.")
        audit.decay(apply=True)
        self.assertTrue(store.set_status(stale.filename(), "active"))
        back = store.get(stale.filename())
        self.assertEqual(back.status, "active")
        self.assertEqual(back.content, "Nobody follows this any more.")
        self.assertTrue(store.search("deployment ritual", federated=False),
                        "a restored memory was not recalled again")

    def test_restore_does_not_revive_what_was_superseded_or_deleted(self):
        # Those did not decay out of relevance: something replaced them, or
        # someone removed them. Reviving them here would undo a decision this
        # call knows nothing about.
        old = MemoryRecord(title="Deadline", content="Ship on Friday.",
                           type="fact")
        new = MemoryRecord(title="Deadline moved", content="Ship on Monday.",
                           type="fact")
        store.write_memory(old)
        store.write_memory(new)
        self.assertTrue(store.supersede(old.filename(), new.filename()))
        self.assertFalse(store.set_status(old.filename(), "active"),
                         "a superseded memory was brought back")
        self.assertEqual(store.get(old.filename()).status, "superseded")

        gone = MemoryRecord(title="Retired", content="Not wanted any more.",
                            type="fact")
        store.write_memory(gone)
        self.assertEqual(store.forget(gone.filename()), "deleted")
        self.assertFalse(store.set_status(gone.filename(), "active"),
                         "a deleted memory was brought back")
        self.assertEqual(store.get(gone.filename()).status, "deleted")

    def test_archiving_only_takes_active_memories(self):
        # The other direction of the same rule.
        old = MemoryRecord(title="Deadline", content="Ship on Friday.",
                           type="fact")
        new = MemoryRecord(title="Deadline moved", content="Ship on Monday.",
                           type="fact")
        store.write_memory(old)
        store.write_memory(new)
        self.assertTrue(store.supersede(old.filename(), new.filename()))
        self.assertFalse(store.set_status(old.filename(), "archived"),
                         "a superseded memory was archived over")
        self.assertEqual(store.get(old.filename()).status, "superseded")

    def test_a_freshly_written_memory_is_given_its_turn(self):
        # Distillation writes `inferred` records at modest confidence, so a
        # brand-new memory can already sit below the threshold. Archiving it
        # on the next run means it never got used because it was never
        # offered — decay is about having had a chance, not about the number.
        from foldcrumbs import audit
        new = MemoryRecord(title="Fresh guess", content="Probably prefers this.",
                           type="preference", confidence=0.3,
                           provenance="inferred")
        store.write_memory(new)
        self.assertLess(store.get(new.filename()).compute_confidence(),
                        audit.STALE_CONF, "the fixture is no longer low-trust")
        res = audit.decay(apply=True)
        self.assertNotIn(new.filename(), res["candidates"],
                         "a memory was archived before it had been offered")
        self.assertEqual(store.get(new.filename()).status, "active")

    def test_validating_a_memory_gives_it_its_grace_back(self):
        # `validate` moves updated_at, so a memory just confirmed by use
        # starts its chance again rather than being archived next run.
        from foldcrumbs import audit
        stale = self._stale("Old rule", "Nobody follows this any more.")
        self.assertIn(stale.filename(), audit.decay()["candidates"])
        rec = store.get(stale.filename())
        rec.validate()
        store.write_memory(rec)
        self.assertNotIn(stale.filename(), audit.decay()["candidates"],
                         "a re-validated memory was archived anyway")

    def _undated(self, name, front):
        path = Path(self.dir) / name
        path.write_text(
            "---\nname: Undated\ndescription: hook\ntype: preference\n"
            f"confidence: 0.3\nprovenance: inferred\n{front}---\n\n"
            "Written before dates were serialized.\n", encoding="utf-8")
        return [m for m in store.load_all() if m.title == "Undated"][0]

    def test_a_memory_with_no_date_at_all_keeps_its_place(self):
        # An unknown date is not evidence of age, and archiving removes it
        # from recall — the one place it could earn its way back.
        from foldcrumbs import audit
        rec = self._undated("preference_undated.md", "")
        self.assertTrue(rec.created_at_missing and rec.updated_at_missing)
        self.assertLess(rec.compute_confidence(), audit.STALE_CONF)
        self.assertEqual(audit.decay()["candidates"], {},
                         "a memory was archived on an age nobody knows")

    def test_a_legacy_memory_with_only_a_creation_date_still_decays(self):
        # Both timestamps default to "now" when absent, so reading the value
        # rather than asking whether the file carries it made such a memory
        # look untouched a second ago — and it would never decay at all.
        from foldcrumbs import audit
        rec = self._undated("preference_undated.md",
                            "created_at: 2020-01-01T00:00:00+00:00\n")
        self.assertTrue(rec.updated_at_missing)
        self.assertFalse(rec.created_at_missing)
        self.assertIn("preference_undated.md", audit.decay()["candidates"],
                      "a memory dated only by its creation never decayed")

    def test_a_memory_dated_only_by_an_update_decays_on_that(self):
        from foldcrumbs import audit
        rec = self._undated("preference_undated.md",
                            "updated_at: 2020-01-01T00:00:00+00:00\n")
        self.assertTrue(rec.created_at_missing)
        self.assertIn("preference_undated.md", audit.decay()["candidates"])

    def test_a_stale_update_date_cannot_age_out_a_new_memory(self):
        # An imported or hand-edited file can carry an updated_at older than
        # its creation date. Preferring updated_at outright would archive a
        # memory that was in fact just written, so the age comes from the
        # newest date on the file, not the first one that happens to be there.
        from datetime import datetime, timezone
        from foldcrumbs import audit
        rec = self._undated(
            "preference_undated.md",
            "created_at: {}\nupdated_at: 2020-01-01T00:00:00+00:00\n".format(
                datetime.now(timezone.utc).isoformat()))
        self.assertLess(rec.compute_confidence(), audit.STALE_CONF)
        self.assertEqual(audit.decay()["candidates"], {},
                         "a memory written today was archived on an old "
                         "updated_at")

    def test_a_timestamp_without_a_zone_does_not_stop_the_pass(self):
        # A hand-edited file can carry a timestamp with no offset. Comparing
        # those against aware ones makes max() raise, which would take down
        # the whole sweep over one file instead of skipping it.
        from foldcrumbs import audit
        stale = self._stale("Old rule", "Nobody follows this any more.")
        naive = Path(self.dir) / "preference_hand_edited.md"
        naive.write_text(
            "---\nname: Hand edited\ndescription: hook\ntype: preference\n"
            "confidence: 0.3\nprovenance: inferred\n"
            "created_at: 2020-01-01T00:00:00\n"
            "updated_at: 2020-01-02T00:00:00+00:00\n---\n\n"
            "Someone typed these dates by hand.\n", encoding="utf-8")
        rec = [m for m in store.load_all() if m.title == "Hand edited"][0]
        self.assertIsNotNone(rec.created_at.tzinfo,
                             "the parser stopped attaching a zone")
        res = audit.decay()          # must not raise
        self.assertIn(stale.filename(), res["candidates"],
                      "one hand-edited file stopped the whole pass")
        self.assertIn("preference_hand_edited.md", res["candidates"],
                      "a naive timestamp was not read as UTC")

    def test_a_sweep_rebuilds_the_index_once(self):
        # Rebuilding per memory means a large store rewrites its index as many
        # times as it archives.
        from foldcrumbs import audit
        for i in range(5):
            self._stale(f"Old rule {i}", "Nobody follows this any more.")
        builds, real = [], store.rebuild_index

        def counting(cwd=None):
            builds.append(1)
            return real(cwd)

        store.rebuild_index = counting
        try:
            res = audit.decay(apply=True)
        finally:
            store.rebuild_index = real
        self.assertEqual(len(res["archived"]), 5)
        self.assertEqual(len(builds), 1,
                         f"the index was rebuilt {len(builds)} times for "
                         f"{len(res['archived'])} memories")

    def test_an_interrupted_sweep_still_leaves_the_index_truthful(self):
        # If the rebuild is skipped on the way out, the index keeps
        # advertising memories that are no longer there to answer.
        from foldcrumbs import audit
        stale = [self._stale(f"Old rule {i}", "Nobody follows this any more.")
                 for i in range(3)]
        real = store.set_status
        calls = []

        def fails_partway(name, status, cwd=None, rebuild=True):
            calls.append(name)
            if len(calls) == 2:
                raise OSError(errno.EIO, "the disk gave up")
            return real(name, status, cwd, rebuild)

        store.set_status = fails_partway
        try:
            with self.assertRaises(OSError):
                audit.decay(apply=True)
        finally:
            store.set_status = real
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        archived = [m for m in store.load_all() if m.status == "archived"]
        self.assertTrue(archived, "the sweep archived nothing at all")
        for m in archived:
            self.assertNotIn(m.filename(), idx,
                             "an interrupted sweep left an archived memory "
                             "advertised in the index")
        self.assertEqual(len(stale), 3)

    def test_a_retired_memory_left_in_the_index_is_healed(self):
        # Archived, superseded and deleted records keep their files, so a
        # stale entry for one is not a dead link — and without noticing it,
        # the index stays wrong for good: whatever retired them has already
        # run, and nothing later looks again.
        from foldcrumbs import audit
        stale = self._stale("Old rule", "Nobody follows this any more.")
        audit.decay(apply=True)
        # Put the index back the way an interrupted sweep would have left it.
        index = Path(self.dir) / "MEMORY.md"
        index.write_text(
            index.read_text() + f"\n- [Old rule]({stale.filename()}) — hook\n",
            encoding="utf-8")
        report = audit.audit()
        self.assertIn(stale.filename(), report["retired_links"],
                      "a retired memory in the index went unnoticed")
        self.assertEqual(report["dead_links"], [],
                         "its file is on disk, so it is not a dead link")
        self.assertTrue(audit.heal_index(), "the index was not healed")
        self.assertNotIn(stale.filename(),
                         index.read_text(), "healing left it advertised")

    def test_doctor_does_not_call_a_store_with_retired_links_healthy(self):
        # The audit learned to see them; the report a person actually reads
        # did not, so the store looked fine while its index advertised
        # memories that answer nothing.
        import contextlib as _c
        import io
        from foldcrumbs import audit, cli
        stale = self._stale("Old rule", "Nobody follows this any more.")
        audit.decay(apply=True)
        index = Path(self.dir) / "MEMORY.md"
        index.write_text(
            index.read_text() + f"\n- [Old rule]({stale.filename()}) — hook\n",
            encoding="utf-8")
        out = io.StringIO()
        with _c.redirect_stdout(out):
            cli._cmd_doctor(None)
        printed = out.getvalue()
        self.assertIn("retired", printed,
                      "doctor never mentions retired links")
        self.assertIn(stale.filename(), printed,
                      "doctor reported a healthy store while the index was "
                      "advertising an archived memory")
        self.assertIn("foldcrumbs index", printed,
                      "doctor found it but suggested nothing")

    def test_a_trusted_memory_is_never_archived(self):
        from foldcrumbs import audit
        keep = MemoryRecord(title="Live rule", content="Still true today.",
                            type="instruction", confidence=0.9)
        store.write_memory(keep)
        self._stale("Old rule", "Nobody follows this any more.")
        res = audit.decay(apply=True)
        self.assertNotIn(keep.filename(), res["candidates"])
        self.assertEqual(store.get(keep.filename()).status, "active")

    def test_archiving_takes_the_recall_history_with_it(self):
        from foldcrumbs import audit, recalls
        stale = self._stale("Deployment ritual", "Nobody follows this any more.")
        store.search("deployment ritual", federated=False)
        self.assertIn(stale.id, recalls.counts())
        audit.decay(apply=True)
        self.assertNotIn(stale.id, recalls.counts(),
                         "an archived memory kept weighting the ranking")

    def test_recall_never_archives_anything(self):
        # Reading must not silently change what the store contains.
        stale = self._stale("Deployment ritual", "Nobody follows this any more.")
        for _ in range(5):
            store.search("deployment ritual", federated=False)
        self.assertEqual(store.get(stale.filename()).status, "active",
                         "recall archived a memory behind the caller's back")


class TestDistill(unittest.TestCase):
    def test_parser_tolerates_fences(self):
        text = '```json\n[{"type":"decision","title":"x","content":"c","confidence":0.9}]\n```'
        out = distill.parse_llm_memories(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "decision")

    def test_gate_filters_low_confidence(self):
        self.assertFalse(distill._passes_gate(
            {"type": "fact", "content": "c", "confidence": 0.4}))
        self.assertTrue(distill._passes_gate(
            {"type": "fact", "content": "c", "confidence": 0.8}))

    def test_heuristic_classifies(self):
        h = distill.heuristic_memories("We decided to use X. Always lint first.")
        types = {m["type"] for m in h}
        self.assertIn("decision", types)
        self.assertIn("instruction", types)

    def test_is_artifact_flags_tooling_output(self):
        self.assertTrue(distill._is_artifact("| Index | File | Stato |"))
        self.assertTrue(distill._is_artifact("references MEMORY.md directly"))
        self.assertTrue(distill._is_artifact("Link OK ✓"))
        self.assertFalse(distill._is_artifact("We use os.replace for atomic writes."))
        # Generality: legit project prose must survive — e.g. a web project that
        # genuinely fixes broken links is NOT a tooling artifact.
        self.assertFalse(distill._is_artifact("Fixed the broken links on the docs page."))

    def test_heuristic_drops_self_referential_artifacts(self):
        h = distill.heuristic_memories(
            "We decided to use Postgres. Bug: dead links in MEMORY.md after rename.")
        joined = " ".join(m["content"] for m in h).lower()
        self.assertIn("postgres", joined)
        self.assertNotIn("dead links", joined)


class TestLLMBackend(unittest.TestCase):
    """CLI backends (claude-cli, codex): dispatch + the anti-recursion kill-switch."""

    def setUp(self):
        import importlib
        from foldcrumbs import config, llm
        self.config, self.llm, self._reload = config, llm, importlib.reload
        self._saved = {k: os.environ.get(k)
                       for k in ("ENGRAM_LLM_BACKEND", "ENGRAM_DISABLE",
                                 "ENGRAM_CLAUDE_BIN", "ENGRAM_CODEX_BIN")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._reload(self.config)
        self._reload(self.llm)

    def _reload_with(self, **env):
        for k, v in env.items():
            os.environ[k] = v
        self._reload(self.config)
        self._reload(self.llm)
        return self.llm

    def test_disabled_blocks_claude_cli_and_never_spawns(self):
        # Recursion guard: inside a foldcrumbs-spawned session the CLI backend is
        # unavailable and chat() returns None without shelling out.
        llm = self._reload_with(ENGRAM_LLM_BACKEND="claude-cli", ENGRAM_DISABLE="1")
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))

    def test_available_true_when_cli_present(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="claude-cli", ENGRAM_CLAUDE_BIN=sys.executable)
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertTrue(llm.available())  # sys.executable always exists

    def test_disabled_blocks_codex_and_never_spawns(self):
        # Same recursion-guard parity for the codex backend: disabled => no spawn.
        llm = self._reload_with(ENGRAM_LLM_BACKEND="codex", ENGRAM_DISABLE="1")
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))

    def test_codex_available_true_when_cli_present(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="codex", ENGRAM_CODEX_BIN=sys.executable)
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertTrue(llm.available())  # sys.executable always exists

    def test_codex_available_false_when_cli_missing(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="codex",
            ENGRAM_CODEX_BIN="/nonexistent/codex-binary-xyz")
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertFalse(llm.available())

    def test_none_backend_skips_llm_entirely(self):
        # The heuristic-only rung: chat() returns None without any network/CLI,
        # and available() is False by design (distill falls to the keyword path).
        llm = self._reload_with(ENGRAM_LLM_BACKEND="none")
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))


class TestBackendConfig(unittest.TestCase):
    """Install-time backend selection: configure_backend + prompt_backend."""

    def setUp(self):
        import importlib
        from foldcrumbs import config
        self.config = config
        self._dir = Path(tempfile.mkdtemp(prefix="ccmem_backend_"))
        # Drive STATE_DIR via env so an importlib.reload (below) keeps the temp
        # dir instead of snapping back to ~/.foldcrumbs. It must be the
        # FOLDCRUMBS_* name: setting only the legacy ENGRAM_* one is exactly
        # how this class used to write the developer's real state dir whenever
        # a FOLDCRUMBS_STATE_DIR existed to outrank it.
        self._saved_env = {k: os.environ.get(k) for k in
                           ("FOLDCRUMBS_STATE_DIR", "ENGRAM_STATE_DIR")}
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(self._dir)
        os.environ.pop("ENGRAM_STATE_DIR", None)
        importlib.reload(config)
        assert config.STATE_DIR == self._dir, "backend test escaped its temp dir"

    def tearDown(self):
        import importlib
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(self.config)

    def _read(self, name):
        return (self._dir / name).read_text(encoding="utf-8").strip()

    def test_configure_codex_writes_backend_and_bin(self):
        written = install.configure_backend("codex", bin_path="/opt/homebrew/bin/codex")
        self.assertIn("llm-backend", written)
        self.assertIn("codex-bin", written)
        self.assertEqual(self._read("llm-backend"), "codex")
        self.assertEqual(self._read("codex-bin"), "/opt/homebrew/bin/codex")

    def test_configure_claude_writes_backend_and_bin(self):
        install.configure_backend("claude-cli", bin_path="/usr/local/bin/claude")
        self.assertEqual(self._read("llm-backend"), "claude-cli")
        self.assertEqual(self._read("claude-bin"), "/usr/local/bin/claude")

    def test_configure_openai_persists_endpoint_and_model(self):
        install.configure_backend(
            "openai", endpoint="http://localhost:8081", model="gemma-4-26b-a4b-it")
        self.assertEqual(self._read("llm-backend"), "openai")
        self.assertEqual(self._read("llm-endpoint"), "http://localhost:8081")
        self.assertEqual(self._read("llm-model"), "gemma-4-26b-a4b-it")

    def test_configure_none_writes_only_marker(self):
        written = install.configure_backend("none")
        self.assertEqual(written, ["llm-backend"])
        self.assertEqual(self._read("llm-backend"), "none")

    def test_configure_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            install.configure_backend("gpt-9000")

    def test_config_reads_endpoint_from_state_file(self):
        # The openai endpoint/model written above must be picked up by config
        # (env unset) — that's what makes the install prompt meaningful.
        import importlib
        install.configure_backend("openai", endpoint="http://host:9999", model="m-1")
        saved = {k: os.environ.pop(k, None)
                 for k in ("ENGRAM_LLM_ENDPOINT", "ENGRAM_LLM_MODEL")}
        try:
            importlib.reload(self.config)
            self.assertEqual(self.config.LLM_ENDPOINT, "http://host:9999")
            self.assertEqual(self.config.LLM_MODEL, "m-1")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(self.config)

    def test_prompt_picks_by_number(self):
        choice = install.prompt_backend(in_fn=lambda _: "2", out_fn=lambda *_: None)
        self.assertEqual(choice, "codex")

    def test_prompt_picks_by_name(self):
        choice = install.prompt_backend(in_fn=lambda _: "openai", out_fn=lambda *_: None)
        self.assertEqual(choice, "openai")

    def test_prompt_blank_is_default_first_choice(self):
        choice = install.prompt_backend(in_fn=lambda _: "", out_fn=lambda *_: None)
        self.assertEqual(choice, install.BACKEND_CHOICES[0][0])

    def test_prompt_unrecognised_returns_none(self):
        choice = install.prompt_backend(in_fn=lambda _: "zzz", out_fn=lambda *_: None)
        self.assertIsNone(choice)

    def test_prompt_eof_returns_none(self):
        def _eof(_):
            raise EOFError
        self.assertIsNone(install.prompt_backend(in_fn=_eof, out_fn=lambda *_: None))


class TestAutoSupersede(TmpStore):
    """The contradiction pass: a new memory can obsolete an old same-subject one."""

    def _old_pypi_decision(self) -> MemoryRecord:
        rec = MemoryRecord(
            title="Launch is GitHub only",
            content="PyPI publishing is deferred; the launch is GitHub-only for now.",
            type="decision")
        store.write_memory(rec)
        return rec

    def _new_pypi_fact(self) -> MemoryRecord:
        return MemoryRecord(
            title="Published to PyPI",
            content="foldcrumbs is published on PyPI, installable via pip install foldcrumbs.",
            type="fact")

    def test_conflict_candidates_same_subject_cross_type(self):
        self._old_pypi_decision()
        store.write_memory(MemoryRecord(title="Postgres storage",
                                        content="We use Postgres for storage.",
                                        type="decision"))
        names = [m.title for m in store.find_conflict_candidates(self._new_pypi_fact())]
        self.assertEqual(names, ["Launch is GitHub only"])

    def test_llm_yes_supersedes_old(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value='{"supersedes": true}'):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 1)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "superseded")
        self.assertEqual(reloaded.compute_confidence(), 0.0)
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertNotIn("Launch is GitHub only", idx)
        self.assertIn("Published to PyPI", idx)

    def test_llm_no_keeps_old(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value='{"supersedes": false}'):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 0)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "active")

    def test_no_llm_fails_soft(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value=None):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 0)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "active")

    def test_kill_switch_skips_llm_entirely(self):
        self._old_pypi_decision()
        os.environ["FOLDCRUMBS_NO_AUTO_SUPERSEDE"] = "1"
        try:
            from unittest.mock import patch
            with patch.object(distill.llm, "chat",
                              side_effect=AssertionError("LLM must not be called")):
                res = distill.persist([self._new_pypi_fact()])
        finally:
            os.environ.pop("FOLDCRUMBS_NO_AUTO_SUPERSEDE", None)
        self.assertEqual(res["superseded"], 0)

    def test_validated_duplicate_never_triggers_pass(self):
        # A near-duplicate validates (dedup) instead of creating; the
        # contradiction pass runs only for genuinely new memories.
        rec = MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                           type="decision")
        store.write_memory(rec)
        dup = MemoryRecord(title="Use stdlib only",
                           content="Hooks use only stdlib here now.", type="decision")
        from unittest.mock import patch
        with patch.object(distill.llm, "chat",
                          side_effect=AssertionError("LLM must not be called")):
            res = distill.persist([dup])
        self.assertEqual(res["validated"], 1)
        self.assertEqual(res["superseded"], 0)


class TestDistillGate(unittest.TestCase):
    """Per-machine distill opt-out (shared-store read-only consumer)."""

    def setUp(self):
        self._saved = os.environ.get("ENGRAM_NO_DISTILL")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ENGRAM_NO_DISTILL", None)
        else:
            os.environ["ENGRAM_NO_DISTILL"] = self._saved

    def test_enabled_by_default(self):
        os.environ.pop("ENGRAM_NO_DISTILL", None)
        from foldcrumbs import config
        # No marker in a throwaway state dir → enabled.
        self.assertTrue(config.distill_enabled() or (config.STATE_DIR / "no-distill").exists())

    def test_env_disables(self):
        os.environ["ENGRAM_NO_DISTILL"] = "1"
        from foldcrumbs import config
        self.assertFalse(config.distill_enabled())

    def test_marker_disables(self):
        os.environ.pop("ENGRAM_NO_DISTILL", None)
        from foldcrumbs import config
        d = tempfile.mkdtemp(prefix="ccmem_state_")
        saved = config.STATE_DIR
        try:
            config.STATE_DIR = Path(d)
            self.assertTrue(config.distill_enabled())
            (Path(d) / "no-distill").write_text("", encoding="utf-8")
            self.assertFalse(config.distill_enabled())
        finally:
            config.STATE_DIR = saved

    def test_machine_local_backend_override(self):
        # A machine-local file selects the backend without any env var (the
        # mechanism that lets one synced machine differ from the others).
        from foldcrumbs import config
        saved_env = os.environ.pop("ENGRAM_LLM_BACKEND", None)
        saved_codex_bin = os.environ.pop("ENGRAM_CODEX_BIN", None)
        d = tempfile.mkdtemp(prefix="ccmem_state_")
        saved = config.STATE_DIR
        try:
            config.STATE_DIR = Path(d)
            self.assertEqual(config.llm_backend(), "openai")  # default
            (Path(d) / "llm-backend").write_text("claude-cli\n", encoding="utf-8")
            self.assertEqual(config.llm_backend(), "claude-cli")
            (Path(d) / "llm-backend").write_text("codex\n", encoding="utf-8")
            self.assertEqual(config.llm_backend(), "codex")
            # codex_bin honours the machine-local file too (no env var).
            self.assertEqual(config.codex_bin(), "codex")  # default name
            (Path(d) / "codex-bin").write_text("/opt/homebrew/bin/codex\n", encoding="utf-8")
            self.assertEqual(config.codex_bin(), "/opt/homebrew/bin/codex")
        finally:
            config.STATE_DIR = saved
            if saved_env is not None:
                os.environ["ENGRAM_LLM_BACKEND"] = saved_env
            if saved_codex_bin is not None:
                os.environ["ENGRAM_CODEX_BIN"] = saved_codex_bin


class TestRedact(unittest.TestCase):
    def test_scrubs_known_tokens(self):
        out = redact.scrub("key is sk-abcdefabcdefabcdefabcdef and gho_" + "a" * 36)
        self.assertNotIn("sk-abcdef", out)
        self.assertNotIn("gho_a", out)
        self.assertIn("[REDACTED]", out)

    def test_scrubs_kv_secret(self):
        out = redact.scrub('password = "hunter2secret"')
        self.assertNotIn("hunter2secret", out)
        self.assertIn("password", out)  # key name kept, value gone

    def test_keeps_normal_text(self):
        text = "We use os.replace for atomic writes."
        self.assertEqual(redact.scrub(text), text)


class TestAudit(TmpStore):
    def _write_raw(self, name, name_field, content, type_="fact"):
        (Path(self.dir) / name).write_text(
            f"---\nname: {name_field}\ndescription: d\ntype: {type_}\n---\n\n{content}\n",
            encoding="utf-8")

    def test_heal_index_relinks_orphan(self):
        from foldcrumbs import audit
        # A memory file present on disk but not in the index → heal rebuilds.
        self._write_raw("note.md", "Some note", "body")
        a = audit.audit()
        self.assertIn("note.md", a["orphans"])
        self.assertTrue(audit.heal_index())
        self.assertIn("(note.md)", store.rebuild_index().read_text())
        self.assertEqual(audit.audit()["orphans"], [])

    def test_audit_flags_pollution(self):
        from foldcrumbs import audit
        store.upsert(MemoryRecord(title="Good", content="We use os.replace.", type="decision"))
        self._write_raw("error_junk.md", "junk", "| Index | File | Stato |", "error")
        self.assertIn("error_junk.md", audit.audit()["pollution"])

    def test_prune_dry_run_then_apply(self):
        from foldcrumbs import audit
        self._write_raw("error_tbl.md", "tbl", "| a | b | c |", "error")
        store.upsert(MemoryRecord(title="Keep", content="Real decision here.", type="decision"))
        dry = audit.prune(apply=False)
        self.assertIn("error_tbl.md", dry["candidates"])
        self.assertEqual(dry["removed"], [])
        self.assertTrue((Path(self.dir) / "error_tbl.md").exists())
        done = audit.prune(apply=True)
        self.assertIn("error_tbl.md", done["removed"])
        self.assertFalse((Path(self.dir) / "error_tbl.md").exists())

    def test_auto_prune_on_persist(self):
        # An artifact memory among real ones is auto-pruned by persist().
        recs = [
            MemoryRecord(title="Real", content="We chose Postgres.", type="decision"),
            MemoryRecord(title="junk", content="| col a | col b | col c |", type="error"),
        ]
        distill.persist(recs)
        names = {m.title for m in store.load_all()}
        self.assertIn("Real", names)
        self.assertNotIn("junk", names)

    def test_auto_prune_spares_legit_memory_mentioning_index(self):
        from foldcrumbs import audit
        # A real foldcrumbs design memory mentions MEMORY.md — must NOT be pruned.
        self._write_raw("decision_arch.md", "Dual-layer architecture",
                        "Durable layer is MEMORY.md; live state is HANDOFF.md.", "decision")
        self.assertNotIn("decision_arch.md", audit.audit()["pollution"])
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertTrue((Path(self.dir) / "decision_arch.md").exists())


class TestImportStore(TmpStore):
    def _make_src(self) -> Path:
        src = Path(tempfile.mkdtemp(prefix="ccmem_src_"))
        for rec in (
            MemoryRecord(title="Uses Postgres", content="We use Postgres.",
                         type="decision"),
            MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                         type="decision"),
        ):
            (src / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        # Noise that must be skipped: index, handoffs, non-frontmatter, non-active.
        (src / "MEMORY.md").write_text("# index\n", encoding="utf-8")
        (src / "HANDOFF.md").write_text("# resume\n", encoding="utf-8")
        (src / "HANDOFF.engram-2026-07-06.md").write_text("# old\n", encoding="utf-8")
        (src / "notes.md").write_text("just a note, no frontmatter\n", encoding="utf-8")
        gone = MemoryRecord(title="Old way", content="We used MySQL.", type="decision")
        gone.status = "superseded"
        (src / gone.filename()).write_text(gone.to_markdown(), encoding="utf-8")
        return src

    def test_dry_run_plans_without_writing(self):
        src = self._make_src()
        # Target already holds a near-duplicate of one source memory.
        store.upsert(MemoryRecord(title="Use stdlib only",
                                  content="Hooks use only stdlib here now.",
                                  type="decision"))
        plan = store.import_store(src)
        self.assertEqual(plan["created"], ["decision_uses_postgres.md"])
        self.assertEqual(plan["validated"], ["decision_use_stdlib.md"])
        self.assertEqual(sorted(plan["skipped"]),
                         ["decision_old_way.md", "notes.md"])
        # Dry-run: nothing was written.
        self.assertEqual(len(store.load_all()), 1)

    def test_apply_merges_and_rebuilds_index(self):
        src = self._make_src()
        plan = store.import_store(src, apply=True)
        self.assertEqual(len(plan["created"]), 2)
        titles = {m.title for m in store.load_all()}
        self.assertEqual(titles, {"Uses Postgres", "Use stdlib"})
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertIn("Uses Postgres", idx)

    def test_apply_is_idempotent(self):
        src = self._make_src()
        store.import_store(src, apply=True)
        plan = store.import_store(src, apply=True)
        self.assertEqual(plan["created"], [])
        self.assertEqual(len(plan["validated"]), 2)
        self.assertEqual(len(store.load_all()), 2)


class TestLifecycle(TmpStore):
    def _make(self, title="Old fact", content="We deploy on Fridays."):
        rec = MemoryRecord(title=title, content=content, type="fact")
        store.write_memory(rec)
        return rec.filename()

    def test_forget_soft_keeps_file_drops_from_index_and_recall(self):
        name = self._make()
        store.rebuild_index()
        self.assertEqual(store.forget(name), "deleted")
        self.assertTrue((Path(self.dir) / name).exists())
        self.assertEqual(store.get(name).status, "deleted")
        self.assertNotIn(name, (Path(self.dir) / "MEMORY.md").read_text())
        self.assertEqual(store.search("deploy fridays"), [])

    def test_forget_hard_removes_file(self):
        name = self._make()
        self.assertEqual(store.forget(name, hard=True), "removed")
        self.assertFalse((Path(self.dir) / name).exists())

    def test_forget_unknown_returns_none(self):
        self.assertIsNone(store.forget("fact_nope.md"))

    def test_supersede_marks_old_and_links_new(self):
        old = self._make("Launch is GitHub only", "PyPI publishing is deferred.")
        new = self._make("Published to PyPI", "foldcrumbs is on PyPI now.")
        self.assertTrue(store.supersede(old, new))
        old_rec, new_rec = store.get(old), store.get(new)
        self.assertEqual(old_rec.status, "superseded")
        self.assertEqual(old_rec.superseded_by, new_rec.id)
        self.assertEqual(old_rec.compute_confidence(), 0.0)
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertNotIn(old, idx)
        self.assertIn(new, idx)

    def test_supersede_unknown_or_self_fails(self):
        name = self._make()
        self.assertFalse(store.supersede(name, "fact_nope.md"))
        self.assertFalse(store.supersede(name, name))

    def test_forgotten_memory_is_prunable(self):
        from foldcrumbs import audit
        name = self._make()
        store.forget(name)
        res = audit.prune(apply=True)
        self.assertIn(name, res["removed"])
        self.assertFalse((Path(self.dir) / name).exists())


class TestStoreContainment(TmpStore):
    """Filename-addressed operations must not reach outside the store."""

    def test_get_refuses_to_escape_the_store(self):
        outside = Path(self.dir).parent / "victim.txt"
        outside.write_text("not a memory\n", encoding="utf-8")
        # from_markdown parses any text into an "Untitled" record, so without a
        # containment check this resolves and forget --hard would unlink it.
        for name in ("../victim.txt", str(outside), "sub/../../victim.txt"):
            self.assertIsNone(store.get(name), name)

    def test_hard_forget_cannot_delete_an_outside_file(self):
        outside = Path(self.dir).parent / "victim2.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        self.assertIsNone(store.forget("../victim2.txt", hard=True))
        self.assertTrue(outside.is_file())

    def test_forget_still_works_on_a_real_memory(self):
        rec = MemoryRecord(title="Doomed", content="Body here.", type="fact")
        store.upsert(rec)
        self.assertEqual(store.forget(rec.filename()), "deleted")


class TestStoreArtifacts(TmpStore):
    """Files that live in the store without being memories."""

    def test_legacy_and_sync_conflict_handoffs_are_not_memories(self):
        # Both were found in a real store: an older dated handoff, and a
        # Syncthing conflict copy. Each parses as an "Untitled" record whose
        # body is the whole file, and federation would show it to every other
        # instance.
        d = Path(self.dir)
        d.mkdir(parents=True, exist_ok=True)
        for name in ("HANDOFF.md", "HANDOFF.engram-2026-07-06.md",
                     "HANDOFF.sync-conflict-20260713-095412-SH53F7C.md",
                     "MEMORY.md"):
            (d / name).write_text("some text\n", encoding="utf-8")
        rec = MemoryRecord(title="Real one", content="An actual memory.")
        store.upsert(rec)
        self.assertEqual([m.title for m in store.iter_memories()], ["Real one"])
        for name in ("HANDOFF.engram-2026-07-06.md",
                     "HANDOFF.sync-conflict-20260713-095412-SH53F7C.md"):
            self.assertIsNone(store.get(name), name)
            self.assertTrue(store.is_store_artifact(name))
        self.assertFalse(store.is_store_artifact(rec.filename()))


class TestSearch(TmpStore):
    def test_search_ranks_relevant(self):
        store.upsert(MemoryRecord(title="Recall via grep",
                                  content="Recall uses grep, no vector DB.", type="decision"))
        store.upsert(MemoryRecord(title="Atomic writes",
                                  content="Use os.replace.", type="instruction"))
        hits = store.search("vector db", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Recall via grep")

    def test_search_unicode_words(self):
        # Accented words must survive tokenization ([a-z0-9]+ would split
        # "città" into "citt" and lose the word-overlap match).
        store.upsert(MemoryRecord(title="Config della città",
                                  content="La città usa il fuso orario di Roma.",
                                  type="fact"))
        store.upsert(MemoryRecord(title="Atomic writes",
                                  content="Use os.replace.", type="instruction"))
        hits = store.search("fuso orario città", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Config della città")

    def test_search_type_filter(self):
        store.upsert(MemoryRecord(title="Grep decision",
                                  content="Recall uses grep.", type="decision"))
        store.upsert(MemoryRecord(title="Grep fact",
                                  content="Recall uses grep too.", type="fact"))
        hits = store.search("grep", limit=5, types=["fact"])
        self.assertEqual([m.title for m in hits], ["Grep fact"])

    def test_search_tag_filter(self):
        store.upsert(MemoryRecord(title="Tagged", content="Recall uses grep.",
                                  type="decision", tags=["arch"]))
        store.upsert(MemoryRecord(title="Untagged", content="Recall uses grep here.",
                                  type="decision"))
        hits = store.search("grep", limit=5, tags=["ARCH"])
        self.assertEqual([m.title for m in hits], ["Tagged"])


class TestHandoff(TmpStore):
    def test_write_read(self):
        store.write_handoff("# Resume point\n\n- You were editing store.py")
        self.assertIn("Resume point", store.read_handoff())

    def test_handoff_not_indexed_or_searched(self):
        store.write_handoff("# Resume point\n\n- secret working state")
        store.upsert(MemoryRecord(title="A fact", content="grep is recall", type="fact"))
        # Handoff file must not appear as a memory.
        titles = [m.title for m in store.load_all()]
        self.assertNotIn("Resume point", titles)
        self.assertEqual(len(store.load_all()), 1)
        idx = store.rebuild_index().read_text()
        self.assertNotIn("HANDOFF", idx)


class TestSurface(unittest.TestCase):
    def setUp(self):
        from foldcrumbs import surface
        self.surface = surface
        self.dir = Path(tempfile.mkdtemp(prefix="ccmem_surface_")) / "commands"

    def test_install_creates_all_commands_with_marker(self):
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(set(actions.values()), {"created"})
        self.assertEqual(set(actions), {"remember.md", "recall.md",
                                        "forget.md", "foldcrumbs.md"})
        for name in actions:
            text = (self.dir / name).read_text(encoding="utf-8")
            self.assertIn(self.surface.MARKER, text)
            self.assertTrue(text.startswith("---"), name)
            self.assertIn("allowed-tools:", text)
        # Frontmatter must stay valid YAML even when the description contains
        # ": " — values are emitted as quoted scalars.
        mem = (self.dir / "foldcrumbs.md").read_text(encoding="utf-8")
        self.assertIn(
            'description: "Project memory dashboard: status, health, resume point"',
            mem)

    def test_reinstall_is_idempotent_and_refreshes_stale(self):
        self.surface.install_commands(self.dir)
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(set(actions.values()), {"unchanged"})
        # A stale managed file (older template) gets refreshed in place.
        stale = self.dir / "recall.md"
        stale.write_text(f"old body\n<!-- {self.surface.MARKER} -->\n",
                         encoding="utf-8")
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(actions["recall.md"], "refreshed")
        self.assertIn("foldcrumbs recall", stale.read_text(encoding="utf-8"))

    def test_user_owned_file_never_touched(self):
        self.dir.mkdir(parents=True)
        mine = self.dir / "remember.md"
        mine.write_text("my own command\n", encoding="utf-8")
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(actions["remember.md"], "skipped (user file)")
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own command\n")
        # Uninstall must not remove it either.
        removed = self.surface.uninstall_commands(self.dir)
        self.assertNotIn("remember.md", removed)
        self.assertTrue(mine.exists())

    def test_uninstall_removes_only_managed(self):
        self.surface.install_commands(self.dir)
        removed = self.surface.uninstall_commands(self.dir)
        self.assertEqual(sorted(removed), ["foldcrumbs.md", "forget.md",
                                           "recall.md", "remember.md"])
        self.assertEqual(list(self.dir.glob("*.md")), [])

    def test_skill_install_refresh_and_user_protection(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_skill_")) / "skills" / "foldcrumbs"
        self.assertEqual(self.surface.install_skill(d), "created")
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(self.surface.MARKER, text)
        self.assertIn("name: foldcrumbs", text)
        self.assertEqual(self.surface.install_skill(d), "unchanged")
        # Stale managed copy refreshes; user copy is protected.
        (d / "SKILL.md").write_text(f"old\n<!-- {self.surface.MARKER} -->\n",
                                    encoding="utf-8")
        self.assertEqual(self.surface.install_skill(d), "refreshed")
        (d / "SKILL.md").write_text("mine\n", encoding="utf-8")
        self.assertEqual(self.surface.install_skill(d), "skipped (user file)")
        self.assertFalse(self.surface.uninstall_skill(d))
        self.assertTrue((d / "SKILL.md").exists())

    def test_skill_uninstall_removes_managed_and_empty_dir(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_skill_")) / "skills" / "foldcrumbs"
        self.surface.install_skill(d)
        self.assertTrue(self.surface.uninstall_skill(d))
        self.assertFalse(d.exists())

    def test_codex_prompts_no_frontmatter_and_managed_cycle(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_codexp_")) / "prompts"
        actions = self.surface.install_codex_prompts(d)
        self.assertEqual(set(actions.values()), {"created"})
        text = (d / "remember.md").read_text(encoding="utf-8")
        self.assertFalse(text.startswith("---"))  # no Claude frontmatter
        self.assertIn(self.surface.MARKER, text)
        self.assertIn("$ARGUMENTS", text)
        # Codex namespaces prompt files: the header must advertise the real
        # invocation name, /prompts:<stem>, not /<stem>.
        self.assertIn("/prompts:remember", text)
        self.assertEqual(set(self.surface.install_codex_prompts(d).values()),
                         {"unchanged"})
        removed = self.surface.uninstall_codex_prompts(d)
        self.assertEqual(len(removed), 4)

    def test_opencode_commands_merge_preserves_user_keys(self):
        import json as _json
        d = Path(tempfile.mkdtemp(prefix="ccmem_ocode_"))
        cfg = d / "opencode.json"
        cfg.write_text(_json.dumps(
            {"mcp": {"x": {}}, "command": {"remember": {"template": "mine"}}}),
            encoding="utf-8")
        added = self.surface.install_opencode_commands(cfg)
        self.assertEqual(
            {n for n, a in added.items() if a == "created"},
            {"foldcrumbs", "forget", "recall"})
        self.assertEqual(added["remember"], "skipped (user command)")
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(out["command"]["remember"]["template"], "mine")  # user's
        self.assertIn("foldcrumbs recall", out["command"]["recall"]["template"])
        self.assertIn("x", out["mcp"])  # unrelated config untouched
        # Idempotent; a stale marked template is refreshed on reinstall.
        again = self.surface.install_opencode_commands(cfg)
        self.assertNotIn("created", again.values())
        out["command"]["recall"]["template"] = f"old <!-- {self.surface.MARKER} -->"
        cfg.write_text(_json.dumps(out), encoding="utf-8")
        self.assertEqual(
            self.surface.install_opencode_commands(cfg)["recall"], "refreshed")
        final = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertIn("foldcrumbs recall", final["command"]["recall"]["template"])
        # Uninstall removes ours, keeps the user's same-name command.
        removed = self.surface.uninstall_opencode_commands(cfg)
        self.assertEqual(sorted(removed), ["foldcrumbs", "forget", "recall"])
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(out["command"], {"remember": {"template": "mine"}})

    def test_opencode_uninstall_spares_user_command_mentioning_foldcrumbs(self):
        # Ownership = our marker, NOT the word "foldcrumbs": a user command
        # that happens to call foldcrumbs must survive uninstall.
        import json as _json
        d = Path(tempfile.mkdtemp(prefix="ccmem_ocode_"))
        cfg = d / "opencode.json"
        cfg.write_text(_json.dumps({"command": {
            "recall": {"template": "run foldcrumbs recall and summarize"}}}),
            encoding="utf-8")
        self.assertEqual(self.surface.uninstall_opencode_commands(cfg), [])
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertIn("recall", out["command"])

    def test_codex_prompts_dir_honours_codex_home(self):
        os.environ["CODEX_HOME"] = "/tmp/fc-codex-home"
        try:
            self.assertEqual(self.surface.codex_prompts_dir(),
                             Path("/tmp/fc-codex-home/prompts"))
        finally:
            os.environ.pop("CODEX_HOME", None)
        self.assertEqual(self.surface.codex_prompts_dir(),
                         Path.home() / ".codex" / "prompts")

    def test_commands_dir_honours_claude_config_dir(self):
        from foldcrumbs import config as cfg
        # Restore rather than unset: popping it leaves the *default* in force
        # for every later test, and the default is the real ~/.claude.
        saved = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/fc-test-instance"
        try:
            self.assertEqual(self.surface.commands_dir(),
                             Path("/tmp/fc-test-instance/commands"))
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = saved
        self.assertEqual(self.surface.commands_dir(),
                         cfg.claude_config_dir() / "commands")


class TestClaudeMcp(unittest.TestCase):
    def _fake_claude(self, get_rc: int, add_rc: int,
                     get_scope: str = "User", get_extra: str = "") -> str:
        d = Path(tempfile.mkdtemp(prefix="ccmem_mcp_"))
        script = d / "claude"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$2" = "get" ]; then echo "Scope: {get_scope} config"; '
            f'echo "Command: {get_extra}"; '
            f"exit {get_rc}; fi\n"
            f'if [ "$2" = "add" ]; then exit {add_rc}; fi\n'
            'if [ "$2" = "remove" ]; then exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def test_missing_cli_returns_manual_snippet(self):
        out = install.install_claude_mcp(claude_bin="/definitely/not/a/claude")
        self.assertIn("mcpServers", out)
        self.assertIn("foldcrumbs", out)

    def test_already_registered_is_idempotent(self):
        # "Already registered" requires scope AND command/args to match.
        rt = Path(tempfile.mkdtemp(prefix="ccmem_rt_"))
        cmd = install._mcp_command(rt)
        out = install.install_claude_mcp(
            runtime_root=rt,
            claude_bin=self._fake_claude(0, 0, get_extra=" ".join(cmd)))
        self.assertEqual(out, "already registered")

    def test_shadowed_scope_is_replaced_on_add_failure(self):
        # get shows the OTHER scope (project shadowed by user); first add
        # fails because the shadowed entry exists -> remove + retry.
        d = Path(tempfile.mkdtemp(prefix="ccmem_mcp_"))
        state = d / "state"
        script = d / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = "get" ]; then echo "Scope: User config"; exit 0; fi\n'
            f'if [ "$2" = "add" ]; then if [ -f "{state}" ]; then exit 0; '
            f'else touch "{state}"; exit 1; fi; fi\n'
            'if [ "$2" = "remove" ]; then exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        out = install.install_claude_mcp(claude_bin=str(script), scope="project")
        self.assertEqual(out, "refreshed (project scope)")

    def test_stale_registration_is_refreshed(self):
        # Same scope but an old interpreter/runtime path -> remove + re-add.
        rt = Path(tempfile.mkdtemp(prefix="ccmem_rt_"))
        out = install.install_claude_mcp(
            runtime_root=rt,
            claude_bin=self._fake_claude(0, 0,
                                         get_extra="/old/python /old/launcher.py"))
        self.assertEqual(out, "refreshed (user scope)")

    def test_registers_at_requested_scope(self):
        out = install.install_claude_mcp(claude_bin=self._fake_claude(1, 0),
                                         scope="project")
        self.assertEqual(out, "registered (project scope)")

    def test_scope_mismatch_still_registers(self):
        # Server exists at user scope; a --local install must still create the
        # project-scoped entry instead of reporting "already registered".
        out = install.install_claude_mcp(
            claude_bin=self._fake_claude(0, 0, get_scope="User"),
            scope="project")
        self.assertEqual(out, "registered (project scope)")

    def test_add_failure_falls_back_to_snippet(self):
        out = install.install_claude_mcp(claude_bin=self._fake_claude(1, 1))
        self.assertIn("failed", out)
        self.assertIn("mcpServers", out)

    def test_uninstall_paths(self):
        self.assertIn("remove manually",
                      install.uninstall_claude_mcp(claude_bin="/not/a/claude"))
        self.assertEqual(install.uninstall_claude_mcp(
            claude_bin=self._fake_claude(0, 0)), "removed")


class TestInstaller(unittest.TestCase):
    def test_merge_preserves_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            path = Path(d) / "settings.json"
            runtime = Path(d) / "runtime"
            path.write_text(json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "node existing.js"}]}]}}))
            changes = install.install_hooks(path, runtime_root=runtime)
            self.assertTrue(changes)
            s = json.loads(path.read_text())
            cmds = [h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"]]
            self.assertTrue(any("existing.js" in c for c in cmds))  # preserved
            self.assertTrue(any("session_start.py" in c for c in cmds))  # added
            self.assertTrue((runtime / "foldcrumbs" / "hooks" / "session_start.py").exists())
            self.assertEqual(
                install.install_hooks(path, runtime_root=runtime), []
            )  # idempotent

    def test_install_refreshes_hook_from_protected_checkout(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            path = Path(d) / "hooks.json"
            runtime = Path(d) / "runtime"
            source_hook = (
                "/Users/me/Documents/claude/foldcrumbs/"
                "foldcrumbs/hooks/session_start.py"
            )
            path.write_text(json.dumps({"hooks": {"SessionStart": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": f'python3 "{source_hook}"'}],
            }]}}))

            changes = install.install_hooks(
                path, agent="codex", runtime_root=runtime
            )

            self.assertIn("SessionStart -> refreshed session_start.py", changes)
            settings = json.loads(path.read_text())
            commands = [
                hook["command"]
                for groups in settings["hooks"].values()
                for group in groups
                for hook in group["hooks"]
            ]
            self.assertFalse(any("/Documents/" in command for command in commands))
            self.assertTrue(any(str(runtime) in command for command in commands))
            self.assertTrue((runtime / "foldcrumbs" / "config.py").exists())

    def test_codex_mcp_refreshes_editable_install_and_preserves_options(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            config_path = Path(d) / "config.toml"
            runtime = Path(d) / "runtime"
            config_path.write_text(
                "model = \"example\"\n\n"
                "[mcp_servers.foldcrumbs]\n"
                "command = \"python3\"\n"
                "args = [\"-m\", \"foldcrumbs.mcp_server\"]\n"
                "enabled = true\n\n"
                "[features]\n"
                "hooks = true\n"
            )

            status = install.install_codex_mcp_toml(config_path, runtime)

            updated = config_path.read_text()
            launcher = runtime / "foldcrumbs_mcp.py"
            self.assertIn("updated", status)
            self.assertIn(f'args = ["{launcher}"]', updated)
            self.assertNotIn('"-m", "foldcrumbs.mcp_server"', updated)
            self.assertIn("enabled = true", updated)
            self.assertIn("[features]", updated)
            self.assertTrue(launcher.exists())
            self.assertEqual(
                install.install_codex_mcp_toml(config_path, runtime),
                "already present",
            )


class TestHooksIsolation(TmpStore):
    def _run_hook(self, script, payload):
        return subprocess.run(
            [sys.executable, str(REPO / "foldcrumbs" / "hooks" / script)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ}, timeout=30,
        )

    def test_session_start_emits_index(self):
        store.upsert(MemoryRecord(title="X", content="a fact", type="fact"))
        store.rebuild_index()
        r = self._run_hook("session_start.py",
                            {"session_id": "t", "cwd": "/x", "source": "startup"})
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertIn("foldcrumbs-index", out["hookSpecificOutput"]["additionalContext"])

    def test_hook_survives_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, str(REPO / "foldcrumbs" / "hooks" / "session_start.py")],
            input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)


class TestMigration(unittest.TestCase):
    """Rename back-compat + engram -> foldcrumbs migration paths."""

    def test_install_clears_legacy_engram_hook_keeps_foreign(self):
        d = tempfile.mkdtemp(prefix="ccmem_mig_")
        sp = Path(d) / "settings.json"
        sp.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command",
                "command": "/usr/local/bin/python3 /x/engram/engram/hooks/session_start.py"}]}],
            "PostToolUse": [{"hooks": [{"type": "command",
                "command": "node /y/graphify.js"}]}],
        }}))
        install.install_hooks(sp, "claude")
        s = json.loads(sp.read_text())
        cmds = [h["command"] for ev in s["hooks"].values()
                for g in ev for h in g["hooks"]]
        self.assertFalse(any("engram/hooks" in c for c in cmds))   # legacy gone
        self.assertTrue(any("foldcrumbs/hooks" in c for c in cmds))  # ours added
        self.assertTrue(any("graphify" in c for c in cmds))          # foreign kept

    def test_uninstall_removes_legacy_too(self):
        d = tempfile.mkdtemp(prefix="ccmem_mig_")
        sp = Path(d) / "settings.json"
        sp.write_text(json.dumps({"hooks": {
            "SessionEnd": [{"hooks": [{"type": "command",
                "command": "python3 /x/engram/engram/hooks/session_end.py"}]}],
        }}))
        install.uninstall_hooks(sp)
        s = json.loads(sp.read_text())
        cmds = [h["command"] for ev in s.get("hooks", {}).values()
                for g in ev for h in g["hooks"]]
        self.assertFalse(any("engram" in c for c in cmds))

    def test_foldcrumbs_dir_env_is_primary(self):
        d = tempfile.mkdtemp(prefix="ccmem_fc_")
        os.environ["FOLDCRUMBS_DIR"] = d
        try:
            import importlib
            from foldcrumbs import config as _c
            importlib.reload(_c)
            self.assertEqual(str(_c.memory_dir()), d)
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
            import importlib
            from foldcrumbs import config as _c
            importlib.reload(_c)


class _FederationEnv(unittest.TestCase):
    """Isolated registry + fake home, so no test touches the real ~/.claude."""

    def setUp(self):
        import importlib
        from foldcrumbs import config as _c, federation as _f
        self.config, self.federation = _c, _f
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

    def tearDown(self):
        import importlib
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(self.config)

    def _root(self, name):
        return self._home / name


class TestFederationRegistry(_FederationEnv):
    """Root registry: stable ids, one shard per root, no shared manifest."""

    def test_register_creates_marker_and_shard(self):
        ref = self.federation.register(self._root(".claude"))
        self.assertIsNotNone(ref)
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())
        self.assertTrue((self._state / "roots" / f"{ref.id}.json").is_file())

    def test_register_is_idempotent_and_keeps_id(self):
        a = self.federation.register(self._root(".claude"))
        b = self.federation.register(self._root(".claude"))
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.registered_at, b.registered_at)
        self.assertEqual(len(list((self._state / "roots").glob("*.json"))), 1)

    def test_id_survives_a_move(self):
        # The id lives in the root, not in a hash of its path: renaming the
        # config dir must not mint a second identity for the same store.
        old = self._root(".claude-work")
        ref = self.federation.register(old)
        new = self._root(".claude-renamed")
        old.rename(new)
        moved = self.federation.register(new)
        self.assertEqual(ref.id, moved.id)
        self.assertEqual(moved.path, new)

    def test_one_shard_per_root_no_shared_manifest(self):
        for name in (".claude", ".claude-work", ".claude-peo"):
            self.federation.register(self._root(name))
        shards = sorted(p.name for p in (self._state / "roots").glob("*.json"))
        self.assertEqual(len(shards), 3)
        labels = {r.label for r in self.federation.iter_roots()}
        self.assertEqual(labels, {"claude", "claude-work", "claude-peo"})

    def test_current_root_sorts_first(self):
        for name in (".claude-work", ".claude", ".claude-peo"):
            self.federation.register(self._root(name))
        roots = self.federation.iter_roots()
        self.assertTrue(roots[0].is_current())
        self.assertEqual(roots[0].label, "claude")

    def test_memory_dir_derives_per_root(self):
        ref = self.federation.register(self._root(".claude-work"))
        got = ref.memory_dir("/tmp/proj")
        self.assertEqual(
            got,
            self._root(".claude-work") / "projects"
            / self.config.encode_cwd("/tmp/proj") / "memory",
        )

    def test_explicit_dir_root_ignores_cwd(self):
        pinned = self._home / "pinned-memory"
        ref = self.federation.register(pinned, mode="explicit")
        self.assertEqual(ref.memory_dir("/tmp/a"), pinned)
        self.assertEqual(ref.memory_dir("/tmp/b"), pinned)

    def test_unregister_hides_root_but_keeps_store(self):
        ref = self.federation.register(self._root(".claude-peo"))
        (ref.path / "keep.txt").write_text("x", encoding="utf-8")
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertIsNone(self.federation.get_root(ref.id))
        self.assertTrue((ref.path / "keep.txt").is_file())

    def test_unavailable_root_is_reported_not_dropped(self):
        # A root we cannot read must stay visible-but-stale, never silently
        # vanish: disappearing entries read as "that memory was deleted".
        ref = self.federation.register(self._root(".claude-work"))
        import shutil
        shutil.rmtree(ref.path)
        again = [r for r in self.federation.iter_roots() if r.id == ref.id]
        self.assertEqual(len(again), 1)
        self.assertFalse(again[0].available())

    def test_empty_root_is_available(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.assertTrue(ref.available())

    def test_corrupt_shard_is_skipped_not_fatal(self):
        self.federation.register(self._root(".claude"))
        (self._state / "roots" / "broken.json").write_text("{not json",
                                                           encoding="utf-8")
        self.assertEqual(len(self.federation.iter_roots()), 1)

    def test_concurrent_marker_creation_agrees_on_one_id(self):
        # Read-then-write would let both processes mint an id and register the
        # same root twice. Creation goes through an atomic link, so the loser
        # adopts the winner's id.
        import threading
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        ids, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            ids.append(self.federation.ensure_marker(root)["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 1)

    def test_marker_id_with_traversal_is_rejected(self):
        # An id becomes a filename under roots/; a hand-edited marker holding
        # "../.." must never let a write escape the registry directory.
        root = self._root(".claude")
        root.mkdir(parents=True)
        (root / self.federation.ROOT_MARKER).write_text(
            json.dumps({"id": "../../escape"}), encoding="utf-8")
        self.assertIsNone(self.federation.read_marker(root))
        self.assertFalse(self.federation.valid_id("../../escape"))
        self.assertIsNone(self.federation.shard_path("../../escape"))
        ref = self.federation.register(root)
        self.assertTrue(self.federation.valid_id(ref.id))
        self.assertEqual((self._state / "roots" / f"{ref.id}.json").parent,
                         self._state / "roots")

    def test_relative_path_is_stored_absolute(self):
        cwd = os.getcwd()
        os.chdir(self._home)
        try:
            ref = self.federation.register(Path("./.claude-work"))
        finally:
            os.chdir(cwd)
        self.assertTrue(ref.path.is_absolute())
        self.assertNotIn("..", str(ref.path))

    def test_named_path_is_config_mode_even_under_explicit_dir(self):
        # `roots add ~/.claude-work` while FOLDCRUMBS_DIR is set must not tag
        # that config root as a pinned single-store root.
        os.environ["FOLDCRUMBS_DIR"] = str(self._home / "pinned")
        try:
            ref = self.federation.register(self._root(".claude-work"))
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        self.assertEqual(ref.mode, "config")

    def test_mode_collision_is_refused(self):
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        with self.assertRaises(self.federation.FederationConflict):
            self.federation.register(root, mode="explicit")

    def test_clone_of_live_root_gets_a_new_id(self):
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        cloned = self.federation.register(clone)
        self.assertNotEqual(cloned.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)
        self.assertEqual(len(self.federation.iter_roots()), 2)

    def test_unregister_current_root_stays_removed(self):
        ref = self.federation.register(self._root(".claude"))
        self.assertTrue(ref.is_current())
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertEqual(
            [r for r in self.federation.iter_roots() if r.id == ref.id], [])
        # The marker survives: leaving the shared view must not cost the root
        # the identity it would need to rejoin with its history intact.
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())

    def test_unregister_survives_the_repair_path(self):
        # Without a tombstone the removed instance's own next command would
        # quietly put the shard back.
        ref = self.federation.register(self._root(".claude"))
        self.federation.unregister(ref.id)
        self.assertIsNone(self.federation.ensure_registered())
        self.assertEqual(
            [r for r in self.federation.iter_roots() if r.id == ref.id], [])

    def test_explicit_add_revokes_a_removal(self):
        ref = self.federation.register(self._root(".claude"))
        self.federation.unregister(ref.id)
        again = self.federation.register(self._root(".claude"))
        self.assertEqual(again.id, ref.id)
        self.assertFalse(self.federation.is_tombstoned(ref.id))
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_unregister_foreign_root_keeps_its_marker(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())
        self.assertTrue(self.federation.is_tombstoned(ref.id))

    def test_symlink_alias_is_not_a_clone(self):
        # Paths are stored unresolved on purpose, so an alias reaches one root
        # under two names. Re-iding the original here would be destructive.
        ref = self.federation.register(self._root(".claude-work"))
        alias = self._root(".claude-alias")
        os.symlink(ref.path, alias)
        same = self.federation.register(alias)
        self.assertEqual(same.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)

    def test_marker_publish_failure_registers_nothing(self):
        # A phantom id — returned but never written — would be minted again on
        # the next run, giving one root two shards.
        root = self._root(".claude")
        root.mkdir(parents=True)
        real_link = os.link

        def no_links(src, dst, *a, **kw):
            raise OSError(45, "Operation not supported")

        os.link = no_links
        try:
            self.assertIsNone(self.federation.ensure_marker(root))
            self.assertIsNone(self.federation.register(root))
        finally:
            os.link = real_link
        self.assertFalse((self._state / "roots").exists()
                         and any((self._state / "roots").glob("*.json")))

    def test_marker_creation_is_create_once_under_contention(self):
        # Contenders no longer meet inside os.link: marker creation now runs
        # under the root's lock, which serialises them on purpose — a barrier
        # forcing them into the link window together can never trip. What must
        # still hold is the outcome: one marker, one id, adopted by everyone.
        # The link-based create-once underneath is still the guarantee where
        # the lock cannot be had, and
        # test_marker_publish_failure_registers_nothing covers that path.
        import threading
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        ids, lock = [], threading.Lock()

        def worker():
            got = self.federation.ensure_marker(root)
            with lock:
                ids.append(got["id"] if got else None)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(ids[0], self.federation.read_marker(root))

    def test_publish_is_create_once_when_no_lock_serialises_it(self):
        # Contention has to be exercised one layer below ensure_marker, where
        # the create-once guarantee actually lives — and where it still has to
        # hold on a filesystem that cannot lock at all. Here the barrier can
        # trip, because _publish_marker takes no lock of its own.
        import threading
        root = self._root(".claude-peo")
        root.mkdir(parents=True)
        real_link, n = os.link, 6
        barrier = threading.Barrier(n)
        outcomes, lock = [], threading.Lock()

        def racing_link(src, dst, *a, **kw):
            barrier.wait()
            try:
                real_link(src, dst, *a, **kw)
            except FileExistsError:
                with lock:
                    outcomes.append("lost")
                raise
            with lock:
                outcomes.append("won")

        got = []
        os.link = racing_link
        try:
            def worker():
                payload = self.federation._marker_payload(root, "config")
                published = self.federation._publish_marker(
                    root, payload, replace=False, exclusive=True)
                with lock:
                    got.append(published["id"] if published else None)

            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
        finally:
            os.link = real_link
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), n - 1)
        self.assertEqual(len(set(got)), 1)   # the losers adopted the winner

    def test_shard_filename_mismatch_is_skipped(self):
        ref = self.federation.register(self._root(".claude-work"))
        stray = self._state / "roots" / "0123456789abcdef.json"
        stray.write_text(json.dumps(ref.to_dict()), encoding="utf-8")
        ids = [r.id for r in self.federation.iter_roots()]
        self.assertEqual(ids.count(ref.id), 1)
        self.assertNotIn("0123456789abcdef", ids)

    def test_ensure_registered_repairs_missing_shard(self):
        ref = self.federation.register(self._root(".claude"))
        (self._state / "roots" / f"{ref.id}.json").unlink()
        self.assertIsNotNone(self.federation.ensure_registered())
        self.assertTrue((self._state / "roots" / f"{ref.id}.json").is_file())

    def test_ensure_registered_never_opts_in_on_its_own(self):
        # No marker means the user never joined (or explicitly left): a stray
        # CLI call must not resurrect the root.
        self._root(".claude").mkdir(parents=True)
        self.assertIsNone(self.federation.ensure_registered())
        self.assertEqual(list((self._state / "roots").glob("*.json")), [])
        self.assertEqual(self.federation.iter_roots(), [])

    def test_clone_of_a_removed_root_gets_a_new_id(self):
        # Removal deletes the shard, but the original's identity is still live
        # on disk: the tombstone has to carry the path or the copy would claim
        # the original's id.
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        cloned = self.federation.register(clone)
        self.assertNotEqual(cloned.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)

    def test_stale_shard_left_by_a_failed_removal_stays_hidden(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        # Simulate the tombstone-written / shard-unlink-failed window.
        (self._state / "roots" / f"{ref.id}.json").write_text(
            json.dumps(ref.to_dict()), encoding="utf-8")
        self.assertNotIn(ref.id, [r.id for r in self.federation.iter_roots()])
        self.assertIsNone(self.federation.get_root(ref.id))

    def test_unregister_fails_loudly_when_it_cannot_record_intent(self):
        ref = self.federation.register(self._root(".claude-work"))
        real = self.federation._write_json

        def boom(target, payload):
            if target.name.endswith(".removed"):
                raise OSError("read-only registry")
            return real(target, payload)

        self.federation._write_json = boom
        try:
            self.assertFalse(self.federation.unregister(ref.id))
        finally:
            self.federation._write_json = real
        # Still registered: a removal that cannot be recorded is not a removal.
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_unresolvable_identity_refuses_registration(self):
        # Both answers are destructive here: "clone" rewrites a live root's
        # marker, "not a clone" overwrites the original's shard with the wrong
        # path. So neither is taken.
        ref = self.federation.register(self._root(".claude-work"))
        other = self._root(".claude-elsewhere")
        other.mkdir(parents=True)
        (other / self.federation.ROOT_MARKER).write_text(
            (ref.path / self.federation.ROOT_MARKER).read_text(), encoding="utf-8")
        real = os.path.samefile

        def boom(a, b):
            raise OSError("cannot stat")

        os.path.samefile = boom
        try:
            self.assertIsNone(self.federation._detect_clone(ref.id, other))
            self.assertIsNone(self.federation.register(other))
        finally:
            os.path.samefile = real
        # The original's shard still points at the original.
        self.assertEqual(self.federation.get_root(ref.id).path, ref.path)

    def test_unreadable_tombstone_fails_closed(self):
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        (self._state / "roots" / f"{ref.id}.removed").write_text(
            "{ truncated", encoding="utf-8")
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        # Cannot tell a copy from a rejoin: refuse rather than hand two live
        # roots the same id.
        self.assertIsNone(self.federation._detect_clone(ref.id, clone))
        self.assertIsNone(self.federation.register(clone))

    def test_mutations_refuse_when_the_registry_cannot_be_locked(self):
        ref = self.federation.register(self._root(".claude"))
        real = self.federation._registry_lock

        @contextlib.contextmanager
        def unlockable():
            yield False

        self.federation._registry_lock = unlockable
        try:
            self.assertIsNone(self.federation.register(self._root(".claude-work")))
            self.assertFalse(self.federation.unregister(ref.id))
            self.assertIsNone(self.federation.ensure_registered())
        finally:
            self.federation._registry_lock = real
        # Nothing moved: the root is still registered, and no new one appeared.
        self.assertEqual([r.id for r in self.federation.iter_roots()], [ref.id])

    def test_mkdir_lock_excludes_and_never_steals(self):
        lockdir = self._state / "roots" / "probe.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05  # don't sit out the real one
        try:
            with self.federation._mkdir_lock(lockdir) as first:
                self.assertTrue(first)
                with self.federation._mkdir_lock(lockdir) as second:
                    self.assertFalse(second)   # already held
            self.assertFalse(lockdir.exists())  # released

            # An old lock is NOT stolen: age cannot tell a dead holder from a
            # slow one, and two live holders lose a tombstone or a shard.
            lockdir.mkdir()
            (lockdir / "owner-someone-else").write_text("x", encoding="utf-8")
            old = time.time() - 86400
            os.utime(lockdir, (old, old))
            with self.federation._mkdir_lock(lockdir) as stolen:
                self.assertFalse(stolen)
            self.assertTrue(lockdir.exists())  # and left alone
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait

    def test_lock_never_displaces_an_existing_holder(self):
        # An empty lock dir means a holder whose marker was deleted by hand,
        # still inside its critical section. It must not be taken over — which
        # is exactly what a rename-based publication would do.
        lockdir = self._state / "roots" / "probe3.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        lockdir.mkdir()
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait
        self.assertTrue(lockdir.exists())

    def test_lock_backs_off_when_another_marker_appears(self):
        # The window between mkdir and writing the marker: if a manual removal
        # let someone else in, the late writer must not believe it holds it.
        lockdir = self._state / "roots" / "probe4.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text

        def intruding_write(self_path, *a, **kw):
            out = real_write(self_path, *a, **kw)
            if self_path.name.startswith("owner-"):
                other = self_path.parent / "owner-someoneelse"
                if not other.exists():
                    real_write(other, "x", encoding="utf-8")
            return out

        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        Path.write_text = intruding_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)   # saw the intruder, backed off
        finally:
            Path.write_text = real_write
            self.federation._LOCK_WAIT_SECONDS = wait
        # It withdrew its own marker and left the intruder's alone.
        self.assertEqual([p.name for p in lockdir.glob("owner-*")],
                         ["owner-someoneelse"])

    def test_transient_failure_does_not_strand_the_lock(self):
        # Stale locks are never broken, so anything left behind by a failed
        # acquisition would make the registry permanently unmutatable.
        lockdir = self._state / "roots" / "probe5.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text

        def failing_write(self_path, *a, **kw):
            if self_path.name.startswith("owner-"):
                raise OSError("no space left on device")
            return real_write(self_path, *a, **kw)

        Path.write_text = failing_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
        finally:
            Path.write_text = real_write
        self.assertFalse(lockdir.exists())
        # And the next attempt still works.
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)

    def test_mutual_backoff_does_not_strand_the_lock(self):
        # Both contenders withdrawing must not leave an empty directory that
        # nobody can ever take.
        lockdir = self._state / "roots" / "probe6.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text
        injected = []

        def intruding_write(self_path, *a, **kw):
            out = real_write(self_path, *a, **kw)
            if self_path.name.startswith("owner-") and not injected:
                injected.append(True)
                real_write(self_path.parent / "owner-other", "x", encoding="utf-8")
            return out

        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        Path.write_text = intruding_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
            # The intruder's marker is what keeps the dir alive here; clear it
            # the way the other contender's withdrawal would.
            (lockdir / "owner-other").unlink()
            lockdir.rmdir()
        finally:
            Path.write_text = real_write
            self.federation._LOCK_WAIT_SECONDS = wait
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)

    def test_flock_failure_never_falls_back_to_a_second_mechanism(self):
        # Two lock domains exclude nobody: if flock is available but fails,
        # the answer is "not locked", not "try another lock".
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        real_flock = self.federation.fcntl.flock

        def refuse(*a, **kw):
            raise OSError("flock unavailable")

        self.federation.fcntl.flock = refuse
        try:
            with self.federation._registry_lock() as locked:
                self.assertFalse(locked)
            self.assertFalse((self._state / "roots" / ".lock.d").exists())
        finally:
            self.federation.fcntl.flock = real_flock

    def test_mkdir_lock_does_not_release_someone_elses_lock(self):
        # A holder whose lock was cleared by hand and retaken must not remove
        # the new holder's directory. Ownership lives in the *filename*, so
        # release can never unlink a marker that isn't this holder's own —
        # there is no read-then-unlink window to lose.
        lockdir = self._state / "roots" / "probe2.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)
            for stale in lockdir.glob("owner-*"):
                stale.unlink()                       # cleared by hand
            (lockdir / "owner-newholder").write_text("x", encoding="utf-8")
        self.assertTrue(lockdir.exists())
        self.assertTrue((lockdir / "owner-newholder").is_file())

    def test_removing_a_root_allows_its_mode_to_change(self):
        # The conflict message says to remove and re-add. Removal keeps the
        # marker, so re-adding used to read the old mode and raise the same
        # conflict — the documented recovery could never work.
        root = self._root(".claude-work")
        first = self.federation.register(root, mode="config")
        with self.assertRaises(self.federation.FederationConflict):
            self.federation.register(root, mode="explicit")
        self.assertTrue(self.federation.unregister(first.id))
        again = self.federation.register(root, mode="explicit")
        self.assertEqual(again.mode, "explicit")
        self.assertEqual(again.id, first.id)      # identity survives
        self.assertEqual(self.federation.read_marker_data(root)["mode"], "explicit")

    def test_a_wiped_registry_does_not_license_a_mode_change(self):
        # A missing shard is repairable, not a removal: ensure_registered()
        # puts it back. Treating absence as consent would silently re-address
        # every memory in a root nobody asked to remove.
        root = self._root(".claude-work")
        ref = self.federation.register(root, mode="config")
        (self._state / "roots" / f"{ref.id}.json").unlink()   # registry wiped
        with self.assertRaises(self.federation.FederationConflict):
            self.federation.register(root, mode="explicit")
        self.assertEqual(self.federation.read_marker_data(root)["mode"], "config")
        self.federation.register(root, mode="config")         # repair first
        self.federation.unregister(ref.id)
        self.assertEqual(
            self.federation.register(root, mode="explicit").mode, "explicit")

    def test_an_aliased_state_dir_is_not_reported_as_a_split(self):
        # Registration treats two spellings of one state directory as one
        # registry and keeps each marker's original wording. Status compared
        # text, so it announced a split federation with "invisible" roots in a
        # setup that works — and told the user to go fix a consistent config.
        import importlib
        root = self._root(".claude")
        self.federation.register(root)
        self.assertIsNone(self.federation.state_dir_conflict())
        alias = self._home / "state-by-another-name"
        os.symlink(self._state, alias)
        os.environ["ENGRAM_STATE_DIR"] = str(alias)
        importlib.reload(self.config)
        try:
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
            said = self.federation.state_dir_conflict()
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
        self.assertIsNone(
            said, f"reported a split for one directory spelled twice: {said}")

    def test_a_genuinely_different_state_dir_is_still_reported(self):
        # The other half of the same rule: identity must not silence a real
        # split, only a spelling difference.
        import importlib
        root = self._root(".claude")
        self.federation.register(root)
        elsewhere = self._home / "really-elsewhere"
        elsewhere.mkdir(parents=True, exist_ok=True)
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        try:
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
            said = self.federation.state_dir_conflict()
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
        self.assertIsNotNone(said, "a real split went unreported")

    def test_a_registry_we_cannot_reach_is_not_reported_as_agreement(self):
        # "Could not tell" is not "fine". Judging a split only on a *provable*
        # difference silenced every real one behind an unreachable mount —
        # exactly the case where the user sees memories missing and needs the
        # status report to say why.
        import importlib
        root = self._root(".claude")
        self.federation.register(root)
        elsewhere = self._home / "unreachable-state"
        elsewhere.mkdir(parents=True, exist_ok=True)
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        real_stat = os.stat

        def opaque(path, *a, **k):
            if str(path) == str(self._state):
                raise OSError(errno.EIO, "I/O error")
            return real_stat(path, *a, **k)

        os.stat = opaque
        try:
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
            said = self.federation.state_dir_conflict()
        finally:
            os.stat = real_stat
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
        self.assertIsNotNone(
            said, "a registry that could not be reached was reported as "
                  "agreeing with this one")
        self.assertIn(str(self._state), said)
        # Not shown to be a different registry, so not told to go change the
        # configuration over it: that instruction belongs to a proven split.
        self.assertNotIn("set FOLDCRUMBS_STATE_DIR consistently", said,
                         "gave advice the check never established")

    def test_a_confirmed_split_does_not_bury_the_unreachable_ones(self):
        # Reporting only what was proven hid the rest behind it. The proven
        # split is the one the user can already see; the unreachable registry
        # is the one they cannot, so burying it loses the useful half.
        a = self.federation.register(self._root(".claude"))
        b = self.federation.register(self._root(".claude-work"))
        other = self._home / "other-state"
        opaque = self._home / "opaque-state"
        for d in (other, opaque):
            d.mkdir(parents=True, exist_ok=True)
        for ref, where in ((a, other), (b, opaque)):
            shard = self._state / "roots" / f"{ref.id}.json"
            data = json.loads(shard.read_text())
            data["state_dir"] = str(where)
            shard.write_text(json.dumps(data), encoding="utf-8")
        real_stat = os.stat

        def unreachable(path, *args, **kw):
            if str(path) == str(opaque):
                raise OSError(errno.EIO, "I/O error")
            return real_stat(path, *args, **kw)

        os.stat = unreachable
        try:
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
            said = self.federation.state_dir_conflict()
        finally:
            os.stat = real_stat
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
        self.assertIsNotNone(said, "neither was reported")
        self.assertIn(str(other), said, "the confirmed split went missing")
        self.assertIn(str(opaque), said,
                      "the unreachable registry was buried by the confirmed one")

    def test_moving_the_state_dir_clears_the_split_warning(self):
        # Re-registering wrote the shard into the new registry but left the
        # marker naming the old one, so the warning stuck forever.
        import importlib
        root = self._root(".claude")
        self.federation.register(root)
        moved = self._home / "moved-state"
        os.environ["ENGRAM_STATE_DIR"] = str(moved)
        importlib.reload(self.config)
        try:
            self.assertIsNotNone(self.federation.state_dir_conflict())
            self.federation.register(root)          # re-register where we are
            self.assertIsNone(self.federation.state_dir_conflict())
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)

    def test_a_failed_relocation_is_not_reported_as_success(self):
        # Publishing the shard while the marker still names the old registry
        # leaves state_dir_conflict() warning forever — the exact fault the
        # refresh exists to clear — with register() claiming it worked.
        import contextlib as _c
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere"
        marker.write_text(json.dumps(data), encoding="utf-8")
        real = self.federation.file_lock

        @_c.contextmanager
        def no_marker_lock(path, allow_unsupported=False):
            if path == self.federation.marker_lock_path(root):
                yield False
                return
            with real(path, allow_unsupported=allow_unsupported) as held:
                yield held

        self.federation.file_lock = no_marker_lock
        try:
            self.assertIsNone(self.federation.register(root))
        finally:
            self.federation.file_lock = real
        self.assertEqual(json.loads(marker.read_text())["registry"], "/elsewhere")

    def test_a_refused_mode_change_mutates_nothing(self):
        # A registration that is going to be refused must not have changed
        # anything on the way there — the marker least of all, since that is
        # what tells every other instance where this root's memory lives.
        root = self._root(".claude-work")
        ref = self.federation.register(root, mode="config")
        marker = root / self.federation.ROOT_MARKER
        before = marker.read_text()
        shard_before = (self._state / "roots" / f"{ref.id}.json").read_text()
        with self.assertRaises(self.federation.FederationConflict):
            self.federation.register(root, mode="explicit")
        self.assertEqual(marker.read_text(), before, "the marker was rewritten")
        self.assertEqual((self._state / "roots" / f"{ref.id}.json").read_text(),
                         shard_before, "the shard was rewritten")

    def test_a_relocation_that_lands_elsewhere_is_refused(self):
        # Another process can publish a marker naming a third registry while
        # we replace ours. Publishing the shard then would leave the two
        # disagreeing again — and report a move that did not happen.
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere"
        marker.write_text(json.dumps(data), encoding="utf-8")
        real = self.federation._publish_marker

        def lands_somewhere_else(path, payload, *, replace, exclusive):
            real(path, dict(payload, registry="/a-third-place"),
                 replace=True, exclusive=exclusive)
            return self.federation.read_marker_data(path)

        self.federation._publish_marker = lands_somewhere_else
        try:
            self.assertIsNone(self.federation.register(root))
        finally:
            self.federation._publish_marker = real
        self.assertIsNotNone(self.federation.state_dir_conflict())

    def test_registration_adopts_the_id_and_mode_that_landed(self):
        # replace_marker returns whichever marker won, which need not be the
        # one we sent. Keeping our own id addressed the shard under an
        # identity nothing else uses; keeping our own mode pointed readers at
        # a memory directory this root does not use.
        root = self._root(".claude")
        self.federation.register(root, mode="config")
        real = self.federation._publish_marker
        landed = "abcdef0123456789"

        def someone_else_won(path, payload, *, replace, exclusive):
            real(path, dict(payload, id=landed, mode="explicit"),
                 replace=True, exclusive=exclusive)
            return self.federation.read_marker_data(path)

        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere"          # force the refresh branch
        marker.write_text(json.dumps(data), encoding="utf-8")
        self.federation._publish_marker = someone_else_won
        try:
            ref = self.federation.register(root)
        finally:
            self.federation._publish_marker = real
        self.assertEqual(ref.id, landed)
        self.assertEqual(ref.mode, "explicit")
        shard = json.loads((self._state / "roots" / f"{landed}.json").read_text())
        self.assertEqual(shard["mode"], "explicit")

    def test_marker_replacement_is_locked_on_the_root_not_the_registry(self):
        # Two processes pointed at different state dirs — which is what a
        # relocation is — take different registry locks and exclude nobody
        # while replacing the same marker. The lock has to live with the root.
        import contextlib as _c
        root = self._root(".claude")
        self.federation.register(root)
        taken = []
        real = self.federation.file_lock

        @_c.contextmanager
        def record(path, allow_unsupported=False):
            taken.append(Path(path))
            with real(path, allow_unsupported=allow_unsupported) as held:
                yield held

        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere"
        marker.write_text(json.dumps(data), encoding="utf-8")
        self.federation.file_lock = record
        try:
            self.federation.register(root)
        finally:
            self.federation.file_lock = real
        self.assertIn(self.federation.marker_lock_path(root), taken)
        self.assertEqual(self.federation.marker_lock_path(root).parent, root)

    def test_the_marker_cannot_change_between_marker_and_shard(self):
        # Marker and shard were two steps under two different locks, so a
        # concurrent replacement in between left the shard published under an
        # identity that had already been superseded. The registration now
        # holds the root's lock across both.
        import contextlib as _c
        import threading as _t
        root = self._root(".claude")
        self.federation.register(root)
        real = self.federation.file_lock
        intruder_done = _t.Event()

        @_c.contextmanager
        def watch(path, allow_unsupported=False):
            with real(path, allow_unsupported=allow_unsupported) as held:
                if held and path == self.federation.marker_lock_path(root):
                    # Whatever tries to replace the marker now must wait for
                    # this registration to finish, so it cannot land between
                    # the marker write and the shard write.
                    def intrude():
                        with real(path) as got:
                            intruder_done.set() if got else None
                    t = _t.Thread(target=intrude, daemon=True)
                    t.start()
                    self.assertFalse(intruder_done.wait(0.3),
                                     "the marker lock was not held throughout")
                yield held

        self.federation.file_lock = watch
        try:
            ref = self.federation.register(root)
        finally:
            self.federation.file_lock = real
        self.assertIsNotNone(ref)
        self.assertTrue(intruder_done.wait(5))   # released once we are done

    def test_every_marker_replacement_revalidates_the_requested_mode(self):
        # Three branches replace the marker, and each can get back a winner
        # that is not the payload it sent. Two of them learned to re-check the
        # requested mode one at a time; the clone branch had not, so a caller
        # asking for 'config' could be handed a successful 'explicit'
        # registration addressing a different layout.
        import shutil
        root = self._root(".claude-work")
        first = self.federation.register(root, mode="config")
        clone = self._root(".claude-copy")
        shutil.copytree(root, clone)
        real = self.federation._publish_marker

        def wins_with_another_mode(path, payload, *, replace, exclusive):
            real(path, dict(payload, mode="explicit"),
                 replace=True, exclusive=exclusive)
            return self.federation.read_marker_data(path)

        self.federation._publish_marker = wins_with_another_mode
        try:
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(clone, mode="config")
        finally:
            self.federation._publish_marker = real
        self.assertEqual(first.mode, "config")

    def test_a_relocation_can_carry_an_allowed_mode_change(self):
        # The two happen together when a removed root is re-added from an
        # instance whose state dir has moved. Validating the mode during the
        # relocation rejected that valid combination — after rewriting the
        # marker, so the registration failed halfway through.
        import importlib
        root = self._root(".claude-work")
        first = self.federation.register(root, mode="config")
        self.federation.unregister(first.id)          # removal records intent
        moved = self._home / "moved-state-3"
        os.environ["ENGRAM_STATE_DIR"] = str(moved)
        importlib.reload(self.config)
        try:
            ref = self.federation.register(root, mode="explicit")
            self.assertIsNotNone(ref, "a valid relocation + mode change failed")
            self.assertEqual(ref.mode, "explicit")
            self.assertEqual(ref.id, first.id)
            marker = json.loads((root / self.federation.ROOT_MARKER).read_text())
            self.assertEqual(marker["registry"], str(self.config.STATE_DIR))
            self.assertTrue((moved / "roots" / f"{ref.id}.json").is_file())
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)

    def test_another_registry_cannot_re_address_a_live_root(self):
        # The marker lives inside the root, so it is shared by every registry.
        # Treating "not registered here" as licence to change the mode would
        # re-address the memories of an instance elsewhere that still has the
        # root live and knows nothing about it.
        import importlib
        root = self._root(".claude-work")
        home = self.federation.register(root, mode="config")   # live here
        elsewhere = self._home / "other-registry"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        try:
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(root, mode="explicit")
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
        self.assertEqual(
            self.federation.read_marker_data(root)["mode"], "config")
        self.assertEqual(self.federation.get_root(home.id).mode, "config")

    def test_a_refusal_leaves_a_retry_identical(self):
        # The refusal comes before any mutation, so a second attempt sees the
        # same state and gives the same answer — rather than a half-relocated
        # root that behaves differently the next time round.
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        marker = root / self.federation.ROOT_MARKER
        before = marker.read_text()
        for _ in range(3):
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(root, mode="explicit")
            self.assertEqual(marker.read_text(), before)

    def test_a_malformed_registry_field_refuses_instead_of_crashing(self):
        # A hand-edited marker can hold anything there, and callers build a
        # path from it. Fail closed: refuse the change, do not raise out of a
        # registration halfway through.
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        marker = root / self.federation.ROOT_MARKER
        for bogus in ([1, 2], 42, {"a": 1}, None, ""):
            data = json.loads(marker.read_text())
            data["registry"] = bogus
            marker.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(root, mode="explicit")
            self.assertIsNone(self.federation._removal_recorded("0" * 16, bogus))
            # Malformed must not collapse into "absent": absent means this
            # registry owns the root, which is exactly where a tombstone
            # authorising the change would be looked for.
            self.assertIsNone(
                self.federation._home_registry({"registry": bogus}))
        # A relative path is not identifiable either: consumers would resolve
        # it against the caller's cwd, so consent would depend on where the
        # command ran — and a project-local tombstone could authorise a change.
        for relative in ("roots", "./state", "../elsewhere", "a/b"):
            self.assertIsNone(
                self.federation._home_registry({"registry": relative}),
                relative)
        self.assertEqual(
            self.federation._home_registry(
                {"registry": str(self.config.STATE_DIR)}),
            str(self.config.STATE_DIR))
        # And a well-formed one still works.
        data = json.loads(marker.read_text())
        data["registry"] = str(self.config.STATE_DIR)
        marker.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNotNone(self.federation.register(root))
        # A marker predating the field belongs to whoever reads it.
        self.assertEqual(self.federation._home_registry({}),
                         str(self.config.STATE_DIR))

    def test_status_reports_a_malformed_registry_instead_of_crashing(self):
        # The value goes into a set and a join: a list is unhashable, a dict
        # untypeable. A status command must say what is wrong, not raise.
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        for bogus in ([1, 2], {"a": 1}, 42, ""):
            data = json.loads(marker.read_text())
            data["registry"] = bogus
            marker.write_text(json.dumps(data), encoding="utf-8")
            msg = self.federation.state_dir_conflict()
            self.assertIsNotNone(msg)
            self.assertIn("unreadable registry field", msg)

    def test_install_repairs_every_malformed_registry_value(self):
        # The advice printed by status is "re-run install". A falsy malformed
        # value read as absent to a plain truth test, so the repair never ran
        # and the advice was a promise the code could not keep.
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        for bogus in ("", 0, {}, [], [1, 2], 42):
            data = json.loads(marker.read_text())
            data["registry"] = bogus
            marker.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNotNone(self.federation.state_dir_conflict())
            ref = self.federation.register(root)      # what install does
            self.assertIsNotNone(ref, f"register failed for {bogus!r}")
            self.assertEqual(json.loads(marker.read_text())["registry"],
                             str(self.config.STATE_DIR),
                             f"not repaired for {bogus!r}")
            self.assertIsNone(self.federation.state_dir_conflict())

    def test_an_unreachable_registry_refuses_instead_of_hanging(self):
        # The marker can name a registry on a mount that stopped answering.
        # `except OSError` does not bound a call that never returns, so the
        # registration hung instead of giving the documented refusal.
        import threading as _t
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = str(self._home / "gone")
        marker.write_text(json.dumps(data), encoding="utf-8")
        release = _t.Event()
        real_is_dir = Path.is_dir

        def hangs(self_path):
            if "gone" in str(self_path):
                release.wait(30)
            return real_is_dir(self_path)

        probe = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.1
        Path.is_dir = hangs
        try:
            start = time.monotonic()
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(root, mode="explicit")
            elapsed = time.monotonic() - start
        finally:
            Path.is_dir = real_is_dir
            self.federation._REGISTRY_PROBE_TIMEOUT = probe
            release.set()
        self.assertLess(elapsed, 3, "the registration waited on a dead mount")

    def test_a_filesystem_without_flock_can_still_take_a_marker(self):
        # The marker lock lives inside the root, which may sit on a share that
        # honours hard links but refuses flock. Making that lock mandatory made
        # registration impossible there — on roots that worked before the lock
        # existed. Create-once still holds, so marker work proceeds.
        import errno as _e
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        real_flock = self.federation.fcntl.flock

        def never_locks(fd, op):
            raise OSError(_e.ENOLCK, "no locks available")

        self.federation.fcntl.flock = never_locks
        try:
            # The lock itself reports the degradation rather than refusing...
            with self.federation.file_lock(
                    self.federation.marker_lock_path(root),
                    allow_unsupported=True) as held:
                self.assertTrue(held, "a lock-less filesystem was refused")
            # ...and callers that rely on create-once still get their marker.
            marker = self.federation.ensure_marker(root)
            self.assertIsNotNone(marker, "no marker on a lock-less filesystem")
            self.assertTrue((root / self.federation.ROOT_MARKER).is_file())
            # Without the opt-in, an unlockable path is still a refusal.
            with self.federation.file_lock(
                    self.federation.marker_lock_path(root)) as strict:
                self.assertFalse(strict)
        finally:
            self.federation.fcntl.flock = real_flock

    def test_without_locking_a_root_registers_but_is_not_rewritten(self):
        # Proceeding unlocked would reopen the marker/shard race the root lock
        # closed. Refusing outright would make lock-less shares unusable. So
        # only what create-once already guarantees goes ahead: a first
        # registration works, a rewrite waits for a filesystem that can lock.
        #
        # Simulated at the right seam — the *root* cannot lock, the registry
        # (on the local state dir) still can, which is the real asymmetry.
        import contextlib as _c
        fresh = self._root(".claude-fresh")
        existing = self._root(".claude-work")
        first = self.federation.register(existing, mode="config")
        self.federation.unregister(first.id)      # a mode change is now allowed
        real = self.federation.file_lock
        roots_dir = self.federation.roots_dir()

        @_c.contextmanager
        def root_cannot_lock(path, allow_unsupported=False):
            if Path(path).parent != roots_dir:      # inside a root
                yield self.federation.DEGRADED if allow_unsupported else False
                return
            with real(path, allow_unsupported=allow_unsupported) as held:
                yield held

        self.federation.file_lock = root_cannot_lock
        try:
            # Creating is safe: the atomic link gives create-once by itself.
            self.assertIsNotNone(self.federation.register(fresh),
                                 "a new root could not register")
            # Rewriting is not, so it declines rather than racing.
            self.assertIsNone(
                self.federation.register(existing, mode="explicit"),
                "a marker rewrite proceeded without exclusion")
        finally:
            self.federation.file_lock = real
        self.assertEqual(
            self.federation.read_marker_data(existing)["mode"], "config")

    def test_no_rewrite_path_can_bypass_the_exclusion_gate(self):
        # Four branches learned this rule one at a time, the fourth by being
        # missed. The gate now lives at the single point every rewrite passes
        # through, so a fifth cannot skip it — checked here directly rather
        # than by enumerating branches that may not exist yet.
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        payload = self.federation._marker_payload(root, "config")
        # Creating is allowed without exclusion: the atomic link is enough.
        created = self.federation._publish_marker(
            root, payload, replace=False, exclusive=False)
        self.assertIsNotNone(created)
        # Replacing is not, whoever asks.
        self.assertIsNone(self.federation._publish_marker(
            root, self.federation._marker_payload(root, "explicit"),
            replace=True, exclusive=False))
        self.assertEqual(
            self.federation.read_marker_data(root)["mode"], "config")
        # And still is with exclusion.
        self.assertIsNotNone(self.federation._publish_marker(
            root, self.federation._marker_payload(root, "explicit"),
            replace=True, exclusive=True))

    def test_the_exclusion_flag_cannot_be_omitted(self):
        # A default would have made the gate opt-in, and the value anyone
        # would leave out is the permissive one. Omitting it is a TypeError at
        # the call, caught by any test that exercises the path — not a silent
        # bypass discovered later.
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        payload = self.federation._marker_payload(root, "config")
        with self.assertRaises(TypeError):
            self.federation._publish_marker(root, payload, replace=True)
        with self.assertRaises(TypeError):
            self.federation._replace_marker_locked(root, payload)
        with self.assertRaises(TypeError):
            self.federation._ensure_marker_locked(root, "config")
        with self.assertRaises(TypeError):
            self.federation._register_with_marker(root, "config", None, None)

    def test_relocation_and_mode_change_are_one_write(self):
        # Two replacements left an intermediate marker naming the new registry
        # with the old mode. On retry the consent tombstone was then looked for
        # in the new registry — where it never was — so the valid change stayed
        # refused for good. One write, or none.
        import importlib
        root = self._root(".claude-work")
        first = self.federation.register(root, mode="config")
        self.federation.unregister(first.id)        # consent, in this registry
        moved = self._home / "moved-state-4"
        os.environ["ENGRAM_STATE_DIR"] = str(moved)
        importlib.reload(self.config)
        writes = []
        real = self.federation._publish_marker

        def counting(path, payload, *, replace, exclusive):
            if replace:
                writes.append(json.loads(json.dumps(payload)))
            return real(path, payload, replace=replace, exclusive=exclusive)

        self.federation._publish_marker = counting
        try:
            ref = self.federation.register(root, mode="explicit")
        finally:
            self.federation._publish_marker = real
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
        self.assertIsNotNone(ref)
        self.assertEqual(len(writes), 1, f"{len(writes)} marker writes, not one")
        # The single write carries the final state, never a half-moved one.
        self.assertEqual(writes[0]["mode"], "explicit")
        self.assertEqual(writes[0]["registry"], str(moved))

    def test_a_relocation_cannot_silently_change_an_agreed_mode(self):
        # Asking for the mode a root already has is still asking. Skipping the
        # check in that case let a process that won the replacement hand back a
        # different mode, and the caller was told the registration succeeded.
        import importlib
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        moved = self._home / "moved-state-5"
        os.environ["ENGRAM_STATE_DIR"] = str(moved)
        importlib.reload(self.config)
        real = self.federation._publish_marker

        def wins_with_another_mode(path, payload, *, replace, exclusive):
            return real(path, dict(payload, mode="explicit"),
                        replace=replace, exclusive=exclusive)

        self.federation._publish_marker = wins_with_another_mode
        try:
            with self.assertRaises(self.federation.FederationConflict):
                self.federation.register(root, mode="config")
        finally:
            self.federation._publish_marker = real
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)

    def test_a_copy_registered_from_another_registry_gets_a_new_id(self):
        # Clone detection ran after the relocation and looked only at the
        # current registry: the copy's marker had already moved here, nothing
        # prior was found, and both roots kept one identity — enough for later
        # federation and supersession claims to conflate them.
        import importlib
        import shutil
        original = self._root(".claude-work")
        first = self.federation.register(original)      # lives in this registry
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        elsewhere = self._home / "other-registry-2"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        try:
            ref = self.federation.register(copy_root)
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
        self.assertIsNotNone(ref)
        self.assertNotEqual(ref.id, first.id, "the copy kept the original's id")
        self.assertEqual(self.federation.read_marker(original), first.id)

    def test_a_copy_with_an_authorised_mode_change_completes(self):
        # The clone branch writes the *recorded* mode by design, so validating
        # the requested one there rejected a legitimate change — after the new
        # id had been written. Mutating and then failing is the one outcome a
        # registration must never have.
        import shutil
        original = self._root(".claude-work")
        first = self.federation.register(original, mode="config")
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        self.federation.unregister(first.id)      # consent for the change
        ref = self.federation.register(copy_root, mode="explicit")
        self.assertIsNotNone(ref, "a copy with an authorised change failed")
        self.assertNotEqual(ref.id, first.id)     # still gets its own identity
        self.assertEqual(ref.mode, "explicit")
        self.assertEqual(
            self.federation.read_marker_data(copy_root)["mode"], "explicit")
        # The original is untouched by any of it.
        self.assertEqual(self.federation.read_marker(original), first.id)
        self.assertEqual(
            self.federation.read_marker_data(original)["mode"], "config")

    def test_a_copy_relocating_with_a_mode_change_writes_once(self):
        # Three concerns, one marker. Written in sequence, whichever ran first
        # mutated it and a failure later left the root half-changed — a new id
        # carrying an old mode, or a registry the caller was told had not
        # moved. All three now land in a single replacement, or none do.
        import importlib
        import shutil
        original = self._root(".claude-work")
        first = self.federation.register(original, mode="config")
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        self.federation.unregister(first.id)         # consent for the change
        elsewhere = self._home / "other-registry-3"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        writes = []
        real = self.federation._publish_marker

        def counting(path, payload, *, replace, exclusive):
            if replace and Path(path) == copy_root:
                writes.append(json.loads(json.dumps(payload)))
            return real(path, payload, replace=replace, exclusive=exclusive)

        self.federation._publish_marker = counting
        try:
            ref = self.federation.register(copy_root, mode="explicit")
        finally:
            self.federation._publish_marker = real
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
        self.assertIsNotNone(ref)
        self.assertEqual(len(writes), 1, f"{len(writes)} marker writes, not one")
        self.assertEqual(writes[0]["mode"], "explicit")
        self.assertEqual(writes[0]["registry"], str(elsewhere))
        self.assertNotEqual(writes[0]["id"], first.id)
        self.assertEqual(self.federation.read_marker(original), first.id)

    def test_a_failed_shard_write_is_completed_by_the_next_attempt(self):
        # The marker lands before the shard, so a failure in between leaves the
        # root moved but unlisted. Rolling the marker back would be another
        # rewrite that can fail just as easily; the design recovers forwards
        # instead — and that only holds if a retry actually converges, which is
        # what this checks rather than assumes.
        import importlib
        root = self._root(".claude-work")
        first = self.federation.register(root, mode="config")
        self.federation.unregister(first.id)
        moved = self._home / "moved-state-6"
        os.environ["ENGRAM_STATE_DIR"] = str(moved)
        importlib.reload(self.config)
        real = self.federation._write_json

        def shard_writes_fail(target, payload):
            if target.suffix == ".json" and target.parent.name == "roots":
                raise OSError("no space left on device")
            return real(target, payload)

        self.federation._write_json = shard_writes_fail
        try:
            self.assertIsNone(self.federation.register(root, mode="explicit"))
            # The marker moved; the registry has nothing.
            marker = self.federation.read_marker_data(root)
            self.assertEqual(marker["registry"], str(self.config.STATE_DIR))
            self.assertEqual(marker["mode"], "explicit")
            self.assertEqual(list((moved / "roots").glob("*.json")), [])
        finally:
            self.federation._write_json = real
        try:
            # The retry needs no consent — the mode already reads as asked —
            # and completes what was left.
            ref = self.federation.register(root, mode="explicit")
            self.assertIsNotNone(ref, "the retry could not finish the move")
            self.assertEqual(ref.mode, "explicit")
            self.assertEqual(ref.id, first.id)
            self.assertTrue((moved / "roots" / f"{ref.id}.json").is_file())
            self.assertIsNone(self.federation.state_dir_conflict())
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)

    def test_clone_detection_cannot_hang_on_a_dead_old_path(self):
        # The registry answered, but the root it names can live on a mount that
        # stopped answering. Reading its marker and comparing inodes were
        # synchronous: either could block a registration with the marker lock
        # held. Both are bounded, and an unanswered probe fails closed.
        import threading as _t
        import shutil
        original = self._root(".claude-work")
        first = self.federation.register(original)
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        release = _t.Event()
        real_read = self.federation.read_marker

        def hangs(path):
            if Path(path) == original:
                release.wait(30)
            return real_read(path)

        probe = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.1
        self.federation.read_marker = hangs
        try:
            start = time.monotonic()
            verdict = self.federation._detect_clone(
                first.id, copy_root, str(self.config.STATE_DIR))
            elapsed = time.monotonic() - start
        finally:
            self.federation.read_marker = real_read
            self.federation._REGISTRY_PROBE_TIMEOUT = probe
            release.set()
        self.assertIsNone(verdict, "an unanswered probe did not fail closed")
        self.assertLess(elapsed, 3, "clone detection waited on a dead mount")

    def test_a_copy_is_refused_while_the_original_is_mid_replacement(self):
        # Publication unlinks before it links, and this check holds the copy's
        # lock, not the original's. Reading that gap as "the original moved
        # away" let the copy keep its id — and with it, its shards and its
        # supersession claims.
        import shutil
        original = self._root(".claude-work")
        first = self.federation.register(original)
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        # The original is still there; its marker is momentarily absent.
        (original / self.federation.ROOT_MARKER).unlink()
        verdict = self.federation._detect_clone(
            first.id, copy_root, str(self.config.STATE_DIR))
        self.assertIsNone(verdict, "a gap in the original was read as a move")
        self.assertIsNone(self.federation.register(copy_root))
        # A genuinely vanished original is still a move, not an ambiguity.
        shutil.rmtree(original)
        self.assertIs(
            self.federation._detect_clone(
                first.id, copy_root, str(self.config.STATE_DIR)),
            False)

    def test_an_unanswered_directory_probe_is_not_a_move(self):
        # _bounded returns None when the probe does not answer, and a plain
        # truth test read that as "the original is gone" — the permissive
        # branch. A hung mount then handed the copy the original's identity.
        import shutil
        import threading as _t
        original = self._root(".claude-work")
        first = self.federation.register(original)
        copy_root = self._root(".claude-copy")
        shutil.copytree(original, copy_root)
        (original / self.federation.ROOT_MARKER).unlink()
        release = _t.Event()
        real_is_dir = Path.is_dir

        def hangs(self_path):
            if Path(self_path) == original:
                release.wait(30)
            return real_is_dir(self_path)

        probe = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.1
        Path.is_dir = hangs
        try:
            verdict = self.federation._detect_clone(
                first.id, copy_root, str(self.config.STATE_DIR))
        finally:
            Path.is_dir = real_is_dir
            self.federation._REGISTRY_PROBE_TIMEOUT = probe
            release.set()
        self.assertIsNone(verdict, "an unanswered probe was read as a move")

    def test_status_does_not_silently_relocate_a_root(self):
        # ensure_registered() runs on every CLI command. Letting it relocate
        # meant `foldcrumbs status` moved the root into whatever registry was
        # configured — hiding the federation it came from and switching off the
        # warning that would have explained it.
        import importlib
        root = self._root(".claude")
        ref = self.federation.register(root)
        before = json.loads((root / self.federation.ROOT_MARKER).read_text())
        elsewhere = self._home / "another-registry"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        try:
            self.assertIsNone(self.federation.ensure_registered(),
                              "a repair relocated the root")
            after = json.loads((root / self.federation.ROOT_MARKER).read_text())
            self.assertEqual(after["registry"], before["registry"])
            self.assertEqual(
                list((elsewhere / "roots").glob("*.json")), [],
                "a repair published into the other registry")
            # The warning stays on, which is how the user learns of the split.
            self.assertIsNotNone(self.federation.state_dir_conflict())
            # An explicit registration still moves it.
            moved = self.federation.register(root)
            self.assertIsNotNone(moved)
            self.assertEqual(moved.id, ref.id)
            self.assertIsNone(self.federation.state_dir_conflict())
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)

    def test_a_moved_root_stops_being_served_by_its_old_registry(self):
        # Relocation used to publish only into the new registry, leaving the
        # root live in the old one. Peers there kept serving its last published
        # entries indefinitely — stale memories presented as current, with
        # nothing to hint at it since the path and mode had not changed.
        import importlib
        root = self._root(".claude-work")
        ref = self.federation.register(root)
        old_registry = self.config.STATE_DIR
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])
        elsewhere = self._home / "new-registry"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        try:
            moved = self.federation.register(root)      # explicit relocation
            self.assertIsNotNone(moved)
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(old_registry)
            importlib.reload(self.config)
        # Immediately: the departure is recorded where it left.
        self.assertTrue((old_registry / "roots" / f"{ref.id}.removed").is_file())
        # And regardless, its old readers no longer serve it.
        self.assertNotIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_an_unreachable_old_registry_still_stops_serving(self):
        # The departure note cannot always be written — the old registry may be
        # unreachable or read-only. The guarantee is on the reading side.
        import importlib
        root = self._root(".claude-work")
        ref = self.federation.register(root)
        old_registry = self.config.STATE_DIR
        elsewhere = self._home / "new-registry-2"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        real = self.federation._leave_registry
        self.federation._leave_registry = lambda *a, **k: None   # never lands
        try:
            self.assertIsNotNone(self.federation.register(root))
        finally:
            self.federation._leave_registry = real
            os.environ["ENGRAM_STATE_DIR"] = str(old_registry)
            importlib.reload(self.config)
        self.assertTrue((old_registry / "roots" / f"{ref.id}.json").is_file())
        self.assertNotIn(ref.id, [r.id for r in self.federation.iter_roots()],
                         "the old registry kept serving a moved root")

    def test_a_dead_old_registry_does_not_hang_the_relocation(self):
        # "Best effort" describes the outcome, not the time. Every step of the
        # departure note touches the registry being left, which can be a mount
        # that stopped answering — an unbounded courtesy would hang the very
        # relocation it exists to tidy up after.
        import importlib
        import threading as _t
        root = self._root(".claude-work")
        self.federation.register(root)
        old_registry = self.config.STATE_DIR
        elsewhere = self._home / "new-registry-3"
        os.environ["ENGRAM_STATE_DIR"] = str(elsewhere)
        importlib.reload(self.config)
        release = _t.Event()
        real_lock = self.federation.file_lock
        import contextlib as _c

        @_c.contextmanager
        def hangs_on_the_old_registry(path, allow_unsupported=False):
            if str(path).startswith(str(old_registry)):
                release.wait(30)
            with real_lock(path, allow_unsupported=allow_unsupported) as held:
                yield held

        probe = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.1
        self.federation.file_lock = hangs_on_the_old_registry
        try:
            start = time.monotonic()
            ref = self.federation.register(root)
            elapsed = time.monotonic() - start
        finally:
            self.federation.file_lock = real_lock
            self.federation._REGISTRY_PROBE_TIMEOUT = probe
            release.set()
            os.environ["ENGRAM_STATE_DIR"] = str(old_registry)
            importlib.reload(self.config)
        self.assertIsNotNone(ref, "the relocation did not complete")
        self.assertLess(elapsed, 5, "the relocation waited on a dead registry")

    def test_a_late_departure_note_cannot_undo_a_later_return(self):
        # The note runs on a thread the relocation stopped waiting for, so it
        # can take the lock long afterwards — by then the root may have been
        # registered back here, and acting on the old intent would tombstone a
        # live registration.
        root = self._root(".claude-work")
        ref = self.federation.register(root)          # home is this registry
        here = str(self.config.STATE_DIR)
        # A departure note that arrives late, for a move that has since been
        # reversed: the marker says this registry again.
        self.federation._leave_registry(ref.id, here, root)
        self.assertFalse(
            (self.config.STATE_DIR / "roots" / f"{ref.id}.removed").is_file(),
            "a stale note tombstoned a live registration")
        self.assertTrue(
            (self.config.STATE_DIR / "roots" / f"{ref.id}.json").is_file())
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_a_late_note_ignores_a_path_that_holds_another_root(self):
        # The thread carries the path it captured. By the time it runs, that
        # path can hold a different root — one that lives in another registry.
        # Validating position alone would read that stranger's marker, see it
        # is away, and tombstone *our* root's live registration on its word.
        root = self._root(".claude-work")
        ref = self.federation.register(root)          # live in this registry
        here = str(self.config.STATE_DIR)
        shard = self.config.STATE_DIR / "roots" / f"{ref.id}.json"
        self.assertTrue(shard.is_file())

        # Another root takes over the directory, and it belongs elsewhere.
        stranger = dict(self.federation.read_marker_data(root),
                        id="abcdef0123456789", registry=str(self._home / "far"))
        (root / self.federation.ROOT_MARKER).write_text(
            json.dumps(stranger), encoding="utf-8")

        self.federation._leave_registry(ref.id, here, root)
        self.assertTrue(shard.is_file(),
                        "a stranger's marker licensed removing our root")
        self.assertFalse(
            (self.config.STATE_DIR / "roots" / f"{ref.id}.removed").is_file())

    def test_a_hung_root_is_probed_once_not_once_per_recall(self):
        # iter_roots() runs on every recall. Probing each root's marker afresh
        # cost a full timeout and an abandoned thread per call on a hung mount:
        # the price grew with traffic, which is exactly backwards.
        import threading as _t
        root = self._root(".claude-work")
        self.federation.register(root)
        release = _t.Event()
        probes, lock = [], _t.Lock()
        real = self.federation.read_marker_data

        def hangs(path):
            if Path(path) == root:
                with lock:
                    probes.append(1)
                release.wait(30)
            return real(path)

        probe_timeout = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.05
        self.federation.read_marker_data = hangs
        try:
            start = time.monotonic()
            for _ in range(5):
                self.federation.iter_roots()
            elapsed = time.monotonic() - start
        finally:
            self.federation.read_marker_data = real
            self.federation._REGISTRY_PROBE_TIMEOUT = probe_timeout
            release.set()
            self.federation._marker_probes.clear()
        self.assertEqual(len(probes), 1, f"{len(probes)} probes for 5 recalls")
        self.assertLess(elapsed, 1, "each recall paid the timeout again")

    def test_a_second_caller_does_not_stack_a_probe_behind_the_first(self):
        # The gate reserved the root only *after* releasing the lock, so a
        # recall arriving while the first probe was still blocked found no
        # entry and spawned one of its own — on a hung mount, one permanently
        # blocked daemon per caller, which is what the gate exists to prevent.
        import threading as _t
        root = self._root(".claude-work")
        self.federation.register(root)
        entered, release = _t.Event(), _t.Event()
        probes, lock = [], _t.Lock()
        real = self.federation.read_marker_data

        def blocks(path):
            if Path(path) == root:
                with lock:
                    probes.append(1)
                entered.set()
                release.wait(30)
            return real(path)

        probe_timeout = self.federation._REGISTRY_PROBE_TIMEOUT
        # Left at 30 for the whole test, deliberately. Lowering it once the
        # first probe is running races the first caller's own read of it: that
        # caller could time out early, publish a cache entry, and the second
        # recall would then find one even without the reservation — the broken
        # code would pass. Here nothing can publish while the probe blocks, so
        # any entry the second caller sees was reserved before the worker ran.
        self.federation._REGISTRY_PROBE_TIMEOUT = 30
        self.federation.read_marker_data = blocks
        first = _t.Thread(
            target=lambda: self.federation._cached_home_registry(root),
            daemon=True)
        try:
            first.start()
            # Proves the interleaving instead of hoping for it: the first probe
            # is demonstrably inside read_marker_data and still blocked there.
            self.assertTrue(entered.wait(10), "first probe never started")
            self.federation._cached_home_registry(root)     # the second recall
        finally:
            self.federation._REGISTRY_PROBE_TIMEOUT = probe_timeout
            release.set()
            first.join(10)
            self.federation.read_marker_data = real
            self.federation._marker_probes.clear()
        self.assertEqual(len(probes), 1,
                         f"{len(probes)} probe threads for one blocked root")

    def test_an_answer_that_arrives_late_is_still_published(self):
        # A read that misses the timeout still finishes eventually. Its answer
        # was dropped on the floor, so the cache kept serving "could not tell"
        # for the rest of the TTL even though the filesystem had replied.
        import threading as _t
        root = self._root(".claude-work")
        self.federation.register(root)
        release = _t.Event()
        real = self.federation.read_marker_data

        def slow(path):
            if Path(path) == root:
                release.wait(30)
            return real(path)

        expected = self.federation._home_registry(real(root) or {})
        self.assertIsNotNone(expected, "root has no readable home registry")
        probe_timeout = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.05
        self.federation.read_marker_data = slow
        try:
            self.assertIsNone(self.federation._cached_home_registry(root),
                              "a timed-out probe reported an answer")
            worker = self.federation._marker_probes[str(root)]["thread"]
            release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "late probe never finished")
            again = self.federation._cached_home_registry(root)
        finally:
            self.federation._REGISTRY_PROBE_TIMEOUT = probe_timeout
            release.set()
            self.federation.read_marker_data = real
            self.federation._marker_probes.clear()
        self.assertEqual(again, expected,
                         "the late answer was dropped; cache still says None")

    def test_a_readable_marker_is_not_re_probed_every_time(self):
        root = self._root(".claude-work")
        self.federation.register(root)
        reads, real = [], self.federation.read_marker_data

        def counting(path):
            if Path(path) == root:
                reads.append(1)
            return real(path)

        self.federation.read_marker_data = counting
        try:
            for _ in range(4):
                self.federation.iter_roots()
        finally:
            self.federation.read_marker_data = real
            self.federation._marker_probes.clear()
        self.assertEqual(len(reads), 1, f"{len(reads)} reads for 4 recalls")

    def test_departure_refuses_a_registry_that_does_not_register_us(self):
        # The registry path comes from the marker — a file inside the root,
        # hand-editable, not ours. A crafted one pointed this at any directory
        # and had it create a lock and a tombstone there and unlink a JSON
        # file, all outside the configured state directory.
        ref = self.federation.register(self._root(".claude-work"))
        outside = self._home / "not-a-registry"
        (outside / "roots").mkdir(parents=True, exist_ok=True)
        bait = outside / "roots" / f"{ref.id}.json"
        # Shaped like a shard, but it does not say it registers this root's
        # path — so it is not a registration this call may withdraw.
        bait.write_text(json.dumps({"id": ref.id}), encoding="utf-8")
        before = sorted(q.relative_to(outside) for q in outside.rglob("*"))
        self.federation._leave_registry(ref.id, str(outside), ref.path)
        self.assertTrue(bait.is_file(), "deleted a file outside the registry")
        # Taking a lock is itself a write — file_lock creates the directory
        # and the lock file — so validating only once inside it still built a
        # tree in a directory this call had no business touching.
        self.assertEqual(
            sorted(q.relative_to(outside) for q in outside.rglob("*")), before,
            "created files in a directory that does not register us")

    def test_departure_leaves_no_trace_where_nothing_is_registered(self):
        # The extreme of the same point: a marker naming a path that holds no
        # registry at all must not bring one into being.
        ref = self.federation.register(self._root(".claude-work"))
        nowhere = self._home / "no-registry-here"
        self.federation._leave_registry(ref.id, str(nowhere), ref.path)
        self.assertFalse(nowhere.exists(),
                         f"{nowhere} was created by a departure")

    def test_an_aliased_state_dir_does_not_empty_federation(self):
        # Registration treats two names for one state directory as one
        # registry and leaves the marker's original spelling alone. Comparing
        # text here then read *every* root as relocated: federation went from
        # working to empty because the same directory was spelled differently.
        import importlib
        ref = self.federation.register(self._root(".claude-work"))
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])
        alias = self._home / "state-seen-through"
        os.symlink(self._state, alias)
        os.environ["ENGRAM_STATE_DIR"] = str(alias)
        importlib.reload(self.config)
        try:
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
            served = [r.id for r in self.federation.iter_roots()]
            # End-to-end smoke check that registering under the aliased
            # spelling still works. It does *not* exercise the marker-registry
            # comparison in _register_with_marker: a root registered afresh
            # writes the current spelling into its own marker, so that check
            # sees them equal either way.
            through_alias = self.federation.register(self._root(".claude-peo"))
        finally:
            os.environ["ENGRAM_STATE_DIR"] = str(self._state)
            importlib.reload(self.config)
            self.federation._marker_probes.clear()
            self.federation._registry_aliases.clear()
        self.assertIn(ref.id, served,
                      "the root vanished because the state dir was spelled "
                      "another way")
        self.assertIsNotNone(
            through_alias,
            "registering through the other spelling was refused")

    def test_a_replacement_that_lands_under_the_other_spelling_is_accepted(self):
        # The marker check after a replacement. Another process can win it,
        # and during a relocation the two registries do not even share a lock,
        # so what lands is read back rather than assumed. Read by spelling, a
        # marker naming this very state directory under its other name was
        # rejected and the registration thrown away.
        ref = self.federation.register(self._root(".claude-work"))
        alias = self._home / "state-other-name"
        os.symlink(self._state, alias)
        self.federation.unregister(ref.id)      # licenses the mode change
        real = self.federation._publish_marker

        def lands_aliased(root_path, payload, *, replace, exclusive):
            won = real(root_path, payload, replace=replace, exclusive=exclusive)
            # What the winner would have written from the other spelling.
            return dict(won, registry=str(alias)) if won else won

        self.federation._publish_marker = lands_aliased
        try:
            self.federation._registry_aliases.clear()
            again = self.federation.register(self._root(".claude-work"),
                                             mode="explicit")
        finally:
            self.federation._publish_marker = real
            self.federation._registry_aliases.clear()
        self.assertIsNotNone(
            again, "a marker naming this registry's other spelling was "
                   "rejected and the registration discarded")
        self.assertEqual(again.id, ref.id)

    def test_departure_refuses_an_alias_of_our_own_registry(self):
        # Two spellings of one state directory read as a move away from
        # ourselves, and the departure that followed tombstoned and deleted
        # the shard the registration had just written.
        ref = self.federation.register(self._root(".claude-work"))
        shard = self._state / "roots" / f"{ref.id}.json"
        self.assertTrue(shard.is_file(), "no shard to protect")
        alias = self._home / "state-alias"
        os.symlink(self._state, alias)
        self.federation._leave_registry(ref.id, str(alias), ref.path)
        self.assertTrue(shard.is_file(),
                        "the departure deleted our own registration")
        self.assertFalse((self._state / "roots" / f"{ref.id}.removed").is_file(),
                         "tombstoned ourselves in our own registry")

    def test_departure_takes_the_project_shards_with_it(self):
        # Leaving removed only roots/<id>.json. A root that later returns
        # clears its own tombstone, and every project shard left behind read
        # as valid again — advertising memories changed or deleted since.
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"))
        old_registry = self._home / "old-registry"
        roots = old_registry / "roots"
        roots.mkdir(parents=True, exist_ok=True)
        (roots / f"{ref.id}.json").write_text(json.dumps(
            {"id": ref.id, "path": str(ref.path), "mode": "config",
             "label": ref.label}), encoding="utf-8")
        left = []
        for name in ("alpha", "beta"):
            d = old_registry / "projects" / name / "roots"
            d.mkdir(parents=True, exist_ok=True)
            shard = d / f"{ref.id}.json"
            shard.write_text(json.dumps(
                {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
                 "label": ref.label, "memory_dir": str(ref.memory_dir(name)),
                 "entries": [{"filename": "stale.md"}]}), encoding="utf-8")
            left.append(shard)
        self.federation._leave_registry(ref.id, str(old_registry), ref.path)
        self.assertFalse((roots / f"{ref.id}.json").is_file(),
                         "the root shard survived the departure")
        self.assertFalse(any(s.is_file() for s in left),
                         "project shards were left behind to come back to life")

    def test_without_locking_a_corrupt_marker_is_left_alone(self):
        # Replacing a corrupt marker is a rewrite like any other. Repairing it
        # without exclusion is the same last-writer-wins race, so it waits for
        # a filesystem that can lock rather than guessing.
        import contextlib as _c
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        (root / self.federation.ROOT_MARKER).write_text("{ truncated",
                                                        encoding="utf-8")
        real = self.federation.file_lock
        roots_dir = self.federation.roots_dir()

        @_c.contextmanager
        def root_cannot_lock(path, allow_unsupported=False):
            if Path(path).parent != roots_dir:
                yield self.federation.DEGRADED if allow_unsupported else False
                return
            with real(path, allow_unsupported=allow_unsupported) as held:
                yield held

        self.federation.file_lock = root_cannot_lock
        try:
            self.assertIsNone(self.federation.ensure_marker(root),
                              "a corrupt marker was replaced without a lock")
        finally:
            self.federation.file_lock = real
        self.assertEqual((root / self.federation.ROOT_MARKER).read_text(),
                         "{ truncated")
        # With locking available it is repaired as before.
        self.assertIsNotNone(self.federation.ensure_marker(root))

    def test_a_claim_that_cannot_round_trip_is_never_written(self):
        # Claims share one comma-separated line, so a filename with a comma
        # would be split into fragments matching nothing.
        from foldcrumbs.schema import _clean_claim
        rid = "0123456789abcdef"
        self.assertIsNotNone(_clean_claim(f"{rid}:fact_ok.md"))
        self.assertIsNone(_clean_claim(f"{rid}:fact,with,commas.md"))
        self.assertIsNone(_clean_claim(f"{rid}:../escape.md"))
        # POSIX allows a newline in a filename; it would split the frontmatter
        # field across lines and the claim would be read back as fragments.
        self.assertIsNone(_clean_claim(f"{rid}:fact_two\nlines.md"))
        self.assertIsNone(_clean_claim(f"{rid}:fact_cr\rlines.md"))

    def test_mode_conflict_is_reported(self):
        root = self._root(".claude")
        self.federation.register(root)          # recorded as config
        os.environ["FOLDCRUMBS_DIR"] = str(root)
        try:
            msg = self.federation.mode_conflict()
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        self.assertIsNotNone(msg)
        self.assertIn("config", msg)

    def test_marker_registry_split_is_detected(self):
        # The invisible case: this instance federated into another state dir,
        # so no scan of *this* registry could ever reveal the other roots.
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere/.foldcrumbs"
        marker.write_text(json.dumps(data), encoding="utf-8")
        msg = self.federation.state_dir_conflict()
        self.assertIsNotNone(msg)
        self.assertIn("/elsewhere/.foldcrumbs", msg)

    def test_state_dir_conflict_detected(self):
        ref = self.federation.register(self._root(".claude-work"))
        shard = self._state / "roots" / f"{ref.id}.json"
        data = json.loads(shard.read_text())
        data["state_dir"] = "/somewhere/else"
        shard.write_text(json.dumps(data), encoding="utf-8")
        msg = self.federation.state_dir_conflict()
        self.assertIsNotNone(msg)
        self.assertIn("/somewhere/else", msg)

    def test_no_conflict_when_all_agree(self):
        self.federation.register(self._root(".claude"))
        self.federation.register(self._root(".claude-work"))
        self.assertIsNone(self.federation.state_dir_conflict())


class TestIndexShards(_FederationEnv):
    """Per-root index shards and their total ordering."""

    def setUp(self):
        super().setUp()
        from foldcrumbs import index_shard, store
        self.index_shard, self.store = index_shard, store
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)

    def _memory(self, root, title, body, created, type_="fact"):
        from foldcrumbs.schema import MemoryRecord
        mem_dir = root.memory_dir(self.proj)
        mem_dir.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title=title, content=body, type=type_)
        text = rec.to_markdown().replace(
            f"created_at: {rec.created_at.isoformat()}", f"created_at: {created}")
        (mem_dir / rec.filename()).write_text(text, encoding="utf-8")
        return rec.filename()

    def test_shard_is_written_next_to_the_untouched_local_index(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Recall is grep", "No vector DB here.",
                     "2026-01-01T00:00:00+00:00")
        index = self.store.rebuild_index(self.proj)
        body = index.read_text(encoding="utf-8")
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertTrue(shard.is_file())
        data = json.loads(shard.read_text())
        self.assertEqual(data["root_id"], ref.id)
        self.assertEqual(len(data["entries"]), 1)
        self.assertTrue(Path(data["entries"][0]["path"]).is_absolute())
        # The local index carries no trace of federation.
        self.assertNotIn(ref.id, body)
        self.assertNotIn("federated", body.lower())

    def test_read_shards_skips_current_and_unregistered_roots(self):
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        for ref in (mine, other):
            d = self.index_shard.shards_dir(self.proj)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{ref.id}.json").write_text(json.dumps(
                {"root_id": ref.id, "version": self.index_shard.SHARD_VERSION,
                 "memory_dir": str(ref.memory_dir(self.proj)),
                 "entries": []}), encoding="utf-8")
        got = [s["root_id"] for s in self.index_shard.read_shards(self.proj)]
        self.assertEqual(got, [other.id])          # current one excluded
        self.federation.unregister(other.id)
        self.assertEqual(self.index_shard.read_shards(self.proj), [])

    def test_unavailable_root_keeps_its_entries_flagged(self):
        import shutil
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps(
            {"root_id": other.id, "version": self.index_shard.SHARD_VERSION,
             "memory_dir": str(other.memory_dir(self.proj)),
             "entries": [{"filename": "a.md"}]}), encoding="utf-8")
        shutil.rmtree(other.path)
        shards = self.index_shard.read_shards(self.proj)
        self.assertEqual(len(shards), 1)
        self.assertFalse(shards[0]["available"])
        self.assertEqual(len(shards[0]["entries"]), 1)  # not dropped

    def test_merge_order_is_total_and_root_independent(self):
        # Same data, shards presented in either order, must merge identically:
        # that is what lets every instance agree without a shared file.
        order = ["decision", "fact"]
        a = {"root_id": "aaaaaaaaaaaaaaaa", "label": "a", "entries": [
            {"filename": "x.md", "type": "fact", "created_at": "2026-01-01T00:00:00+00:00"},
            {"filename": "same.md", "type": "fact", "created_at": "2026-02-01T00:00:00+00:00"},
        ]}
        b = {"root_id": "bbbbbbbbbbbbbbbb", "label": "b", "entries": [
            {"filename": "same.md", "type": "fact", "created_at": "2026-02-01T00:00:00+00:00"},
            {"filename": "d.md", "type": "decision", "created_at": "2020-01-01T00:00:00+00:00"},
        ]}
        one = self.index_shard.merge_entries([a, b], order)
        two = self.index_shard.merge_entries([b, a], order)
        self.assertEqual([(e["root_id"], e["filename"]) for e in one],
                         [(e["root_id"], e["filename"]) for e in two])
        # decision first (type order), then facts newest-first, and the
        # colliding filename is broken by root id — not left to chance.
        self.assertEqual([e["filename"] for e in one],
                         ["d.md", "same.md", "same.md", "x.md"])
        self.assertEqual([e["root_id"] for e in one[1:3]],
                         ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    def test_missing_created_at_uses_a_reproducible_timestamp(self):
        from foldcrumbs.schema import MemoryRecord
        ref = self.federation.register(self._root(".claude"))
        mem_dir = ref.memory_dir(self.proj)
        mem_dir.mkdir(parents=True, exist_ok=True)
        p = mem_dir / "fact_no_date.md"
        p.write_text("---\nname: No date\ntype: fact\n---\n\nBody.\n",
                     encoding="utf-8")
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        self.assertTrue(rec.created_at_missing)
        first = self.index_shard._stable_created_at(rec, p)
        again = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        self.assertEqual(first, self.index_shard._stable_created_at(again, p))

    def test_naive_timestamps_do_not_break_the_index_sort(self):
        from foldcrumbs.schema import MemoryRecord
        naive = MemoryRecord.from_markdown(
            "---\nname: Old\ntype: fact\ncreated_at: 2020-01-01T00:00:00\n---\n\nB.\n")
        aware = MemoryRecord(title="New", content="B.", type="fact")
        self.assertIsNotNone(naive.created_at.tzinfo)
        sorted([naive, aware], key=lambda m: m.created_at)  # must not raise

    def test_invalid_timestamp_is_treated_as_missing(self):
        from foldcrumbs.schema import MemoryRecord
        text = ("---\nname: Bad date\ntype: fact\ncreated_at: not-a-date\n"
                "---\n\nBody.\n")
        a = MemoryRecord.from_markdown(text)
        b = MemoryRecord.from_markdown(text)
        # Present but unparseable is invented too, and differently each time —
        # so it must be flagged exactly like an absent one.
        self.assertTrue(a.created_at_missing)
        self.assertNotEqual(a.created_at, b.created_at)
        p = self.proj / "bad.md"
        p.write_text(text, encoding="utf-8")
        self.assertEqual(self.index_shard._stable_created_at(a, p),
                         self.index_shard._stable_created_at(b, p))

    def test_paths_stay_absolute_under_a_relative_override(self):
        # Shard entries are read by *other* instances, from *their* cwd: a
        # relative path there would resolve somewhere else entirely.
        cwd = os.getcwd()
        os.chdir(self._home)
        os.environ["FOLDCRUMBS_DIR"] = "relative-memory"
        try:
            self.assertTrue(self.config.memory_dir().is_absolute())
            os.environ["CLAUDE_CONFIG_DIR"] = "relative-config"
            os.environ.pop("FOLDCRUMBS_DIR")
            self.assertTrue(self.config.claude_config_dir().is_absolute())
            self.assertTrue(self.config.memory_dir(self.proj).is_absolute())
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
            os.environ["CLAUDE_CONFIG_DIR"] = str(self._home / ".claude")
            os.chdir(cwd)

    def test_duplicate_entries_do_not_depend_on_arrival_order(self):
        order = ["fact"]
        dup = {"root_id": "aaaaaaaaaaaaaaaa", "label": "a", "entries": [
            {"filename": "x.md", "type": "fact", "title": "first",
             "created_at": "2026-01-01T00:00:00+00:00"},
            {"filename": "x.md", "type": "fact", "title": "second",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ]}
        rows = self.index_shard.merge_entries([dup], order)
        flipped = dict(dup, entries=list(reversed(dup["entries"])))
        other = self.index_shard.merge_entries([flipped], order)
        self.assertEqual(len(rows), 1)
        # Not just the count: the *same* record must survive either way, or
        # its title and timestamp — and so its position — depend on arrival.
        self.assertEqual(rows, other)
        self.assertEqual(rows[0]["title"], "first")   # newest wins, by content

    def test_shard_validation_rejects_every_malformed_shape(self):
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{other.id}.json"
        good = {"root_id": other.id, "version": self.index_shard.SHARD_VERSION,
                "memory_dir": str(other.memory_dir(self.proj)), "entries": []}
        for broken in (
            {k: v for k, v in good.items() if k != "version"},   # no version
            dict(good, version="1"),                             # not an int
            dict(good, version=self.index_shard.SHARD_VERSION + 1),
            {k: v for k, v in good.items() if k != "entries"},   # no entries
            dict(good, entries={"not": "a list"}),
            dict(good, root_id="ffffffffffffffff"),              # id mismatch
        ):
            target.write_text(json.dumps(broken), encoding="utf-8")
            self.assertEqual(self.index_shard.read_shards(self.proj), [],
                             f"accepted malformed shard: {broken}")
        target.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(len(self.index_shard.read_shards(self.proj)), 1)

    def test_shard_from_a_newer_format_is_skipped(self):
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps({
            "root_id": other.id, "version": self.index_shard.SHARD_VERSION + 1,
            "entries": [{"filename": "a.md"}]}), encoding="utf-8")
        self.assertEqual(self.index_shard.read_shards(self.proj), [])

    def _publish(self, ref, entries, **extra):
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        payload = {"root_id": ref.id, "version": self.index_shard.SHARD_VERSION,
                   "label": ref.label, "memory_dir": str(ref.memory_dir(self.proj)),
                   "entries": entries}
        payload.update(extra)
        (d / f"{ref.id}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_rendered_block_announces_paths_and_read_only(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "d.md", "type": "decision",
                               "title": "Recall is grep", "description": "No vector DB.",
                               "path": "/abs/d.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["decision", "fact"])
        self.assertIn("<foldcrumbs-federated>", block)
        self.assertIn("claude-work", block)
        self.assertIn("/abs/d.md", block)          # greppable
        self.assertIn("READ-ONLY", block)          # ownership stated
        self.assertIn(str(other.memory_dir(self.proj)), block)

    def test_rendered_block_is_empty_without_other_instances(self):
        self.federation.register(self._root(".claude"))
        self.assertEqual(self.index_shard.render_block(self.proj, ["fact"]), "")

    def test_rendered_block_caps_and_says_what_it_dropped(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": f"m{i:03d}.md", "type": "fact", "title": f"M{i}",
             "path": f"/abs/m{i:03d}.md",
             "created_at": f"2026-01-01T00:{i:02d}:00+00:00"}
            for i in range(50)
        ])
        block = self.index_shard.render_block(
            self.proj, ["fact"], max_entries=10)
        self.assertEqual(block.count("[claude-work]"), 10)
        # "further", not "older": type rank outranks date in the sort, so what
        # falls off the end is not necessarily the oldest.
        self.assertIn("40 further entries not shown", block)

    def test_rendered_block_flags_an_unreachable_root(self):
        import shutil
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        shutil.rmtree(other.path)
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("UNREACHABLE", block)
        self.assertIn("A", block)   # entries kept, not silently dropped

    def test_rendered_block_reports_a_stale_shard(self):
        other = self.federation.register(self._root(".claude-work"))
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}],
                      written_at=old)
        self.assertIn("last published 90d ago",
                      self.index_shard.render_block(self.proj, ["fact"]))

    def test_one_malformed_entry_does_not_erase_the_whole_view(self):
        # The hook swallows exceptions, so a TypeError in the sort would not
        # degrade the federated view — it would blank it.
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": "bad.md", "type": "fact", "title": "Bad",
             "created_at": ["not", "a", "string"], "path": "/abs/bad.md"},
            {"filename": None},
            "not even a dict",
            {"filename": "good.md", "type": "fact", "title": "Good",
             "path": "/abs/good.md", "created_at": "2026-01-01T00:00:00+00:00"},
        ])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("Good", block)
        self.assertIn("Bad", block)     # kept, with its bad field coerced away
        rows = self.index_shard.merge_entries(
            self.index_shard.read_shards(self.proj), ["fact"])
        self.assertEqual({r["filename"] for r in rows}, {"bad.md", "good.md"})

    def test_availability_probe_is_time_bounded(self):
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        real = type(other).available
        release = _t.Event()

        def hang(self_ref):
            release.wait(5)     # interruptible: nothing outlives the test
            return True

        type(other).available = hang
        try:
            start = time.monotonic()
            got = other.available_within(0.05)
        finally:
            release.set()
            type(other).available = real
        self.assertIsNone(got)                       # no answer, not a guess
        self.assertLess(time.monotonic() - start, 2)  # and it did not hang

    def test_unknown_publication_date_is_stated(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("never reported when it was published", block)

    def test_long_entries_are_capped_by_size_too(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": f"m{i}.md", "type": "fact", "title": f"M{i}",
             "description": "x" * 4000, "path": f"/abs/m{i}.md",
             "created_at": f"2026-01-01T00:{i:02d}:00+00:00"}
            for i in range(10)
        ])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertLess(len(block), 20000)
        self.assertIn("further entries not shown", block)

    def test_shard_is_published_on_first_federated_read(self):
        # Federating an existing store must not look empty to everyone else
        # until its owner happens to write something.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Pre-existing", "Recorded before federating.",
                     "2026-01-01T00:00:00+00:00")
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertFalse(shard.exists())        # nothing rebuilt yet
        self.index_shard.ensure_shard(self.proj)
        self.assertTrue(shard.is_file())
        self.assertEqual(len(json.loads(shard.read_text())["entries"]), 1)

    def test_ensure_shard_does_not_republish_a_current_one(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        first = self.index_shard.ensure_shard(self.proj)
        stamp = first.stat().st_mtime
        # Returns the existing shard without rewriting it: republishing on
        # every session would churn the file for no reader benefit.
        self.assertEqual(self.index_shard.ensure_shard(self.proj), first)
        self.assertEqual(first.stat().st_mtime, stamp)

    def test_the_store_is_read_while_the_lock_is_held(self):
        # The invariant that makes a stale overwrite impossible: entries are
        # scanned inside the critical section, so no other process can publish
        # between reading the store and writing the shard. Every version that
        # scanned first and checked afterwards had a blind spot.
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "First", "Body one.", "2026-01-01T00:00:00+00:00")
        real_lock = self.federation.file_lock
        added = []

        @_c.contextmanager
        def lock_then_mutate(path):
            with real_lock(path) as held:
                if held and not added:
                    # A write landing exactly here is invisible to any scan
                    # taken before the lock; it must be visible to this one.
                    added.append(self._memory(ref, "Second", "Body two.",
                                              "2026-01-02T00:00:00+00:00"))
                yield held

        self.federation.file_lock = lock_then_mutate
        try:
            self.index_shard.write_shard(self.proj)
        finally:
            self.federation.file_lock = real_lock
        shard = self.index_shard.shard_path(ref.id, self.proj)
        titles = {e["title"] for e in json.loads(shard.read_text())["entries"]}
        self.assertEqual(titles, {"First", "Second"})

    def test_publishing_does_not_hold_the_machine_wide_registry_lock(self):
        # The scan runs inside the lock, so a large store held the registry
        # long enough to stall every other instance's SessionStart. Only this
        # instance's own processes race for this shard.
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        taken = []
        real = self.federation.file_lock

        @_c.contextmanager
        def record(path, allow_unsupported=False):
            taken.append(Path(path))
            with real(path, allow_unsupported=allow_unsupported) as held:
                yield held

        self.federation.file_lock = record
        try:
            self.index_shard.write_shard(self.proj)
        finally:
            self.federation.file_lock = real
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].parent, self.index_shard.shards_dir(self.proj))
        self.assertNotEqual(taken[0].parent, self.federation.roots_dir())
        self.assertIn(ref.id, taken[0].name)

    def test_a_lock_held_elsewhere_does_not_hang_the_hook(self):
        # A bounded wait: an editor that will not start is worse than a
        # publish that waits for the next session.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        lock = self.index_shard.shards_dir(self.proj) / f".lock-{ref.id}"
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        try:
            with self.federation.file_lock(lock) as held:
                self.assertTrue(held)
                start = time.monotonic()
                self.assertIsNone(self.index_shard.write_shard(self.proj))
                self.assertLess(time.monotonic() - start, 3)
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait

    def test_a_filesystem_that_cannot_lock_fails_fast(self):
        # ENOLCK will never become success, so retrying it spends the whole
        # deadline on the session-start path for nothing.
        import errno as _e
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        lock = self.index_shard.shards_dir(self.proj) / ".lock-probe"
        real = self.federation.fcntl.flock

        def unsupported(*a, **kw):
            raise OSError(_e.ENOLCK, "no locks available")

        self.federation.fcntl.flock = unsupported
        try:
            start = time.monotonic()
            with self.federation.file_lock(lock) as held:
                self.assertFalse(held)
            elapsed = time.monotonic() - start
        finally:
            self.federation.fcntl.flock = real
        self.assertLess(elapsed, self.federation._LOCK_WAIT_SECONDS / 2)

    def test_contention_is_still_waited_out(self):
        # The counterpart: a lock merely held by someone else must be retried,
        # not abandoned on the first refusal.
        import errno as _e
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        lock = self.index_shard.shards_dir(self.proj) / ".lock-probe2"
        real = self.federation.fcntl.flock
        calls = []

        def busy_then_free(fd, op):
            calls.append(op)
            if len(calls) < 3:
                raise OSError(_e.EWOULDBLOCK, "would block")
            return real(fd, op)

        self.federation.fcntl.flock = busy_then_free
        try:
            with self.federation.file_lock(lock) as held:
                self.assertTrue(held)
        finally:
            self.federation.fcntl.flock = real
        self.assertGreaterEqual(len(calls), 3)

    def test_a_moved_root_republishes_even_with_unchanged_memories(self):
        # Readers refuse a shard describing a layout the root has left. The
        # publisher skipped writing whenever the entries matched, so a root
        # that moved without its memories changing kept re-deciding it had
        # nothing to say — and stayed invisible until someone edited a memory.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        target = self.index_shard.write_shard(self.proj)
        published = json.loads(target.read_text())
        # What an earlier layout left behind: same entries, other directory.
        stale = dict(published,
                     memory_dir=str(self._home / "where-it-used-to-live"))
        target.write_text(json.dumps(stale), encoding="utf-8")
        self.index_shard.write_shard(self.proj)
        again = json.loads(target.read_text())
        self.assertEqual(again["entries"], published["entries"],
                         "the entries changed")
        self.assertEqual(again["memory_dir"], published["memory_dir"],
                         "the shard kept naming a directory this root left")

    def test_a_shard_already_matching_the_store_is_left_alone(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        first = self.index_shard.write_shard(self.proj)
        stamp = first.stat().st_mtime
        self.assertEqual(self.index_shard.write_shard(self.proj), first)
        self.assertEqual(first.stat().st_mtime, stamp)   # no churn

    def test_an_edit_that_preserves_size_and_mtime_still_republishes(self):
        # A one-character change keeps the size; a restore or a sync can keep
        # the mtime. Deciding by stat alone would leave the old title
        # published for good, so the entries themselves are compared.
        ref = self.federation.register(self._root(".claude"))
        name = self._memory(ref, "Deploys run on Mondays", "Body.",
                            "2026-01-01T00:00:00+00:00")
        self.index_shard.write_shard(self.proj)
        shard = self.index_shard.shard_path(ref.id, self.proj)
        path = ref.memory_dir(self.proj) / name
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8")
                        .replace("Mondays", "Fridays"), encoding="utf-8")
        # ns, not float seconds: restoring with float leaves st_mtime_ns
        # different, which would move the stat signature and let this test
        # pass without the fix it exists to prove.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.index_shard.ensure_shard(self.proj)
        titles = [e["title"] for e in json.loads(shard.read_text())["entries"]]
        self.assertEqual(titles, ["Deploys run on Fridays"])

    def test_deleting_a_memory_reaches_the_shard(self):
        # The version has to change when a store *shrinks*: a newest-mtime
        # counter goes down on delete, so the removed memory would stay
        # published forever.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Keep", "Body one.", "2026-01-01T00:00:00+00:00")
        name = self._memory(ref, "Drop", "Body two.", "2026-01-02T00:00:00+00:00")
        self.index_shard.write_shard(self.proj)
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertEqual(len(json.loads(shard.read_text())["entries"]), 2)
        (ref.memory_dir(self.proj) / name).unlink()
        self.index_shard.ensure_shard(self.proj)
        titles = [e["title"] for e in json.loads(shard.read_text())["entries"]]
        self.assertEqual(titles, ["Keep"])

    def test_shard_is_not_published_without_the_lock(self):
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        real = self.federation.file_lock

        @_c.contextmanager
        def unlockable(path, allow_unsupported=False):
            yield False

        self.federation.file_lock = unlockable
        try:
            self.assertIsNone(self.index_shard.write_shard(self.proj))
        finally:
            self.federation.file_lock = real
        self.assertFalse(self.index_shard.shard_path(ref.id, self.proj).exists())

    def test_federated_scan_reads_a_bounded_number_of_files(self):
        from foldcrumbs import store
        other = self.federation.register(self._root(".claude-work"))
        d = other.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        cap = store._MAX_FEDERATED_SCAN
        reads = []
        real_read = Path.read_text

        def counting_read(self_path, *a, **kw):
            if self_path.suffix == ".md":
                reads.append(self_path.name)
            return real_read(self_path, *a, **kw)

        for i in range(cap + 25):
            # Unparseable on purpose: a file that fails to become a record
            # still cost a read, which is what the cap has to bound.
            (d / f"m{i:04d}.md").write_text("not frontmatter", encoding="utf-8")
        Path.read_text = counting_read
        try:
            list(store.iter_federated(self.proj))
        finally:
            Path.read_text = real_read
        self.assertLessEqual(len(reads), cap)

    def test_a_single_huge_entry_cannot_blow_the_size_cap(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "big.md", "type": "fact",
                               "title": "T" * 200, "description": "x" * 500000,
                               "path": "/abs/big.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertLess(len(block), self.index_shard._MAX_FEDERATED_CHARS + 2000)
        self.assertIn("/abs/big.md", block)   # the path survives the cut

    def test_unfederated_root_publishes_nothing(self):
        self._root(".claude").mkdir(parents=True)   # marker never created
        self.assertIsNone(self.index_shard.write_shard(self.proj))


class TestProfiles(_FederationEnv):
    """One identity per agent or node, on top of the roots that already exist."""

    def setUp(self):
        super().setUp()
        from foldcrumbs import profiles
        self.profiles = profiles

    def test_a_dedicated_profile_keeps_one_store_for_every_project(self):
        ref = self.profiles.add("councillor")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.mode, "explicit")
        a = ref.memory_dir(self._home / "projA")
        b = ref.memory_dir(self._home / "projB")
        self.assertEqual(a, b,
                         "a dedicated profile split its memory by project")

    def test_a_shared_profile_keeps_memory_per_project(self):
        other = self._root(".claude-work")
        ref = self.profiles.add("assistant", self.profiles.SHARED, other)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.mode, "config")
        a = ref.memory_dir(self._home / "projA")
        b = ref.memory_dir(self._home / "projB")
        self.assertNotEqual(a, b,
                            "a shared profile merged two projects' memory")

    def test_a_shared_profile_will_not_guess_its_directory(self):
        # That directory belongs to someone else; picking one would be a
        # decision dressed up as a default.
        with self.assertRaises(ValueError):
            self.profiles.add("assistant", self.profiles.SHARED)

    def test_a_name_that_would_escape_its_directory_is_refused(self):
        for bad in ("../elsewhere", "with/slash", ".hidden", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.profiles.add(bad)

    def test_env_names_the_variable_that_matches_the_shape(self):
        self.profiles.add("councillor")
        self.profiles.add("assistant", self.profiles.SHARED,
                          self._root(".claude-work"))
        self.assertIn("FOLDCRUMBS_DIR", self.profiles.env_line("councillor"))
        self.assertIn("CLAUDE_CONFIG_DIR", self.profiles.env_line("assistant"))
        self.assertIsNone(self.profiles.env_line("nobody"))

    def test_the_env_line_actually_selects_that_store(self):
        # The command exists because a CLI cannot change its parent's
        # environment. The least it can do is print a line that works.
        import importlib
        from foldcrumbs import config as _config, store
        ref = self.profiles.add("councillor")
        line = self.profiles.env_line("councillor")
        var, _, value = line[len("export "):].partition("=")
        os.environ[var] = value.strip('"')
        try:
            importlib.reload(_config)
            self.assertEqual(_config.memory_dir(self._home / "anywhere"),
                             ref.memory_dir(self._home / "anywhere"))
            store.write_memory(MemoryRecord(title="Mine", content="Body.",
                                            type="fact"))
            self.assertTrue((ref.memory_dir() / "fact_mine.md").is_file(),
                            "the printed line did not select that store")
        finally:
            os.environ.pop(var, None)
            importlib.reload(_config)

    def test_roots_registered_before_profiles_are_listed_too(self):
        # They are the same thing. Hiding them would suggest a second
        # registry that does not exist.
        ref = self.federation.register(self._root(".claude-work"),
                                       mode="config")
        listed = {p["name"]: p for p in self.profiles.listing()}
        self.assertIn(ref.label, listed)
        self.assertEqual(listed[ref.label]["kind"], self.profiles.SHARED,
                         "a config root was not described as a shared profile")

    def test_a_name_two_roots_answer_to_is_refused_not_guessed(self):
        # Labels have never been unique — two config dirs called .claude under
        # different homes are both plausible. Guessing points a process at
        # another agent's store, or unregisters one nobody meant to touch.
        first = self.profiles.add("councillor")
        twin = self._home / "twin-home" / "councillor"
        twin.mkdir(parents=True, exist_ok=True)
        self.federation.register(twin, mode="explicit", label="councillor")
        self.assertEqual(
            len([p for p in self.profiles.listing()
                 if p["name"] == "councillor"]), 2)
        with self.assertRaises(self.profiles.AmbiguousProfile):
            self.profiles.env_line("councillor")
        with self.assertRaises(self.profiles.AmbiguousProfile):
            self.profiles.remove("councillor")
        self.assertTrue((first.path).is_dir(),
                        "an ambiguous name unregistered something anyway")

    def test_a_name_another_root_already_holds_is_refused(self):
        # Under the registry lock, not before it: asking first would be a
        # check-then-act — two processes both find the name free, both take
        # it, and it then identifies nothing.
        first = self.profiles.add("councillor")
        elsewhere = self._home / "another-councillor"
        elsewhere.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(self.federation.FederationConflict):
            self.profiles.add("councillor", path=elsewhere)
        self.assertEqual(
            [p["path"] for p in self.profiles.listing()
             if p["name"] == "councillor"], [str(first.path)],
            "a second root took a name that was already in use")

    def test_re_adding_the_same_profile_is_idempotent(self):
        # Same name, same directory: that is the profile being registered
        # again, not a second one claiming its name.
        first = self.profiles.add("councillor")
        again = self.profiles.add("councillor")
        self.assertIsNotNone(again)
        self.assertEqual(again.id, first.id)

    def test_the_env_line_survives_an_awkward_path(self):
        # A memory directory can hold a space, a quote, a dollar sign. A line
        # meant to be pasted or eval'd would otherwise set the wrong variable
        # — or run whatever the path spells.
        import shlex
        awkward = self._home / 'od d "$(echo pwned)"'
        awkward.mkdir(parents=True, exist_ok=True)
        self.profiles.add("tricky", path=awkward)
        line = self.profiles.env_line("tricky")
        var, _, value = line[len("export "):].partition("=")
        self.assertEqual(shlex.split(f"{var}={value}"),
                         [f"{var}={awkward}"],
                         f"a shell would not read this back whole: {line}")

    def test_removing_a_profile_leaves_its_memories_alone(self):
        ref = self.profiles.add("councillor")
        (ref.path / "fact_kept.md").write_text(
            "---\nname: Kept\ntype: fact\n---\n\nBody.\n", encoding="utf-8")
        self.assertTrue(self.profiles.remove("councillor"))
        self.assertNotIn("councillor",
                         [p["name"] for p in self.profiles.listing()])
        self.assertTrue((ref.path / "fact_kept.md").is_file(),
                        "removing a profile deleted its memories")
        self.assertFalse(self.profiles.remove("councillor"),
                         "removing an unknown profile reported success")


class TestFederatedSearch(_FederationEnv):
    """Federated recall: the path OpenCode and Codex depend on."""

    def setUp(self):
        super().setUp()
        from foldcrumbs import store
        self.store = store
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)

    def _drain_scans(self, release=None):
        """Wake any fake-hung scan, wait for it, then clear the store's maps.

        ``Event.set()`` only unblocks the worker: it can still be inside the
        store appending results. Clearing the maps or restoring the patched
        reader while it runs lets it race the next test — which is how a
        timing test starts failing for reasons that have nothing to do with it.
        """
        if release is not None:
            release.set()
        with self.store._pending_lock:
            threads = [v["thread"] for v in self.store._pending_scans.values()]
            threads += [t for ts in self.store._stuck_roots.values() for t in ts]
            threads += list(self.store._root_busy.values())
        for t in threads:
            t.join(10)
        with self.store._pending_lock:
            self.store._pending_scans.clear()
            self.store._stuck_roots.clear()
            self.store._root_busy.clear()

    def _write(self, ref, title, content, type_="fact"):
        from foldcrumbs.schema import MemoryRecord
        d = ref.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title=title, content=content, type=type_)
        (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        return rec

    def test_search_finds_another_instances_memory(self):
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(mine, "Local note", "Something about caching.")
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        hits = self.store.search("deploys on fridays", cwd=self.proj)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Deploy window")
        self.assertTrue(hits[0].is_foreign)
        self.assertEqual(hits[0].origin_root, "claude-work")
        self.assertTrue(Path(hits[0].origin_path).is_absolute())

    def test_search_can_stay_local(self):
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        self.assertEqual(
            self.store.search("fridays", cwd=self.proj, federated=False), [])

    def test_local_memory_wins_an_exact_tie(self):
        # Both scored identically, the actionable one first: only the local
        # copy can be forgotten or superseded from here.
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        for ref in (mine, other):
            self._write(ref, "Same note", "Identical content here.")
        hits = self.store.search("identical content", cwd=self.proj)
        self.assertFalse(hits[0].is_foreign)
        self.assertTrue(hits[1].is_foreign)

    def test_unregistered_instance_is_not_searched(self):
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        self.federation.unregister(other.id)
        self.assertEqual(self.store.search("fridays", cwd=self.proj), [])

    def test_foreign_results_are_labelled_read_only(self):
        from foldcrumbs.profile import format_context_block
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        block = format_context_block(
            self.store.search("fridays", cwd=self.proj), heading="deploy")
        self.assertIn("from claude-work", block)
        self.assertIn("read-only", block)

    def test_write_paths_refuse_a_foreign_record(self):
        # The rendered blocks tell the model these are read-only; this is the
        # part that does not depend on the model believing it.
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Theirs", "Foreign body.")
        foreign = self.store.search("foreign body", cwd=self.proj)[0]
        self.assertTrue(foreign.is_foreign)
        for call in (
            lambda: self.store.write_memory(foreign, self.proj),
            lambda: self.store.upsert(foreign, self.proj),
            lambda: self.store.mark_superseded_on_disk(foreign, "x", self.proj),
        ):
            with self.assertRaises(self.store.ForeignMemoryError):
                call()
        # And nothing was created in this root as a side effect.
        self.assertEqual(list(self.store.iter_memories(self.proj)), [])

    def test_contradiction_with_a_foreign_memory_is_asserted_locally(self):
        from foldcrumbs import distill
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "PyPI publishing deferred",
                            "Publishing to PyPI is deferred for now.",
                            type_="decision")
        fresh = MemoryRecord(title="Published to PyPI",
                             content="Publishing to PyPI is done and released.",
                             type="fact")
        calls = []
        real_chat = distill.llm.chat

        def yes(*a, **kw):
            calls.append(1)
            return '{"supersedes": true}'

        distill.llm.chat = yes
        try:
            n = distill._auto_supersede([fresh], self.proj)
        finally:
            distill.llm.chat = real_chat
        self.assertEqual(n, 1)
        # Their file is untouched — only their instance may retire it.
        theirs = (other.memory_dir(self.proj) / stale.filename()).read_text()
        self.assertNotIn("status: superseded", theirs)
        # The claim is recorded on our own memory, and survives a round-trip.
        mine = list(self.store.iter_memories(self.proj))
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].supersedes_external,
                         [f"{other.id}:{stale.filename()}"])

    def test_several_foreign_claims_are_all_kept(self):
        # A single field would silently drop every claim after the first.
        from foldcrumbs import distill
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        a = self.federation.register(self._root(".claude-work"))
        b = self.federation.register(self._root(".claude-peo"))
        s1 = self._write(a, "Deploy deferred", "Deploying is deferred for now.",
                         type_="decision")
        s2 = self._write(b, "Deploy postponed", "Deploying is postponed.",
                         type_="decision")
        fresh = MemoryRecord(title="Deployed",
                             content="Deploying is done and released now.",
                             type="fact")
        real_chat = distill.llm.chat
        distill.llm.chat = lambda *a, **k: '{"supersedes": true}'
        try:
            distill._auto_supersede([fresh], self.proj)
        finally:
            distill.llm.chat = real_chat
        claims = set(list(self.store.iter_memories(self.proj))[0].supersedes_external)
        self.assertEqual(claims, {f"{a.id}:{s1.filename()}",
                                  f"{b.id}:{s2.filename()}"})

    def test_claims_are_not_attributed_by_label(self):
        # Two instances can share a label: "~/a/.claude" and "~/b/.claude" are
        # both "claude". Keying on the label would contest the wrong memory.
        from foldcrumbs import index_shard
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        (self._home / "a").mkdir(exist_ok=True)
        (self._home / "b").mkdir(exist_ok=True)
        one = self.federation.register(self._home / "a" / ".claude")
        two = self.federation.register(self._home / "b" / ".claude")
        self.assertEqual(one.label, two.label)     # the ambiguity is real
        target = self._write(two, "Deploy Mondays", "Deploys run Mondays.")
        claim = MemoryRecord(title="Moved to Friday", content="Now Fridays.",
                             type="fact")
        claim.supersedes_external = [f"{two.id}:{target.filename()}"]
        self.store.write_memory(claim, self.proj)
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        for ref in (one, two):
            (d / f"{ref.id}.json").write_text(json.dumps({
                "root_id": ref.id, "version": index_shard.SHARD_VERSION,
                "label": ref.label,
                "memory_dir": str(ref.memory_dir(self.proj)), "entries": [
                    {"filename": target.filename(), "type": "fact",
                     "title": f"Deploy Mondays ({ref.id[:4]})",
                     "path": "/abs/x.md",
                     "created_at": "2026-01-01T00:00:00+00:00"}]}),
                encoding="utf-8")
        block = index_shard.render_block(self.proj, ["fact"])
        # Exactly one of the two same-named roots is contested — the right one.
        self.assertEqual(block.count("your store records this as obsolete"), 1)
        contested_line = [ln for ln in block.splitlines()
                          if "records this as obsolete" in ln][0]
        self.assertIn(two.id[:4], contested_line)

    def test_federated_view_shows_a_contested_entry(self):
        from foldcrumbs import index_shard
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "Deploy on Mondays", "Deploys run Mondays.")
        claim = MemoryRecord(title="Deploys moved to Friday",
                             content="Deploys now run on Fridays.", type="fact")
        claim.supersedes_external = [f"{other.id}:{stale.filename()}"]
        self.store.write_memory(claim, self.proj)
        self.store.rebuild_index(self.proj)
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps({
            "root_id": other.id, "version": index_shard.SHARD_VERSION,
            "label": "claude-work",
            "memory_dir": str(other.memory_dir(self.proj)), "entries": [
                {"filename": stale.filename(), "type": "fact",
                 "title": "Deploy on Mondays", "path": "/abs/x.md",
                 "created_at": "2026-01-01T00:00:00+00:00"}]}),
            encoding="utf-8")
        block = index_shard.render_block(self.proj, ["fact"])
        self.assertIn("your store records this as obsolete", block)
        self.assertIn("Deploys moved to Friday", block)

    def test_answer_attributes_foreign_memories(self):
        # Without the label the model can present another instance's
        # conclusion as this store's own.
        from foldcrumbs import cli, llm
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        seen = {}
        real = llm.chat

        def capture(messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return "ok"

        llm.chat = capture
        cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            import argparse
            cli._cmd_answer(argparse.Namespace(question="when do deploys run",
                                               limit=5))
        finally:
            llm.chat = real
            os.chdir(cwd)
        self.assertIn("from claude-work", seen["prompt"])
        self.assertIn("read-only", seen["prompt"])

    def test_recall_hides_a_memory_this_store_declared_obsolete(self):
        # The claim was resolved only in the injected block, so CLI/MCP recall
        # could still hand back the very decision this store superseded.
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "Deploy on Mondays", "Deploys run Mondays only.")
        claim = MemoryRecord(title="Deploys moved to Friday",
                             content="Deploys run Fridays now.", type="fact")
        claim.supersedes_external = [f"{other.id}:{stale.filename()}"]
        self.store.write_memory(claim, self.proj)
        titles = [h.title for h in self.store.search("deploys run", cwd=self.proj)]
        self.assertIn("Deploys moved to Friday", titles)
        self.assertNotIn("Deploy on Mondays", titles)
        every = self.store.search("deploys run", cwd=self.proj,
                                  include_contested=True)
        contested = [h for h in every if h.title == "Deploy on Mondays"]
        self.assertEqual(len(contested), 1)
        self.assertEqual(contested[0].contested_by, "Deploys moved to Friday")

    def test_a_slow_foreign_store_cannot_hang_recall(self):
        # The probe only stats the root; the listing and the reads after it
        # can block just as long on the same mount.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Slow", "From a store that stalls.")
        real = self.store.iter_memories_in
        release = _t.Event()

        def crawling(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                release.wait(5)      # interruptible: no thread outlives the test
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = crawling
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.1
        try:
            start = time.monotonic()
            got = list(self.store.iter_federated(self.proj))
            elapsed = time.monotonic() - start
        finally:
            self._drain_scans(release)
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
        self.assertLess(elapsed, 3)
        self.assertEqual(got, [])   # nothing from it, but recall came back

    def test_a_stuck_scan_is_not_restarted_on_every_recall(self):
        # The thread blocked on a hung mount cannot be killed; starting
        # another one per recall stacks them for as long as the mount is down.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Slow", "From a store that stalls.")
        real = self.store.iter_memories_in
        starts, release = [], _t.Event()

        def crawling(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                starts.append(1)
                release.wait(5)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = crawling
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.1
        try:
            for _ in range(4):
                list(self.store.iter_federated(self.proj))
        finally:
            self._drain_scans(release)
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
        self.assertEqual(len(starts), 1, "a scan thread per recall")

    def test_concurrent_recalls_start_one_scan_between_them(self):
        # Check-then-act: two recalls both seeing no live worker each start
        # their own, which is the leak the slot is meant to prevent.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Slow", "From a store that stalls.")
        real = self.store.iter_memories_in
        starts, lock, release = [], _t.Lock(), _t.Event()

        def crawling(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                with lock:
                    starts.append(1)
                release.wait(3)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = crawling
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.1
        barrier = _t.Barrier(4)

        def recall():
            barrier.wait()
            list(self.store.iter_federated(self.proj))

        try:
            threads = [_t.Thread(target=recall) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        finally:
            self._drain_scans(release)
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
        self.assertEqual(len(starts), 1, "concurrent recalls each spawned a scan")

    def test_concurrent_recalls_share_a_healthy_scan(self):
        # Treating every live scan as stuck made the second caller return
        # nothing from a root whose scan was perfectly fine.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Shared", "Visible to both callers.")
        real = self.store.iter_memories_in

        def unhurried(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                time.sleep(0.2)          # slow, but well inside the timeout
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = unhurried
        results, barrier = [], _t.Barrier(3)

        def recall():
            barrier.wait()
            results.append([r.title for r in self.store.iter_federated(self.proj)])

        try:
            threads = [_t.Thread(target=recall) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        finally:
            self.store.iter_memories_in = real
            self._drain_scans()
        self.assertEqual(len(results), 3)
        for got in results:
            self.assertEqual(got, ["Shared"], "a concurrent caller lost the root")

    def test_a_paused_recall_does_not_block_another(self):
        # A yield inside the `with` suspends the generator while still holding
        # the lock. Abandoning it is harmless — close() releases it — but a
        # caller that merely consumes slowly holds it for the whole time, and
        # every other recall, plus the scan thread's own appends, wait on it.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        for i in range(3):
            self._write(other, f"M{i}", f"Body {i}.")
        paused = self.store.iter_federated(self.proj)
        self.assertIsNotNone(next(paused))   # suspended mid-iteration, alive
        done = _t.Event()

        def second_recall():
            list(self.store.iter_federated(self.proj))
            done.set()

        t = _t.Thread(target=second_recall, daemon=True)
        t.start()
        try:
            self.assertTrue(done.wait(5), "a paused recall held the scan lock")
        finally:
            paused.close()

    def test_a_hung_root_is_not_rescanned_once_per_project(self):
        # The slot is per project so results never cross, but stuck-ness
        # belongs to the root: without a root-level check, every project a
        # long-lived process touches starts another unkillable thread.
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        projects = [self._home / f"p{i}" for i in range(4)]
        for proj in projects:
            proj.mkdir(parents=True, exist_ok=True)
            d = other.memory_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title="X", content="Body.")
            (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        import threading as _t
        real = self.store.iter_memories_in
        starts, release = [], _t.Event()

        def hung(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                starts.append(1)
                release.wait(5)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = hung
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.1
        try:
            for proj in projects:
                list(self.store.iter_federated(proj))
        finally:
            self._drain_scans(release)
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
        self.assertEqual(len(starts), 1, "a blocked thread per project")

    def test_the_root_gate_remembers_every_blocked_worker(self):
        # Two projects can time out on one hung mount at once. A single slot
        # let the second overwrite the first, so the gate reopened as soon as
        # the remembered thread died — while the forgotten one still hung.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "X", "Body.")
        release = _t.Event()
        still_hanging = _t.Thread(target=release.wait, daemon=True)
        finished = _t.Thread(target=lambda: None)
        still_hanging.start()
        finished.start()
        finished.join(2)
        # Order matters: the live one was recorded first and must not be lost.
        # Built with the same helper the code uses: a hand-spelled key stops
        # matching the moment the gate's shape changes, and the test then
        # passes without gating anything.
        self.store._stuck_roots[self.store._gate_key(other)] = [
            still_hanging, finished]
        started = []
        real = self.store.iter_memories_in

        def counting(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                started.append(1)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = counting
        try:
            self.assertEqual(list(self.store.iter_federated(self.proj)), [])
            self.assertEqual(started, [], "started a scan while one was blocked")
        finally:
            self.store.iter_memories_in = real
            release.set()
            self.store._stuck_roots.clear()
            self.store._pending_scans.clear()

    def test_a_mode_change_reopens_the_gate_for_the_new_layout(self):
        # A mode change leaves the root's id *and* its path untouched while
        # moving its memory somewhere else entirely. The old layout's blocked
        # worker therefore went on gating a directory it had never read, and
        # the new layout stayed invisible for the life of the process.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        root = self._root(".claude-work")
        config_ref = self.federation.register(root, mode="config")
        release = _t.Event()
        hung = _t.Thread(target=release.wait, daemon=True)
        hung.start()
        self.store._stuck_roots[self.store._gate_key(config_ref)] = [hung]

        # The *same* path, in explicit mode: id and path both unchanged, only
        # the memory directory moves. Registering elsewhere would have changed
        # the path too, and the gate would have reopened for that reason alone
        # — the test would then pass without testing anything.
        rec = MemoryRecord(title="Moved", content="Body.", type="fact")
        (root / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        # The supported sequence: reinterpreting a root's mode in place is
        # refused until the removal is on record.
        self.federation.unregister(config_ref.id)
        moved = self.federation.register(root, mode="explicit")
        self.assertEqual(moved.id, config_ref.id, "re-add minted a new id")
        self.assertEqual(moved.path, config_ref.path, "the path moved too")
        try:
            titles = [m.title
                      for m in self.store.iter_federated(self.proj)]
        finally:
            release.set()
            hung.join(10)
            self._drain_scans()
        self.assertIn("Moved", titles,
                      "the new layout stayed gated by the old one's worker")

    def test_concurrent_projects_share_one_worker_on_the_same_root(self):
        # Each project needs its *own* results, so it cannot join another's
        # scan — but it must not start a second thread against the same mount
        # either. It waits for the root, then scans it.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        projects = [self._home / f"c{i}" for i in range(4)]
        for i, proj in enumerate(projects):
            proj.mkdir(parents=True, exist_ok=True)
            d = other.memory_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title=f"P{i}", content=f"Body {i}.")
            (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        real = self.store.iter_memories_in
        live, peak, lock = 0, [0], _t.Lock()

        def tracked(directory, max_files=None):
            nonlocal live
            if str(directory).startswith(str(other.path)):
                with lock:
                    live += 1
                    peak[0] = max(peak[0], live)
                time.sleep(0.15)
                with lock:
                    live -= 1
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = tracked
        results, barrier = {}, _t.Barrier(len(projects))

        def recall(i, proj):
            barrier.wait()
            results[i] = [r.title for r in self.store.iter_federated(proj)]

        try:
            threads = [_t.Thread(target=recall, args=(i, p))
                       for i, p in enumerate(projects)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
        finally:
            self.store.iter_memories_in = real
            self._drain_scans()
        self.assertEqual(peak[0], 1, "two scans ran against one root at once")
        for i in range(len(projects)):
            self.assertEqual(results.get(i), [f"P{i}"],
                             "a project lost its own root")

    def test_one_root_costs_at_most_one_timeout_in_total(self):
        # Waiting for the root and scanning it are both part of what the root
        # is allowed to cost. The doubling only shows when the root frees up
        # *before* the deadline: the waiter then starts its own scan, and with
        # a fresh budget that scan gets the full timeout all over again.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        proj_b = self._home / "cost-b"
        proj_b.mkdir(parents=True, exist_ok=True)
        for proj in (self.proj, proj_b):
            d = other.memory_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title="X", content="Body.")
            (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        dir_a = str(other.memory_dir(self.proj))
        dir_b = str(other.memory_dir(proj_b))
        real = self.store.iter_memories_in
        holding, release = _t.Event(), _t.Event()

        def paced(directory, max_files=None):
            if str(directory) == dir_a:
                holding.set()
                time.sleep(0.25)      # frees the root before the deadline
            elif str(directory) == dir_b:
                # Interruptible: a bare sleep would leave this thread running
                # long after the test, skewing whatever timing test runs next.
                release.wait(30)      # then B's own scan hangs
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = paced
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.4
        try:
            first = _t.Thread(
                target=lambda: list(self.store.iter_federated(self.proj)),
                daemon=True)
            first.start()
            self.assertTrue(holding.wait(2))
            start_t = time.monotonic()
            list(self.store.iter_federated(proj_b))
            elapsed = time.monotonic() - start_t
        finally:
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
            self._drain_scans(release)
        self.assertLess(elapsed, 0.4 * 1.4, "the root cost two full timeouts")

    def test_a_capped_scan_says_what_it_left_out(self):
        # A capped scan that stays quiet is indistinguishable from a store
        # with no matches — the same silent-truncation trap the rendered
        # block avoids.
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        d = other.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        cap = self.store._MAX_FEDERATED_SCAN
        for i in range(cap + 7):
            rec = MemoryRecord(title=f"M{i:04d}", content=f"Body {i}.")
            (d / f"fact_m{i:04d}.md").write_text(rec.to_markdown(),
                                                 encoding="utf-8")
        logged = []
        real_log = self.config.log_event
        self.config.log_event = lambda m: logged.append(m)
        try:
            got = list(self.store.iter_federated(self.proj))
        finally:
            self.config.log_event = real_log
            self._drain_scans()
        self.assertLessEqual(len(got), cap)
        self.assertTrue(any(f"only {cap} of {cap + 7}" in m for m in logged),
                        f"no truncation warning in {logged}")

    def test_finished_scans_are_reaped_from_every_project_slot(self):
        # A scan that timed out and later completed leaves its records and a
        # dead thread behind. Reaping only the requested slot means one such
        # entry accumulates for every project the process ever timed out on.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        projects = [self._home / f"reap{i}" for i in range(3)]
        for proj in projects:
            proj.mkdir(parents=True, exist_ok=True)
            d = other.memory_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title="X", content="Body.")
            (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        real = self.store.iter_memories_in
        release = _t.Event()

        def slow(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                release.wait(10)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = slow
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.05
        try:
            list(self.store.iter_federated(projects[0]))   # times out
            self.assertEqual(len(self.store._pending_scans), 1)
            release.set()                                  # it finishes late
            for t in list(self.store._pending_scans.values()):
                t["thread"].join(5)
            self.store._stuck_roots.clear()                # mount recovered
            list(self.store.iter_federated(projects[1]))   # a different slot
            self.assertEqual(len(self.store._pending_scans), 0,
                             "a dead slot survived a later recall")
        finally:
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
            self._drain_scans(release)

    def test_a_relocated_root_is_not_gated_by_its_old_path(self):
        # Ids survive a move by design, so a gate keyed on the id alone kept
        # skipping the root at its new, healthy path while the old path's
        # worker stayed blocked. Driven through the real flow: seeding the gate
        # by hand would only ever match the key format being tested.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        old_path = self._root(".claude-work")
        other = self.federation.register(old_path)
        d = other.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title="Reachable", content="At the new path.")
        (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")

        real = self.store.iter_memories_in
        release = _t.Event()

        def hangs_at_old_path(directory, max_files=None):
            if str(directory).startswith(str(old_path)):
                release.wait(30)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = hangs_at_old_path
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.05
        try:
            self.assertEqual(list(self.store.iter_federated(self.proj)), [])
            self.assertTrue(self.store._stuck_roots, "no gate was recorded")
            # Now the root moves somewhere healthy, keeping its identity.
            self.store.iter_memories_in = real
            new_path = self._root(".claude-relocated")
            old_path.rename(new_path)
            moved = self.federation.register(new_path)
            self.assertEqual(moved.id, other.id)
            got = [r.title for r in self.store.iter_federated(self.proj)]
        finally:
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
            self._drain_scans(release)
        self.assertEqual(got, ["Reachable"], "the old path's gate hid the move")

    def test_one_blocked_thread_is_recorded_once(self):
        # Several recalls for the same project share one scan and can all
        # reach the deadline together. Appending blindly makes one blocked
        # thread look like many, in the list and in what the log reports.
        import threading as _t
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Slow", "From a store that stalls.")
        real = self.store.iter_memories_in
        release = _t.Event()

        def hangs(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                release.wait(30)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = hangs
        timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 0.1
        barrier = _t.Barrier(4)

        def recall():
            barrier.wait()
            list(self.store.iter_federated(self.proj))

        try:
            threads = [_t.Thread(target=recall) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            recorded = [t for ts in self.store._stuck_roots.values() for t in ts]
            self.assertEqual(len(recorded), 1, f"one thread recorded {len(recorded)}x")
        finally:
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = timeout
            self._drain_scans(release)

    def test_a_claim_written_during_a_scan_is_honoured_by_the_joiner(self):
        # The shared scan used to bake in the starting caller's claims. The
        # joiner has to be *inside* the same in-flight scan for that to show:
        # once it finishes, the slot is reaped and a later recall simply scans
        # again with fresh claims.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "Deploy on Mondays", "Deploys run Mondays.")
        real = self.store.iter_memories_in
        scanning, proceed = _t.Event(), _t.Event()
        starts, lock = [], _t.Lock()

        def slow(directory, max_files=None):
            if str(directory).startswith(str(other.path)):
                with lock:
                    starts.append(1)
                scanning.set()
                proceed.wait(5)
            yield from real(directory, max_files=max_files)

        # Observe the join instead of sleeping towards it: a sleep that runs
        # short makes the second caller start its own scan, and the assertion
        # below would then accuse correct code of not sharing.
        attached = _t.Event()
        joiner_name = "joiner-thread"

        class WatchedScans(dict):
            def get(self, key, default=None):
                found = super().get(key, default)
                if (found is not None
                        and _t.current_thread().name == joiner_name):
                    attached.set()
                return found

        self.store.iter_memories_in = slow
        real_scans = self.store._pending_scans
        self.store._pending_scans = WatchedScans(real_scans)
        # The scan must stay in flight for the whole coordination window. At
        # the default two seconds a loaded machine can time it out mid-test:
        # the root then lands in the stuck gate, the joiner skips it, and
        # correct code fails. The window is bounded by the waits below, so a
        # generous ceiling here cannot make the test hang.
        real_timeout = self.store._FEDERATED_SCAN_TIMEOUT
        self.store._FEDERATED_SCAN_TIMEOUT = 30.0
        first, joined = [], []
        try:
            a = _t.Thread(target=lambda: first.extend(
                self.store.iter_federated(self.proj)), daemon=True)
            a.start()
            reached_scan = scanning.wait(2)
            # Recorded while that scan is still running...
            claim = MemoryRecord(title="Moved to Friday",
                                 content="Deploys run Fridays now.")
            claim.supersedes_external = [f"{other.id}:{stale.filename()}"]
            self.store.write_memory(claim, self.proj)
            # ...and this caller joins that same scan, not a new one.
            b = _t.Thread(target=lambda: joined.extend(
                self.store.iter_federated(self.proj)),
                name=joiner_name, daemon=True)
            b.start()
            # Only one thing here is a scheduling accident: the first scan
            # never starting at all. Past that point the first caller is
            # provably still blocked — this test holds `proceed` — so a joiner
            # that fails to attach is not late, it is not sharing, and that is
            # a regression this test exists to catch. Skipping both cases alike
            # would let the sharing path be removed without a single failure.
            if not reached_scan:
                proceed.set()
                a.join(5)
                b.join(5)
                self.skipTest("the first scan never started on this run")
            did_attach = attached.wait(5)
            proceed.set()
            a.join(5)
            b.join(5)
            self.assertTrue(
                did_attach,
                "the joiner did not attach while the scan was provably still "
                "running: the shared-scan path is gone")
        finally:
            self.store.iter_memories_in = real
            self.store._FEDERATED_SCAN_TIMEOUT = real_timeout
            proceed.set()
            self.store._pending_scans = real_scans
            self._drain_scans()
        # Proof the second caller *joined* rather than scanning on its own:
        # a scan of its own would show up here as a second start, and the test
        # would then prove nothing about sharing. Sleeps cannot establish this;
        # the counter can.
        self.assertEqual(len(starts), 1, "the second recall ran its own scan")
        self.assertEqual([r.contested_by for r in joined],
                         ["Moved to Friday"], "the joiner got a stale marking")
        self.assertEqual([r.contested_by for r in first], [None])

    def test_dead_entries_are_reaped_even_for_roots_no_longer_visited(self):
        # Reaping inside the loop never reached an entry whose root had since
        # become unavailable — those are skipped earlier — or been
        # unregistered, which drops it from the loop entirely. Its dead thread
        # and its records then stayed for the life of the process.
        import shutil
        import threading as _t
        gone = self.federation.register(self._root(".claude-work"))
        removed = self.federation.register(self._root(".claude-peo"))
        finished = _t.Thread(target=lambda: None)
        finished.start()
        finished.join(2)
        for ref in (gone, removed):
            from foldcrumbs import index_shard
            slot = (ref.id, str(ref.memory_dir(self.proj)),
                    index_shard.project_key(self.proj))
            self.store._pending_scans[slot] = {
                "thread": finished, "results": [object()] * 3,
                "timed_out": True}
            self.store._stuck_roots[self.store._gate_key(ref)] = [finished]
        shutil.rmtree(gone.path)                 # unavailable: skipped early
        self.federation.unregister(removed.id)   # unregistered: never listed
        list(self.store.iter_federated(self.proj))
        self.assertEqual(self.store._pending_scans, {},
                         "dead scans survived for roots the loop never reached")
        self.assertEqual(self.store._stuck_roots, {})

    def test_a_shard_from_an_old_layout_is_not_served(self):
        # Changing a root's mode moves its whole memory directory. Shards
        # already published for other projects keep the old one, with absolute
        # paths to match, and were accepted on root id alone — serving stale
        # paths until each project happened to republish, which for an old
        # project may be never.
        from foldcrumbs import index_shard
        other = self.federation.register(self._root(".claude-work"),
                                         mode="config")
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps({
            "root_id": other.id, "version": index_shard.SHARD_VERSION,
            "label": other.label,
            "memory_dir": str(self._home / "somewhere-else"),
            "entries": [{"filename": "a.md", "type": "fact", "title": "Old",
                         "path": "/old/layout/a.md",
                         "created_at": "2026-01-01T00:00:00+00:00"}]}),
            encoding="utf-8")
        self.assertEqual(index_shard.read_shards(self.proj), [],
                         "a shard describing another layout was served")
        # The same shard, describing where the root actually keeps this
        # project's memory, is accepted.
        data = json.loads((d / f"{other.id}.json").read_text())
        data["memory_dir"] = str(other.memory_dir(self.proj))
        (d / f"{other.id}.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(len(index_shard.read_shards(self.proj)), 1)

    def test_a_mode_change_clears_shards_of_every_project(self):
        # Refusing a stale shard is permanent for a project that never opens
        # again: it sits there rejected and logged forever. The change that
        # invalidates them is the moment to drop them.
        from foldcrumbs import index_shard
        other = self.federation.register(self._root(".claude-work"),
                                         mode="config")
        projects = [self._home / f"drop{i}" for i in range(3)]
        for proj in projects:
            d = index_shard.shards_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{other.id}.json").write_text(json.dumps({
                "root_id": other.id, "version": index_shard.SHARD_VERSION,
                "label": other.label,
                "memory_dir": str(other.memory_dir(proj)),
                "entries": []}), encoding="utf-8")
        self.assertEqual(
            len(list((self.config.STATE_DIR / "projects").glob(
                f"*/roots/{other.id}.json"))), 3)
        self.federation.unregister(other.id)          # consent for the change
        moved = self.federation.register(self._root(".claude-work"),
                                         mode="explicit")
        self.assertEqual(moved.mode, "explicit")
        # Every shard described the old config layout, so none survive.
        self.assertEqual(
            list((self.config.STATE_DIR / "projects").glob(
                f"*/roots/{other.id}.json")), [],
            "shards from the old layout were left to be refused forever")

    def test_cleanup_does_not_delete_a_fresh_publication(self):
        # Reading a shard then unlinking it is a check-then-act: a project
        # publishing a fresh shard in that window had it deleted as though it
        # were the old one. The cleanup takes the same lock write_shard does.
        import contextlib as _c
        from foldcrumbs import index_shard
        other = self.federation.register(self._root(".claude-work"),
                                         mode="config")
        proj = self._home / "racing"
        d = index_shard.shards_dir(proj)
        d.mkdir(parents=True, exist_ok=True)
        shard = d / f"{other.id}.json"
        stale = {"root_id": other.id, "version": index_shard.SHARD_VERSION,
                 "label": other.label,
                 "memory_dir": str(self._home / "old-layout"), "entries": []}
        shard.write_text(json.dumps(stale), encoding="utf-8")
        real = self.federation.file_lock
        fresh = dict(stale, memory_dir=str(other.memory_dir(proj)),
                     entries=[{"filename": "new.md"}])

        @_c.contextmanager
        def publish_underneath(path, allow_unsupported=False):
            with real(path, allow_unsupported=allow_unsupported) as held:
                if held and path.name == f".lock-{other.id}":
                    # What a concurrent publication would have written.
                    shard.write_text(json.dumps(fresh), encoding="utf-8")
                yield held

        self.federation.file_lock = publish_underneath
        try:
            index_shard.drop_stale_shards(other)
        finally:
            self.federation.file_lock = real
        self.assertTrue(shard.is_file(), "a fresh publication was deleted")
        self.assertEqual(json.loads(shard.read_text())["entries"],
                         [{"filename": "new.md"}])

    def test_a_federated_recall_reads_the_local_store_once(self):
        # search() scored the local store, then the federated pass resolved
        # this store's claims by listing and parsing every local file again.
        # Each recall paid for the local store twice, and the foreign-scan
        # timeout does not bound that second pass.
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude-work"), mode="config")
        for i in range(3):
            self.store.write_memory(
                MemoryRecord(title=f"local {i}", content=f"body {i}",
                             type="fact"), cwd=str(self.proj))
        # Counted at the one place that touches the directory, so the test
        # keeps measuring listings even if the callers above it change.
        reads, real = [], self.store.scan_store
        local_dir = self.store.config.memory_dir(str(self.proj))

        def counting(directory, max_files=None):
            if Path(directory) == local_dir:
                reads.append(1)
            return real(directory, max_files)

        self.store.scan_store = counting
        try:
            self.store.search("local", cwd=str(self.proj), federated=True)
        finally:
            self.store.scan_store = real
        self.assertEqual(len(reads), 1,
                         f"local store listed {len(reads)} times for one recall")

    def test_cleanup_keeps_a_shard_published_through_an_alias(self):
        # The reader learned to recognise aliases; the cleanup still judged by
        # text, and there the verdict is an unlink. A shard freshly published
        # under the root's real path was deleted as though it belonged to an
        # abandoned layout.
        from foldcrumbs import index_shard
        real_root = self._home / "aliased-root"
        (real_root / "projects").mkdir(parents=True, exist_ok=True)
        alias = self._home / "cleanup-alias"
        os.symlink(real_root, alias)
        ref = self.federation.register(alias, mode="config")
        proj = self._home / "aliased-project"
        d = index_shard.shards_dir(proj)
        d.mkdir(parents=True, exist_ok=True)
        shard = d / f"{ref.id}.json"
        # Same directory as ref.memory_dir(proj), spelled through the real path.
        through_real = Path(str(ref.memory_dir(proj)).replace(
            str(alias), str(real_root), 1))
        through_real.mkdir(parents=True, exist_ok=True)
        shard.write_text(json.dumps(
            {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
             "label": ref.label, "memory_dir": str(through_real),
             "entries": [{"filename": "kept.md"}]}), encoding="utf-8")
        index_shard.drop_stale_shards(ref)
        self.assertTrue(shard.is_file(),
                        "a shard published through the root's real path was "
                        "deleted as stale")

        # A directory that is genuinely outside the root is still dropped.
        elsewhere = self._home / "somewhere-else" / "memory"
        elsewhere.mkdir(parents=True, exist_ok=True)
        shard.write_text(json.dumps(
            {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
             "label": ref.label, "memory_dir": str(elsewhere),
             "entries": []}), encoding="utf-8")
        index_shard.drop_stale_shards(ref)
        self.assertFalse(shard.is_file(), "a truly stale shard survived")

    def test_cleanup_drops_what_is_gone_but_not_what_it_cannot_read(self):
        # These two are not the same fact. A directory that is *gone* proves
        # the layout is dead; one the filesystem declines to describe — on a
        # stalled mount, unreadable — proves nothing, and treating it as gone
        # would let a slow disk look like a deletion and delete real shards.
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"),
                                       mode="config")
        d = index_shard.shards_dir(self._home / "judged")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / f"{ref.id}.json"

        def put(memory_dir):
            shard.write_text(json.dumps(
                {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
                 "label": ref.label, "memory_dir": str(memory_dir),
                 "entries": []}), encoding="utf-8")

        put(self._home / "never-existed" / "memory")
        index_shard.drop_stale_shards(ref)
        self.assertFalse(shard.is_file(),
                         "a shard for a directory that is gone was kept")

        opaque = self._home / "unreadable"
        gone_quiet = opaque / "memory"
        gone_quiet.mkdir(parents=True, exist_ok=True)
        put(gone_quiet)
        # Only this subtree refuses to be described — the root itself must
        # still answer, or the shard would survive for the wrong reason.
        real_stat = os.stat

        def opaque_stat(path, *a, **k):
            if str(path).startswith(str(opaque)):
                raise OSError(errno.EIO, "I/O error")
            return real_stat(path, *a, **k)

        os.stat = opaque_stat
        try:
            index_shard.drop_stale_shards(ref)
        finally:
            os.stat = real_stat
        self.assertTrue(shard.is_file(),
                        "a shard was deleted on an answer nobody gave")

    def test_cleanup_keeps_a_shard_whose_probe_never_answered(self):
        # A probe that runs out of time is the same non-answer as an
        # unreadable directory, and must not be read as "gone" either — a
        # stalled mount would otherwise delete every shard published under it.
        import threading as _t
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"),
                                       mode="config")
        d = index_shard.shards_dir(self._home / "timed-out")
        d.mkdir(parents=True, exist_ok=True)
        shard = d / f"{ref.id}.json"
        stalled = self._home / "stalled"
        (stalled / "memory").mkdir(parents=True, exist_ok=True)
        shard.write_text(json.dumps(
            {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
             "label": ref.label, "memory_dir": str(stalled / "memory"),
             "entries": []}), encoding="utf-8")
        release = _t.Event()
        real_stat = os.stat

        def hangs(path, *a, **k):
            if str(path).startswith(str(stalled)):
                release.wait(30)
            return real_stat(path, *a, **k)

        probe_timeout = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.05
        os.stat = hangs
        try:
            index_shard.drop_stale_shards(ref)
        finally:
            os.stat = real_stat
            self.federation._REGISTRY_PROBE_TIMEOUT = probe_timeout
            release.set()
        self.assertTrue(shard.is_file(),
                        "a shard was deleted because a probe timed out")

    def test_a_stalled_mount_costs_the_cleanup_one_timeout_not_one_each(self):
        # Every probe is bounded, but the bound is per call. Judging each shard
        # on its own paid that bound again for every project of the root and
        # left another unkillable os.stat thread behind, so a single stalled
        # mount cost N seconds and N threads instead of one.
        import threading as _t
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"),
                                       mode="config")
        stalled = self._home / "stalled-mount"
        (stalled / "memory").mkdir(parents=True, exist_ok=True)
        shards = []
        for i in range(5):
            d = index_shard.shards_dir(self._home / f"p{i}")
            d.mkdir(parents=True, exist_ok=True)
            shard = d / f"{ref.id}.json"
            # A *distinct* directory each, all under the one hung mount: with
            # a shared path the per-path memo alone would hide the cost, and
            # the test would not be measuring the end-on-first-stall at all.
            shard.write_text(json.dumps(
                {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
                 "label": ref.label,
                 "memory_dir": str(stalled / f"m{i}" / "memory"),
                 "entries": []}), encoding="utf-8")
            shards.append(shard)
        release = _t.Event()
        hung, lock = [], _t.Lock()
        real_stat = os.stat

        def hangs(path, *a, **k):
            if str(path).startswith(str(stalled)):
                with lock:
                    hung.append(1)
                release.wait(30)
            return real_stat(path, *a, **k)

        probe_timeout = self.federation._REGISTRY_PROBE_TIMEOUT
        self.federation._REGISTRY_PROBE_TIMEOUT = 0.2
        os.stat = hangs
        try:
            start = time.monotonic()
            index_shard.drop_stale_shards(ref)
            elapsed = time.monotonic() - start
        finally:
            os.stat = real_stat
            self.federation._REGISTRY_PROBE_TIMEOUT = probe_timeout
            release.set()
        self.assertEqual(len(hung), 1,
                         f"{len(hung)} blocked probes for {len(shards)} shards")
        self.assertLess(elapsed, 0.2 * len(shards),
                        "the cleanup paid the timeout once per shard")
        self.assertTrue(all(s.is_file() for s in shards),
                        "shards were dropped on an answer nobody gave")

    def test_the_root_describes_itself_once_per_cleanup(self):
        # Judging each shard on its own re-read the root's identity every time.
        # That read is a bounded probe: on a slow root it cost its timeout once
        # per project, and this is the guard that stops it — separate from
        # ending the sweep when some *other* path stalls.
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"),
                                       mode="config")
        shards = []
        for i in range(5):
            d = index_shard.shards_dir(self._home / f"q{i}")
            d.mkdir(parents=True, exist_ok=True)
            shard = d / f"{ref.id}.json"
            # Outside the root, so every one is nominated and reaches the
            # filesystem check — the root answers normally throughout.
            shard.write_text(json.dumps(
                {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
                 "label": ref.label,
                 "memory_dir": str(self._home / f"gone{i}" / "memory"),
                 "entries": []}), encoding="utf-8")
            shards.append(shard)
        looked, real_stat = [], os.stat

        def counting(path, *a, **k):
            if str(path) == str(ref.path):
                looked.append(1)
            return real_stat(path, *a, **k)

        os.stat = counting
        try:
            index_shard.drop_stale_shards(ref)
        finally:
            os.stat = real_stat
        self.assertEqual(len(looked), 1,
                         f"the root described itself {len(looked)} times for "
                         f"{len(shards)} shards")
        self.assertFalse(any(s.is_file() for s in shards),
                         "the stale shards were not dropped")

    def test_departure_cleanup_spares_a_root_that_has_come_back(self):
        # The departure runs on a thread the relocation stopped waiting for,
        # so it can reach this cleanup long afterwards — by which time the
        # root may have returned to that registry and republished there.
        # Deleting then would destroy a *fresh* shard, not a left-behind one.
        import contextlib as _c
        from foldcrumbs import index_shard
        ref = self.federation.register(self._root(".claude-work"))
        left = self._home / "left-registry"
        (left / "roots").mkdir(parents=True, exist_ok=True)
        d = left / "projects" / "alpha" / "roots"
        d.mkdir(parents=True, exist_ok=True)
        shard = d / f"{ref.id}.json"

        def put(entries):
            shard.write_text(json.dumps(
                {"root_id": ref.id, "version": index_shard.SHARD_VERSION,
                 "label": ref.label, "memory_dir": str(ref.memory_dir("alpha")),
                 "entries": entries}), encoding="utf-8")

        put([{"filename": "before-the-move.md"}])
        real = self.federation.file_lock

        @_c.contextmanager
        def returns_underneath(path, allow_unsupported=False):
            with real(path, allow_unsupported=allow_unsupported) as held:
                if held and path.name == f".lock-{ref.id}":
                    # Proves the interleaving rather than hoping for it: the
                    # return lands while the cleanup holds the shard lock,
                    # which is the only window where it could be destroyed.
                    (left / "roots" / f"{ref.id}.json").write_text(json.dumps(
                        {"id": ref.id, "path": str(ref.path), "mode": "config",
                         "label": ref.label}), encoding="utf-8")
                    put([{"filename": "republished.md"}])
                yield held

        self.federation.file_lock = returns_underneath
        try:
            dropped = index_shard.drop_root_shards_in(left, ref.id)
        finally:
            self.federation.file_lock = real
        self.assertTrue(shard.is_file(), "deleted a fresh republication")
        self.assertEqual(json.loads(shard.read_text())["entries"],
                         [{"filename": "republished.md"}])
        self.assertEqual(dropped, 0)

    def test_a_shard_written_through_an_alias_is_still_served(self):
        # Paths are stored unresolved on purpose, so a root reached through a
        # symlink yields a different string for the same directory. Comparing
        # text alone rejected its shards for good: publishing through the alias
        # kept producing the same rejected value.
        from foldcrumbs import index_shard
        real_root = self._home / "real-root"
        (real_root / "projects").mkdir(parents=True, exist_ok=True)
        alias = self._home / "alias-root"
        os.symlink(real_root, alias)
        ref = self.federation.register(alias)          # registered via alias
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        # A shard recorded through the *real* path: same directory, other name.
        via_real = str(real_root / "projects"
                       / self.config.encode_cwd(self.proj) / "memory")
        via_real_path = Path(via_real)
        via_real_path.mkdir(parents=True, exist_ok=True)
        self.assertNotEqual(via_real, str(ref.memory_dir(self.proj)))
        (d / f"{ref.id}.json").write_text(json.dumps({
            "root_id": ref.id, "version": index_shard.SHARD_VERSION,
            "label": ref.label, "memory_dir": via_real,
            "entries": [{"filename": "a.md", "type": "fact", "title": "A",
                         "path": f"{via_real}/a.md",
                         "created_at": "2026-01-01T00:00:00+00:00"}]}),
            encoding="utf-8")
        # It is the same directory, so it is served.
        self.assertEqual(len(index_shard.read_shards(self.proj)), 1,
                         "an alias of the same directory was rejected")
        # A genuinely different directory still is not.
        (d / f"{ref.id}.json").write_text(json.dumps({
            "root_id": ref.id, "version": index_shard.SHARD_VERSION,
            "label": ref.label,
            "memory_dir": str(self._home / "somewhere-else"),
            "entries": []}), encoding="utf-8")
        self.assertEqual(index_shard.read_shards(self.proj), [])

    def test_a_scan_is_never_reused_across_projects(self):
        # Keyed on the root alone, a recall for another project would join an
        # in-flight scan and be handed the wrong project's memories.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        other = self.federation.register(self._root(".claude-work"))
        proj_b = self._home / "proj-b"
        proj_b.mkdir(parents=True, exist_ok=True)
        for proj, title in ((self.proj, "Belongs to A"), (proj_b, "Belongs to B")):
            d = other.memory_dir(proj)
            d.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title=title, content=f"Memory of {title}.")
            (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")

        # Sequentially the slot is already cleared, so the bug only shows with
        # A's scan still in flight — which is exactly when B would join it.
        real = self.store.iter_memories_in
        started = _t.Event()

        def slow_for_a(directory, max_files=None):
            if str(directory) == str(other.memory_dir(self.proj)):
                started.set()
                time.sleep(0.6)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = slow_for_a
        out_a = []
        try:
            t = _t.Thread(target=lambda: out_a.extend(
                r.title for r in self.store.iter_federated(self.proj)))
            t.start()
            self.assertTrue(started.wait(2))
            b = [r.title for r in self.store.iter_federated(proj_b)]
            t.join(5)
        finally:
            self.store.iter_memories_in = real
            self._drain_scans()
        self.assertEqual(b, ["Belongs to B"], "joined another project's scan")
        self.assertEqual(out_a, ["Belongs to A"])

    def test_an_explicit_root_does_not_leak_claims_between_projects(self):
        # An explicit root serves every cwd from one fixed directory, so two
        # projects share a memory dir. Their claims are not shared, and it is
        # the claims that decide which records come back marked obsolete.
        import threading as _t
        from foldcrumbs.schema import MemoryRecord
        pinned = self._home / "pinned-store"
        pinned.mkdir(parents=True, exist_ok=True)
        other = self.federation.register(pinned, mode="explicit")
        theirs = MemoryRecord(title="Deploy on Mondays", content="Mondays only.")
        (pinned / theirs.filename()).write_text(theirs.to_markdown(),
                                                encoding="utf-8")
        proj_b = self._home / "proj-b2"
        proj_b.mkdir(parents=True, exist_ok=True)
        claim = MemoryRecord(title="Moved to Friday", content="Fridays now.")
        claim.supersedes_external = [f"{other.id}:{theirs.filename()}"]
        self.store.write_memory(claim, self.proj)   # only project A claims it

        real = self.store.iter_memories_in
        started = _t.Event()

        def slow(directory, max_files=None):
            if str(directory) == str(pinned) and not started.is_set():
                started.set()
                time.sleep(0.6)
            yield from real(directory, max_files=max_files)

        self.store.iter_memories_in = slow
        marks_a = []
        try:
            t = _t.Thread(target=lambda: marks_a.extend(
                r.contested_by for r in self.store.iter_federated(self.proj)))
            t.start()
            self.assertTrue(started.wait(2))
            b = list(self.store.iter_federated(proj_b))
            t.join(5)
        finally:
            self.store.iter_memories_in = real
            self._drain_scans()
        self.assertEqual(marks_a, ["Moved to Friday"])
        self.assertEqual([r.contested_by for r in b], [None])

    def test_iter_memories_stays_local(self):
        # The write paths are all built on it; federating it would let a
        # foreign record be validated or superseded under this root.
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(mine, "Mine", "Local body.")
        self._write(other, "Theirs", "Foreign body.")
        titles = {m.title for m in self.store.iter_memories(self.proj)}
        self.assertEqual(titles, {"Mine"})
        dup = self.store.find_duplicate(
            self._write(other, "Theirs", "Foreign body."), cwd=self.proj)
        self.assertIsNone(dup)   # never matches across roots


class TestRootsCLI(_FederationEnv):
    """`foldcrumbs roots` end-to-end, on the same isolated registry."""

    def _run(self, *argv):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_add_list_remove_roundtrip(self):
        code, out = self._run("roots", "add", str(self._root(".claude-work")))
        self.assertEqual(code, 0)
        self.assertIn("registered claude-work", out)

        _, out = self._run("roots")
        self.assertIn("claude-work", out)
        rid = [r.id for r in self.federation.iter_roots()
               if r.label == "claude-work"][0]

        code, out = self._run("roots", "remove", rid)
        self.assertEqual(code, 0)
        self.assertIn("store is untouched", out)
        _, out = self._run("roots")
        self.assertNotIn("claude-work", out)

    def test_remove_unknown_id_fails_cleanly(self):
        code, out = self._run("roots", "remove", "deadbeefdeadbeef")
        self.assertEqual(code, 1)
        self.assertIn("no registered root", out)

    def test_mode_collision_is_reported_not_raised(self):
        self._run("roots", "add", str(self._root(".claude-work")))
        code, out = self._run("roots", "add", str(self._root(".claude-work")),
                              "--mode", "explicit")
        self.assertEqual(code, 1)
        self.assertIn("refused:", out)

    def test_cli_repairs_a_missing_shard(self):
        self._run("roots", "add", str(self._root(".claude")))
        rid = self.federation.read_marker(self._root(".claude"))
        (self._state / "roots" / f"{rid}.json").unlink()
        self._run("roots")  # any command triggers the repair
        self.assertTrue((self._state / "roots" / f"{rid}.json").is_file())

    def test_cli_does_not_resurrect_a_removed_root(self):
        self._run("roots", "add", str(self._root(".claude")))
        rid = self.federation.read_marker(self._root(".claude"))
        self._run("roots", "remove", rid)
        _, out = self._run("roots")
        self.assertFalse((self._state / "roots" / f"{rid}.json").exists())
        self.assertIn("no federated roots", out)


if __name__ == "__main__":
    unittest.main()
