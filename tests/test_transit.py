"""Fixtures for D3-bis — transit-only for attested superseded nodes (0.9.0).

Design docs/design/g2-extraction.md §D3-bis (REV-3) mandates the acceptance
matrix BEFORE code: two-phase BFS (phase 1 active-only = exact 0.8.0
behaviour; phase 2 only after phase-1 NOT_FOUND_EXHAUSTIVE, extended to
superseded nodes a human attested via `graph transit`), reserved-key trust
boundary (automatic paths strip the key; only the CLI attests), exact field
semantics, markers never silent.

These tests are born red against 0.8.0 main.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs.schema import MemoryRecord  # noqa: E402
from foldcrumbs import config, store, relations  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


class TransitStore(TmpStore):
    """Helpers: chains of active + superseded memories with manual arcs."""

    def _put(self, title, content="Body.", **kw):
        rec = MemoryRecord(title=title, content=content, **kw)
        store.write_memory(rec)
        return rec

    def _link(self, src, dst, prov="manual", evidence="e"):
        relations.add_relation(src.id, "depends_on", {"k": "m", "id": dst.id},
                               evidence=evidence, prov=prov)

    def _supersede(self, rec, by_id):
        """Supersede with a FRESH load (the arcs live on disk, not on the
        stale in-memory object)."""
        fresh = next(m for m in store.load_all() if m.id == rec.id)
        store.mark_superseded_on_disk(fresh, by_id)

    def _chain(self, middle_status="superseded"):
        """A -manual-> S -manual-> B. S superseded by default."""
        a, s, b = self._put("A"), self._put("S"), self._put("B")
        self._link(a, s)
        self._link(s, b)
        if middle_status == "superseded":
            self._supersede(s, b.id)
        return a, s, b

    def _attest(self, rec):
        relations.set_transit(rec.id, on=True)


# ── matrix row 1: attested superseded becomes transit-only ─────────────────

class TestTransitBasic(TransitStore):

    def test_attested_superseded_traversed_with_marker(self):
        """Row 1: A -manual-> S(superseded, transit on) -manual-> B → FOUND,
        S step carries status=superseded."""
        a, s, b = self._chain()
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE",
                         "before attestation the chain must NOT resolve")
        self._attest(s)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")
        titles = [step["title"] for step in res["path"]]
        self.assertEqual(titles, ["A", "S", "B"])
        mid = res["path"][1]
        self.assertEqual(mid.get("status"), "superseded")
        self.assertEqual(res["path"][0].get("status"), "active")

    def test_unattested_superseded_stays_excluded(self):
        """Row 3: fail-closed — no attestation, no transit."""
        a, s, b = self._chain()
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_attestation_is_idempotent(self):
        """graph transit on on an attested record is a valid no-op."""
        a, s, b = self._chain()
        self._attest(s)
        res = relations.set_transit(s.id, on=True)   # again
        self.assertEqual(res.get("action"), "noop")
        res = relations.set_transit(s.id, on=False)
        self.assertEqual(res.get("action"), "ok")
        res = relations.set_transit(s.id, on=False)  # again
        self.assertEqual(res.get("action"), "noop")

    def test_attestation_refuses_non_superseded(self):
        """Kimi R2-F1: attesting an active memory is a visible refusal."""
        a = self._put("A")
        with self.assertRaises(relations.InvalidRelation):
            relations.set_transit(a.id, on=True)
        # And the refusal writes nothing.
        rec = next(m for m in store.load_all() if m.id == a.id)
        self.assertNotIn("transit", rec.extra_meta)


# ── matrix row 2: prov containment stays orthogonal (D1) ───────────────────

class TestTransitProvContainment(TransitStore):

    def test_agent_arcs_through_transit_still_need_flag(self):
        """Row 2: agent arcs through an attested superseded node are walked
        ONLY behind include_inferred."""
        a, s, b = self._put("A"), self._put("S"), self._put("B")
        self._link(a, s, prov="agent")
        self._link(s, b, prov="agent")
        self._supersede(s, b.id)
        self._attest(s)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")
        res = relations.find_path(a.id, b.id, include_inferred=True)
        self.assertEqual(res["status"], "FOUND")
        self.assertEqual(res["path"][1].get("status"), "superseded")


# ── matrix rows 4-7: chains, endpoints, forbidden statuses, directions ─────

class TestTransitChainsAndEndpoints(TransitStore):

    def test_two_consecutive_superseded(self):
        """Row 4: A - S1 - S2 - B, both attested → FOUND, markers on both."""
        a = self._put("A")
        s1, s2 = self._put("S1"), self._put("S2")
        b = self._put("B")
        self._link(a, s1)
        self._link(s1, s2)
        self._link(s2, b)
        self._supersede(s1, s2.id)
        self._supersede(s2, b.id)
        self._attest(s1)
        self._attest(s2)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")
        self.assertEqual([s["title"] for s in res["path"]],
                         ["A", "S1", "S2", "B"])
        self.assertEqual(res["path"][1].get("status"), "superseded")
        self.assertEqual(res["path"][2].get("status"), "superseded")

    def test_superseded_endpoint_refused_with_note(self):
        """Row 5: superseded src/dst stays NOT_FOUND_EXHAUSTIVE with note."""
        a, s, b = self._chain()
        self._attest(s)
        res = relations.find_path(s.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")
        self.assertIn("not traversable", res.get("note", ""))
        res = relations.find_path(b.id, s.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_deleted_never_transited(self):
        """Row 6: deleted/provisional/expired are never traversed."""
        a, s, b = self._chain()
        self._attest(s)
        fresh = next(m for m in store.load_all() if m.id == s.id)
        fresh.status = "deleted"
        store.write_memory(fresh)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_both_storage_directions_walked(self):
        """Row 7: arcs stored outward from the transit node are walked too —
        and (the 0.8.0 leak) arcs stored INTO an UNattested superseded node
        are NOT walked in either direction."""
        # Outward storage: S holds both arcs (A<-S via S->A reverse walk).
        a, s, b = self._put("A"), self._put("S"), self._put("B")
        self._link(s, a)          # stored S->A, walked A->S
        self._link(s, b)          # stored S->B, walked S->B
        self._supersede(s, b.id)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE",
                         "unattested superseded must not leak (0.8.0 bug)")
        self._attest(s)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")


# ── matrix row 8: phase 1 (active-only) always wins ────────────────────────

class TestTwoPhasePreference(TransitStore):

    def test_active_only_path_beats_shorter_transit_path(self):
        """Row 8: diamond — A-C-B active path (2 hops) vs A-S-B (2 hops, S
        attested). Tie-break is deterministic id order; the point is the
        result never requires transit when an active path exists. Use a
        LONGER transit path to prove the preference: A-S1-S2-B vs A-C-B."""
        a, c, b = self._put("A"), self._put("C"), self._put("B")
        s1, s2 = self._put("S1"), self._put("S2")
        self._link(a, c)
        self._link(c, b)
        self._link(a, s1)
        self._link(s1, s2)
        self._link(s2, b)
        self._supersede(s1, s2.id)
        self._supersede(s2, b.id)
        self._attest(s1)
        self._attest(s2)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")
        self.assertEqual([s["title"] for s in res["path"]], ["A", "C", "B"],
                         "phase 1 must find the active-only path")

    def test_phase1_truncated_stays_truncated_no_phase2(self):
        """Row 12: phase-1 TRUNCATED returns TRUNCATED; phase 2 never runs.
        The fixture must truncate PHASE 1 itself: an active-only path that
        exceeds the depth budget (A-X-Y-B, 3 hops at depth 2), while a
        shorter transit path (A-S-B, 2 hops) exists for phase 2. If phase 2
        ran, the answer would be FOUND — TRUNCATED:depth without the transit
        note proves it did not."""
        a, x, y, b = (self._put("A"), self._put("X"),
                      self._put("Y"), self._put("B"))
        self._link(a, x)
        self._link(x, y)
        self._link(y, b)
        s = self._put("S")
        self._link(a, s)
        self._link(s, b)
        self._supersede(s, b.id)
        self._attest(s)
        res = relations.find_path(a.id, b.id, depth=2)
        self.assertEqual(res["status"], "TRUNCATED:depth")
        self.assertNotIn("transit", res.get("note", ""),
                         "phase 2 must not have run after a phase-1 "
                         "TRUNCATED")

    def test_phase2_truncated_says_transit_in_play(self):
        """Row 14: phase 1 exhaustive, phase 2 budget exhausted → TRUNCATED
        with the transit note."""
        a, s, b = self._chain()
        self._attest(s)
        res = relations.find_path(a.id, b.id, max_nodes=2)
        self.assertTrue(res["status"].startswith("TRUNCATED"))
        self.assertIn("transit", res.get("note", ""))


# ── matrix rows 13: phase transitions ──────────────────────────────────────

class TestPhaseTransitions(TransitStore):

    def test_phase1_not_found_then_phase2_found(self):
        """Row 13: the canonical field-test chain resolves in phase 2."""
        a, s, b = self._chain()
        self._attest(s)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")

    def test_080_answers_never_change(self):
        """The phase-2 universe only adds answers where 0.8.0 said
        NOT_FOUND_EXHAUSTIVE; every FOUND of 0.8.0 stays byte-identical."""
        a, c, b = self._put("A"), self._put("C"), self._put("B")
        self._link(a, c)
        self._link(c, b)
        before = relations.find_path(a.id, b.id)
        # Attesting an unrelated superseded chain must not alter it.
        s1 = self._put("S1")
        self._link(a, s1)
        self._link(s1, b)
        self._supersede(s1, b.id)
        self._attest(s1)
        after = relations.find_path(a.id, b.id)
        self.assertEqual(before, after)


# ── matrix row 15: trust boundary ──────────────────────────────────────────

class TestTrustBoundary(TransitStore):

    def _write_memory_file(self, title, extra_frontmatter=""):
        """Write a memory file BY HAND (simulating an external store)."""
        rec = MemoryRecord(title=title, content="Body.")
        text = rec.to_markdown()
        # Inject the extra frontmatter line before the closing ---.
        text = text.replace("---\n\n", f"{extra_frontmatter}---\n\n", 1)
        path = Path(config.memory_dir()) / rec.filename()
        path.write_text(text, encoding="utf-8")
        return rec

    def test_imported_transit_key_stripped(self):
        """Row 15: an imported record carrying transit:true enters WITHOUT
        the key; a later supersede does not make it transit-eligible."""
        import tempfile
        src_dir = Path(tempfile.mkdtemp(prefix="fc_import_src_"))
        rec = MemoryRecord(title="Imp", content="Body.")
        text = rec.to_markdown()
        text = text.replace("---\n\n", "transit: true\n---\n\n", 1)
        (src_dir / rec.filename()).write_text(text, encoding="utf-8")
        plan = store.import_store(src_dir, apply=True)
        self.assertEqual(plan["created"], [rec.filename()])
        loaded = next(m for m in store.load_all() if m.id == rec.id)
        self.assertNotIn("transit", loaded.extra_meta,
                         "reserved key must be stripped at import")
        # Supersede it now: still not transit-eligible.
        other = self._put("Other")
        fresh = next(m for m in store.load_all() if m.id == rec.id)
        store.mark_superseded_on_disk(fresh, other.id)
        # Build a chain through it: needs a second active on each side.
        a, b = self._put("A2"), self._put("B2")
        relations.add_relation(a.id, "depends_on",
                               {"k": "m", "id": rec.id},
                               evidence="e", prov="manual")
        relations.add_relation(rec.id, "depends_on",
                               {"k": "m", "id": b.id},
                               evidence="e", prov="manual")
        # Re-supersede after the arcs (fresh load).
        fresh = next(m for m in store.load_all() if m.id == rec.id)
        store.mark_superseded_on_disk(fresh, other.id)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")

    def test_field_semantics_fail_closed(self):
        """Row 15: only the exact value 'true' (strip, case-sensitive)
        attests. yes/1/TRUE/empty are off."""
        for bad in ("yes", "1", "TRUE", "", "false"):
            a, s, b = self._chain()
            fresh = next(m for m in store.load_all() if m.id == s.id)
            fresh.extra_meta["transit"] = bad
            store.write_memory(fresh)
            res = relations.find_path(a.id, b.id)
            self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE",
                             f"value {bad!r} must stay intransit")

    def test_migrate_strips_whitespace_key_variants(self):
        """GPT code-RT P0: the parser partitions on the FIRST colon and
        strips the key, so ' transit: true' and 'transit : true' both parse
        as the key 'transit'. migrate must strip those variants too — a
        literal 'transit:' prefix match lets them ride through."""
        from foldcrumbs import cli
        for variant in ("transit: true", " transit: true", "transit : true"):
            with self.subTest(variant=variant):
                text = cli._strip_reserved_transit(
                    "---\nname: M\n" + variant + "\nstatus: active\n---\n\n"
                    "Body keeps transit: mentions in prose.\n")
                meta, body = __import__("foldcrumbs.schema", fromlist=["x"]) \
                    ._split_frontmatter(text)
                self.assertNotIn("transit", meta)
                # Body untouched — a 'transit:' mention in prose survives.
                self.assertIn("transit: mentions in prose", body)
                self.assertIn("status: active", text)

    def test_migrate_entry_never_imports_attestation(self):
        """Full migrate path: a copied memory carrying a pre-positioned
        transit attestation lands WITHOUT it; a later supersede cannot turn
        it into a transit node."""
        import os
        import tempfile
        from importlib import reload
        from foldcrumbs import cli
        from foldcrumbs import config as _c

        # migrate --from derives the SOURCE store via memory_dir(from_dir)
        # and the TARGET via memory_dir(). With FOLDCRUMBS_DIR set both
        # collapse onto the same dir (migrate would skip), so clear the DIR
        # overrides and let CLAUDE_CONFIG_DIR drive the layout — same
        # pattern as TestP03PerStoreQueue.
        cfg_dir = tempfile.mkdtemp(prefix="fc_mig_cfg_")
        env_keys = ("FOLDCRUMBS_DIR", "ENGRAM_DIR", "CLAUDE_CONFIG_DIR")
        saved = {k: os.environ.get(k) for k in env_keys}
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ["CLAUDE_CONFIG_DIR"] = cfg_dir
        reload(_c)
        try:
            proj = tempfile.mkdtemp(prefix="fc_mig_proj_")
            src_mem = Path(_c.memory_dir(proj))
            src_mem.mkdir(parents=True, exist_ok=True)
            rec = MemoryRecord(title="Mig", content="Body.")
            text = rec.to_markdown().replace(
                "---\n\n", "transit: true\n---\n\n", 1)
            (src_mem / rec.filename()).write_text(text, encoding="utf-8")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["migrate", "--from", proj, "--force"])
            self.assertEqual(code, 0)
            loaded = next(m for m in store.load_all() if m.id == rec.id)
            self.assertNotIn("transit", loaded.extra_meta)
            # Supersede + chain: still not traversable.
            other = self._put("Other")
            store.mark_superseded_on_disk(loaded, other.id)
            a, b = self._put("MA"), self._put("MB")
            relations.add_relation(a.id, "depends_on",
                                   {"k": "m", "id": rec.id},
                                   evidence="e", prov="manual")
            relations.add_relation(rec.id, "depends_on",
                                   {"k": "m", "id": b.id},
                                   evidence="e", prov="manual")
            fresh = next(m for m in store.load_all() if m.id == rec.id)
            store.mark_superseded_on_disk(fresh, other.id)
            res = relations.find_path(a.id, b.id)
            self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            reload(_c)

    def test_rewrite_preserves_valid_attestation(self):
        """Row 15: normal rewrites preserve a valid attestation."""
        a, s, b = self._chain()
        self._attest(s)
        fresh = next(m for m in store.load_all() if m.id == s.id)
        fresh.validate()          # a normal rewrite path
        store.write_memory(fresh)
        res = relations.find_path(a.id, b.id)
        self.assertEqual(res["status"], "FOUND")

    def test_foreign_record_cannot_be_attested(self):
        """The gate is local-only: a foreign record refuses attestation.
        origin_root is runtime-only (not serialized), so simulate it by
        patching the loader."""
        from unittest import mock
        a, s, b = self._chain()
        foreign = next(m for m in store.load_all() if m.id == s.id)
        foreign.origin_root = "some-other-root"
        with mock.patch.object(store, "load_all", return_value=[foreign]):
            with self.assertRaises(relations.InvalidRelation):
                relations.set_transit(s.id, on=True)
        # Nothing was written.
        rec = next(m for m in store.load_all() if m.id == s.id)
        self.assertNotIn("transit", rec.extra_meta)


# ── CLI end-to-end: graph transit + markers ───────────────────────────────

class TestTransitCLI(TransitStore):
    """The commands the user actually types, run in-process."""

    def _run(self, *argv):
        import contextlib
        import io
        from foldcrumbs import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_transit_on_then_path_marker(self):
        """Full loop: chain hidden, attest on, path FOUND with the marker."""
        a, s, b = self._chain()
        code, out, _ = self._run("graph", "path", "A", "B")
        self.assertEqual(code, 1)
        self.assertIn("NOT_FOUND_EXHAUSTIVE", out)

        code, out, _ = self._run("graph", "transit", "S", "on")
        self.assertEqual(code, 0, out)
        self.assertIn("attested", out)

        code, out, _ = self._run("graph", "path", "A", "B")
        self.assertEqual(code, 0, out)
        self.assertIn("FOUND", out)
        self.assertIn("superseded — transit", out,
                      "the transit step must be marked, never silent")

    def test_transit_off_withdraws(self):
        a, s, b = self._chain()
        self._run("graph", "transit", "S", "on")
        code, out, _ = self._run("graph", "transit", "S", "off")
        self.assertEqual(code, 0, out)
        self.assertIn("withdrawn", out)
        code, out, _ = self._run("graph", "path", "A", "B")
        self.assertEqual(code, 1)
        self.assertIn("NOT_FOUND_EXHAUSTIVE", out)

    def test_transit_refuses_active_memory(self):
        self._put("Alive")
        code, out, err = self._run("graph", "transit", "Alive", "on")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)
        self.assertIn("superseded", err)

    def test_transit_idempotent_noop(self):
        a, s, b = self._chain()
        self._run("graph", "transit", "S", "on")
        code, out, _ = self._run("graph", "transit", "S", "on")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do", out)

    def test_mcp_marker_present(self):
        """The MCP graph_path rendering marks transit steps too."""
        from foldcrumbs import mcp_server
        a, s, b = self._chain()
        self._attest(s)
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1,
                               "method": "tools/call",
                               "params": {"name": "graph_path", "arguments": {
                                   "from": "A", "to": "B"}}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("FOUND", text)
        self.assertIn("superseded — transit", text)


if __name__ == "__main__":
    unittest.main()
