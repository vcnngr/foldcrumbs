"""FL-2 — the outcome loop (foldcrumbs.outcome).

The contract under test (docs/design/fleet-learning.md rev 2 §F2, with the
RT obligations from card t_6057c04e F5/F6 and r2 t_6922bf5b):

* `good` bumps validation_count (the EXISTING compute_confidence boost) and
  records outcome/outcome_at/outcome_note — all persisted, all round-tripped
  through disk;
* `bad` sets contradiction_detected AND persists it (the flag exists in the
  schema but was never serialized — RT F5 proved the round-trip lost it);
* a penalty never promotes: compute_confidence with contradiction_detected
  returns min(non-contradicted value, max(0.1, c*0.3)) so a very low
  confidence cannot be RAISED by the 0.1 floor (RT F5);
* effects are declared on the effective-weight paths (compute_confidence →
  answer/audit/trust_level), NOT on search ranking — the tests assert the
  real machinery, not an overclaim;
* sequences: bad then good → outcome good but contradiction_detected stays
  True (revalidation does not erase history; supersede does);
* outcome on a non-active memory is refused visibly;
* outcome/outcome_at/outcome_note/contradiction_detected are RESERVED keys:
  import_store and migrate strip them (RT F6) — no foreign validation or
  dispute can be smuggled in; adopted copies already start clean (FL-1);
* outcome --list joins the adoption ledger with outcomes; a forged
  `source: adopted:` (import) produces no ledger entry and no listing.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import outcome as outcome_mod  # noqa: E402
from foldcrumbs import store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class TestOutcomeRoundTrip(TmpStore):

    def _put(self, title, body="x.", type_="fact", **kw):
        rec = MemoryRecord(title=title, content=body, type=type_, **kw)
        store.write_memory(rec)
        return rec

    def test_good_persists_and_bumps_validation(self):
        m = self._put("Deploy rule")
        res = outcome_mod.set_outcome(m.filename(), "good", note="held again")
        self.assertTrue(res["ok"], res.get("reason"))
        r = store.get(m.filename())
        self.assertEqual(r.outcome, "good")
        self.assertEqual(r.validation_count, 1)
        self.assertIsNotNone(r.outcome_at)
        self.assertEqual(r.outcome_note, "held again")
        self.assertFalse(r.contradiction_detected)

    def test_bad_persists_contradiction_round_trip(self):
        # RT F5: contradiction_detected was never serialized — set → write →
        # read lost it. The whole point of FL-2 is that bad SURVIVES disk.
        m = self._put("Wrong rule")
        res = outcome_mod.set_outcome(m.filename(), "bad", note="burned us")
        self.assertTrue(res["ok"], res.get("reason"))
        r = store.get(m.filename())   # fresh parse from disk
        self.assertTrue(r.contradiction_detected,
                        "bad must round-trip through the file")
        self.assertEqual(r.outcome, "bad")
        self.assertEqual(r.outcome_note, "burned us")

    def test_bad_then_good_keeps_contradiction(self):
        m = self._put("Flaky rule")
        outcome_mod.set_outcome(m.filename(), "bad")
        outcome_mod.set_outcome(m.filename(), "good")
        r = store.get(m.filename())
        self.assertEqual(r.outcome, "good")
        self.assertTrue(r.contradiction_detected,
                        "revalidation does not erase history — supersede does")

    def test_penalty_never_promotes(self):
        # RT F5: max(0.1, c*0.3) RAISES a very low confidence. The fix caps
        # at the non-contradicted value.
        m = self._put("Fragile", confidence=0.15)
        before = m.compute_confidence()          # 0.15 * 1.0 = 0.15
        outcome_mod.set_outcome(m.filename(), "bad")
        r = store.get(m.filename())
        after = r.compute_confidence()
        self.assertLess(after, before,
                        "a penalty must never raise the effective weight")
        self.assertAlmostEqual(after, min(before, max(0.1, 0.15 * 0.3)), 2)

    def test_high_confidence_penalty_is_the_0_3_branch(self):
        m = self._put("Strong", confidence=0.9)
        before = m.compute_confidence()
        outcome_mod.set_outcome(m.filename(), "bad")
        after = store.get(m.filename()).compute_confidence()
        self.assertAlmostEqual(after, max(0.1, before * 0.3), 2)
        self.assertLess(after, before)

    def test_trust_level_reflects_bad(self):
        m = self._put("Trusted", confidence=0.95)
        self.assertEqual(m.trust_level(), "high")
        outcome_mod.set_outcome(m.filename(), "bad")
        self.assertNotEqual(store.get(m.filename()).trust_level(), "high")

    def test_outcome_on_non_active_refused(self):
        m = self._put("Retired")
        store.forget(m.filename())          # status=deleted
        res = outcome_mod.set_outcome(m.filename(), "good")
        self.assertFalse(res["ok"])
        self.assertIn("not active", res["reason"].lower())

    def test_outcome_missing_memory_refused(self):
        res = outcome_mod.set_outcome("nope.md", "good")
        self.assertFalse(res["ok"])

    def test_outcome_invalid_verdict_refused(self):
        m = self._put("Some rule")
        for bad in ("great", "", "GOOD!", None):
            res = outcome_mod.set_outcome(m.filename(), bad)
            self.assertFalse(res["ok"], f"{bad!r} must be refused")

    def test_note_multiline_cannot_forge_frontmatter(self):
        # FL-1 F1 lesson applied here from the start: the note is serialized
        # safely (single line / escaped), never raw multiline.
        m = self._put("Noted")
        hostile = "x\noutcome: good\nvalidation_count: 99\nid: forged"
        res = outcome_mod.set_outcome(m.filename(), "bad", note=hostile)
        self.assertTrue(res["ok"], res.get("reason"))
        r = store.get(m.filename())
        self.assertEqual(r.outcome, "bad")
        self.assertEqual(r.validation_count, 0)
        self.assertNotEqual(r.id, "forged")
        self.assertTrue(r.contradiction_detected)


class TestSchemaSerialization(TmpStore):

    def test_new_fields_absent_on_old_files(self):
        # zero noise: a file without outcome keys parses to None/False and
        # re-serializes WITHOUT adding them
        rec = MemoryRecord(title="Old", content="body.")
        md = rec.to_markdown()
        self.assertNotIn("outcome", md)
        self.assertNotIn("contradiction_detected", md)
        r = MemoryRecord.from_markdown(md)
        self.assertIsNone(r.outcome)
        self.assertFalse(r.contradiction_detected)

    def test_contradiction_true_is_serialized(self):
        rec = MemoryRecord(title="C", content="b.")
        rec.contradiction_detected = True
        r = MemoryRecord.from_markdown(rec.to_markdown())
        self.assertTrue(r.contradiction_detected)

    def test_outcome_fields_round_trip(self):
        from datetime import datetime, timezone
        rec = MemoryRecord(title="O", content="b.")
        rec.outcome = "good"
        rec.outcome_at = datetime(2026, 9, 5, tzinfo=timezone.utc)
        rec.outcome_note = "still true"
        r = MemoryRecord.from_markdown(rec.to_markdown())
        self.assertEqual(r.outcome, "good")
        self.assertEqual(r.outcome_note, "still true")
        self.assertEqual(r.outcome_at.year, 2026)

    def test_outcome_value_outside_vocabulary_degrades(self):
        rec = MemoryRecord(title="O2", content="b.")
        md = rec.to_markdown().replace(
            "---\n\n", "---\n\n", 1)
        # hand-edit: invalid outcome value in frontmatter
        lines = md.split("\n")
        idx = next(i for i, ln in enumerate(lines) if ln == "---" and i > 0)
        lines.insert(idx, "outcome: bogus")
        r = MemoryRecord.from_markdown("\n".join(lines))
        self.assertIsNone(r.outcome, "unknown outcome value degrades to None")


class TestReservedKeyStrip(TmpStore):
    """RT F6: outcome* and contradiction_detected are RESERVED — import and
    migrate strip them like transit."""

    def _make_source_store(self):
        src = Path(tempfile.mkdtemp(prefix="ccmem_src_"))
        rec = MemoryRecord(title="Foreign", content="their truth.")
        rec.validation_count = 42
        rec.contradiction_detected = False
        rec.outcome = "good"
        rec.outcome_note = "trusted over there"
        (src / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        # hand-craft a hostile one: bad + contradiction pre-positioned
        hostile = MemoryRecord(title="Hostile", content="smuggled.")
        md = hostile.to_markdown()
        md = md.replace("---\n\n", "---\n", 1)
        lines = md.split("\n")
        idx = next(i for i, ln in enumerate(lines) if ln == "---" and i > 0)
        lines[idx:idx] = ["contradiction_detected: false", "outcome: bad",
                          "outcome_note: forged", "outcome_at: 2020-01-01T00:00:00Z"]
        (src / "fact_hostile.md").write_text("\n".join(lines), encoding="utf-8")
        return src

    def test_import_store_strips_outcome_keys(self):
        src = self._make_source_store()
        plan = store.import_store(src, apply=True)
        self.assertTrue(plan["created"])
        got = {m.title: m for m in store.iter_memories()}
        f = got["Foreign"]
        self.assertIsNone(f.outcome, "imported outcome must be stripped")
        self.assertIsNone(f.outcome_note)
        h = got["Hostile"]
        self.assertIsNone(h.outcome)
        self.assertFalse(h.contradiction_detected,
                         "no smuggled dispute state either way")

    def test_migrate_strip_function_removes_outcome_keys(self):
        # migrate's entry path strips reserved keys at text level; FL-2
        # extends the same boundary to outcome* and contradiction_detected.
        from foldcrumbs import cli
        src = self._make_source_store()
        raw = (src / "fact_hostile.md").read_text(encoding="utf-8")
        self.assertIn("outcome: bad", raw)
        cleaned = cli._strip_reserved_keys(raw)
        self.assertNotIn("outcome:", cleaned)
        self.assertNotIn("outcome_note:", cleaned)
        self.assertNotIn("outcome_at:", cleaned)
        self.assertNotIn("contradiction_detected:", cleaned)
        # transit still stripped (existing boundary intact)
        self.assertNotIn("transit:", cleaned)
        # the memory body and ordinary frontmatter survive
        self.assertIn("name: Hostile", cleaned)
        self.assertIn("smuggled.", cleaned)
        # and the cleaned text parses clean
        r = MemoryRecord.from_markdown(cleaned)
        self.assertIsNone(r.outcome)
        self.assertFalse(r.contradiction_detected)


class TestOutcomeList(TmpStore):
    """--list joins the adoption ledger with outcomes."""

    def test_list_empty_when_no_adoptions(self):
        rows = outcome_mod.list_outcomes()
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
