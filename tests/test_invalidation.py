"""INV — invalidation contracts (docs/design/invalidated-by.md rev 2).

Acceptance matrix T1-T15 + T10-bis. The contract: A --invalidated_by--> B
means "A holds only while B is BASE-alive". Derivation on read, never a
cascade write; three outcomes (INVALIDATED / DANGLING / UNRESOLVED),
all fail-closed; contract-carrying records are create-only at their
destination (D4) so no upsert can silently overwrite a contract away.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import config as _c  # noqa: E402
from foldcrumbs import invalidation as inv  # noqa: E402
from foldcrumbs import relations, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402
from test_recall_layers import _call, _text  # noqa: E402


def _rec(title, content, type_="fact", **kw):
    return MemoryRecord(title=title, content=content, type=type_, **kw)


class InvBase(TmpStore):
    """A store with B (the contract target) and A (the dependent)."""

    def setUp(self):
        super().setUp()
        self.b = _rec("Stg cluster", "We use the stg cluster for staging.",
                      type_="decision")
        store.write_memory(self.b)
        self.a = _rec("Staging URL", "The staging URL is https://stg.example.com.",
                      type_="fact")
        store.write_memory(self.a)
        relations.add_relation(
            self.a.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="URL exists only while the cluster does",
            confidence=0.9, prov="manual")
        self.a2 = store.get(self.a.filename())   # reload with the edge
        store.rebuild_index()

    def _kill_b(self, how):
        if how == "supersede":
            nb = _rec("Stg cluster v2", "We moved staging to the k8s cluster.",
                      type_="decision")
            store.write_memory(nb)
            self.assertTrue(store.supersede(self.b.filename(), nb.filename()))
        elif how == "archive":
            self.assertTrue(store.set_status(self.b.filename(), "archived"))
        elif how == "delete":
            store.forget(self.b.filename())   # soft: status=deleted
        elif how == "expire":
            p = Path(self.dir) / self.b.filename()
            rec = store.get(self.b.filename())
            rec.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            rec.source_path = rec.source_path or rec.filename()
            store.write_memory(rec)
        elif how == "forget":
            p = Path(self.dir) / self.b.filename()
            p.unlink()
        else:
            raise ValueError(how)
        store.rebuild_index()


class TestT1DeathMatrix(InvBase):
    """T1: every base-death mode excludes A from the served context."""

    def test_matrix(self):
        for how in ("supersede", "archive", "delete", "expire", "forget"):
            with self.subTest(death=how):
                # fresh store per mode (TmpStore state is per-test; rebuild
                # the pair inline for the subtests after the first)
                if how != "supersede":
                    self._restore_pair()
                self._kill_b(how)
                hits = store.search("staging url", limit=5)
                self.assertNotIn(self.a.filename(),
                                 [m.filename() for m in hits],
                                 f"A served after B {how}")
                diag = []
                store.search("staging url", limit=5, collect_invalidated=diag)
                self.assertTrue(diag, f"no diagnostic after B {how}")

    def _restore_pair(self):
        # rebuild a live B and a contracted A for the next subtest
        self.b = _rec("Stg cluster", "We use the stg cluster for staging.",
                      type_="decision")
        store.write_memory(self.b)
        relations.add_relation(
            self.a.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="pair restore", confidence=0.9, prov="manual")


class TestT2Revival(InvBase):
    def test_t2_revival_free(self):
        self._kill_b("archive")
        hits = store.search("staging url", limit=5)
        self.assertNotIn(self.a.filename(), [m.filename() for m in hits])
        # revive B by hand: no repair pass, next read re-derives
        self.assertTrue(store.set_status(self.b.filename(), "active"))
        store.rebuild_index()
        hits = store.search("staging url", limit=5)
        self.assertIn(self.a.filename(), [m.filename() for m in hits])


class TestT3RecallPartition(InvBase):
    def test_t3_diagnostic_tail(self):
        self._kill_b("supersede")
        txt = _text(_call(
            1, "recall", query="staging url"))
        self.assertIn("matched but not served", txt)
        self.assertIn("invalidated", txt)
        # the answer LLM context never sees the tail: tool_answer passes no
        # collect_invalidated — assert the served block excludes A
        self.assertNotIn("stg.example.com", txt.split("matched but not served")[0])

    def test_t3_cap_at_three(self):
        # three more contracted dependents of B, all invalidated -> 3 lines
        # + "showing 3 of N"
        for i in range(3):
            dep = _rec(f"Dependent {i}", f"staging detail number {i} for url.",
                       type_="fact")
            store.write_memory(dep)
            relations.add_relation(
                dep.id, "invalidated_by",
                target={"k": "m", "id": self.b.id},
                evidence=f"dep {i}", confidence=0.9, prov="manual")
        self._kill_b("supersede")
        txt = _text(_call(
            1, "recall", query="staging", limit=10))
        self.assertIn("showing 3 of", txt)
        self.assertEqual(txt.count("matched but not served"), 3)


class TestT4FetchEnvelope(InvBase):
    def test_t4_envelope_cli_mcp_parity(self):
        self._kill_b("supersede")
        txt = _text(_call(
            2, "fetch", names=[self.a.filename()]))
        self.assertIn("not served as current", txt)
        self.assertIn("stg.example.com", txt)   # raw file still served

    def test_t4_valid_fetch_unchanged(self):
        txt = _text(_call(
            2, "fetch", names=[self.a.filename()]))
        self.assertNotIn("not served as current", txt)
        self.assertIn("stg.example.com", txt)

    def test_t4_dangling_envelope(self):
        self._kill_b("forget")
        txt = _text(_call(
            2, "fetch", names=[self.a.filename()]))
        self.assertIn("does not resolve", txt)
        self.assertNotIn("deleted", txt.split("not served")[1][:120].lower()
                         .replace("does not resolve", ""))


class TestT5NoCascade(InvBase):
    def test_t5_supersede_writes_only_b(self):
        before = {p.name: p.read_bytes()
                  for p in Path(self.dir).glob("*.md")}
        nb = _rec("Stg cluster v2", "Moved staging to k8s.", type_="decision")
        store.write_memory(nb)
        store.supersede(self.b.filename(), nb.filename())
        after = {p.name: p.read_bytes()
                 for p in Path(self.dir).glob("*.md")}
        # A's file untouched — derivation is a read, never a write
        self.assertEqual(before[self.a.filename()], after[self.a.filename()])
        # only the two supersede participants changed/were added
        changed = {k for k in after
                   if k not in before or before.get(k) != after[k]}
        self.assertTrue(changed <= {self.b.filename(), nb.filename(),
                                    _c.INDEX_NAME})


class TestT6WriteRefusals(InvBase):
    def test_t6_dead_target(self):
        self._kill_b("archive")
        c = _rec("Another dependent", "staging thing.", type_="fact")
        store.write_memory(c)
        with self.assertRaises(relations.InvalidRelation) as ctx:
            relations.add_relation(
                c.id, "invalidated_by",
                target={"k": "m", "id": self.b.id},
                evidence="late contract", confidence=0.9, prov="manual")
        self.assertIn("live memory", str(ctx.exception))

    def test_t6_entity_target(self):
        c = _rec("Dep on entity", "x.", type_="fact")
        store.write_memory(c)
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                c.id, "invalidated_by",
                target={"k": "e", "name": "some-entity"},
                evidence="entity contract", confidence=0.9, prov="manual")

    def test_t6_self_edge(self):
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                self.a.id, "invalidated_by",
                target={"k": "m", "id": self.a.id},
                evidence="paradox", confidence=0.9, prov="manual")

    def test_t6_dangling_target(self):
        c = _rec("Dep on ghost", "y.", type_="fact")
        store.write_memory(c)
        with self.assertRaises(relations.InvalidRelation):
            relations.add_relation(
                c.id, "invalidated_by",
                target={"k": "m", "id": "no-such-id"},
                evidence="ghost", confidence=0.9, prov="manual")


class TestT7Mutual(InvBase):
    def test_t7_mutual_pair(self):
        # B also depends on A — allowed; both die on either BASE death
        relations.add_relation(
            self.b.id, "invalidated_by",
            target={"k": "m", "id": self.a.id},
            evidence="mutual", confidence=0.9, prov="manual")
        self._kill_b("supersede")   # B base-dies
        hits = [m.filename() for m in store.search("staging", limit=10)]
        self.assertNotIn(self.a.filename(), hits)   # A: target dead
        # doctor reports the pair
        from foldcrumbs import invalidation
        report = invalidation.doctor_report()
        self.assertTrue(any("mutual" in line.lower() for line in report),
                        report)


class TestT8SingleHop(InvBase):
    def test_t8_chain_does_not_propagate(self):
        c = _rec("Stg cluster parent", "The stg cluster exists.",
                 type_="decision")
        store.write_memory(c)
        # B invalidated_by C
        relations.add_relation(
            self.b.id, "invalidated_by",
            target={"k": "m", "id": c.id},
            evidence="chain", confidence=0.9, prov="manual")
        store.rebuild_index()
        # C dies -> B invalidated; A stays SERVED (B is base-alive)
        nc = _rec("Cluster parent v2", "Superseded.", type_="decision")
        store.write_memory(nc)
        store.supersede(c.filename(), nc.filename())
        store.rebuild_index()
        hits = [m.filename() for m in store.search("staging url", limit=5)]
        self.assertIn(self.a.filename(), hits,
                      "single-hop violated: A excluded because B was "
                      "invalidated (B is still base-alive)")
        bhits = [m.filename() for m in store.search("stg cluster", limit=5)]
        self.assertNotIn(self.b.filename(), bhits)


class TestT10bisDedupProtection(InvBase):
    """T10-bis: the rev1 P0 — upsert of identical content must NOT
    overwrite a contract-carrying record."""

    def _attempt_upsert(self):
        same = _rec("Staging URL",
                    "The staging URL is https://stg.example.com.",
                    type_="fact")
        return store.upsert(same)

    def test_overwrite_refused_contract_intact(self):
        self._kill_b("supersede")
        p = Path(self.dir) / self.a.filename()
        before = p.read_bytes()
        with self.assertRaises(store.ContractProtectedError) as ctx:
            self._attempt_upsert()
        self.assertIn("invalidation contract", str(ctx.exception))
        self.assertEqual(p.read_bytes(), before)
        # and while B is ALIVE too — the protection is identity-based, not
        # visibility-based (design D4: whatever the derived state)
        self._restore_b()
        after_repair = p.read_bytes()   # the repair legitimately changed A
        with self.assertRaises(store.ContractProtectedError):
            self._attempt_upsert()
        self.assertEqual(p.read_bytes(), after_repair)

    def _restore_b(self):
        nb = _rec("Stg cluster", "We use the stg cluster for staging.",
                  type_="decision")
        # supersede back is not a thing; write a fresh live B and re-point
        store.write_memory(nb)
        # repair: drop the stale edge and add a new one to the live B
        rec = store.get(self.a.filename())
        rec.source_path = rec.source_path or rec.filename()
        rec.relations_json = relations.canonical(
            [r for r in relations.parse(rec.relations_json)
             if r.get("p") != "invalidated_by"])
        store.write_memory(rec)
        relations.add_relation(
            self.a.id, "invalidated_by",
            target={"k": "m", "id": nb.id},
            evidence="repaired", confidence=0.9, prov="manual")
        store.rebuild_index()


class TestT10ConsumerSweep(InvBase):
    def test_index_excludes_and_counts(self):
        store.rebuild_index()
        idx = (Path(self.dir) / _c.INDEX_NAME).read_text(encoding="utf-8")
        self.assertNotIn("stg.example.com", idx)
        self.assertIn("Invalidation contracts", idx)
        self.assertIn("withheld from this snapshot", idx)

    def test_conflict_candidates_exclude(self):
        self._kill_b("supersede")
        probe = _rec("Staging URL note", "Something about staging URL.",
                     type_="fact")
        cands = store.find_conflict_candidates(probe, limit=5)
        self.assertNotIn(self.a.filename(), [m.filename() for m in cands])

    def test_timeline_anchor_refused(self):
        self._kill_b("supersede")
        from foldcrumbs import mcp_server
        txt = mcp_server.tool_timeline({"ref": self.a.filename(), "window": 2})
        self.assertIn("refused", txt)
        self.assertIn("not served as current", txt)

    def test_timeline_rows_exclude(self):
        self._kill_b("supersede")
        from foldcrumbs import mcp_server
        # anchor on a healthy memory; A must not appear as a row
        other = _rec("Payments note", "Payments oncall notes.", type_="fact")
        store.write_memory(other)
        store.rebuild_index()
        txt = mcp_server.tool_timeline({"ref": other.filename(), "window": 10})
        self.assertNotIn("Staging URL", txt)


class TestT11GraphUntouched(InvBase):
    def test_t11_path_traverses_invalidated(self):
        self._kill_b("supersede")
        c = _rec("Ingress fact", "Ingress routes to the staging URL.",
                 type_="fact")
        store.write_memory(c)
        relations.add_relation(
            c.id, "depends_on", target={"k": "m", "id": self.a.id},
            evidence="route", confidence=0.9, prov="manual")
        res = relations.find_path(c.id, self.b.id)
        # whatever the traversal verdict, it must not be an *invalidation*
        # refusal: graph_path knows nothing about contracts
        self.assertNotIn("invalidat", str(res).lower())


class TestT12Cost(InvBase):
    def test_t12_one_context_per_search(self):
        reads = []
        real_scan = store._read_local

        def spy(cwd=None):
            recs, complete = real_scan(cwd)
            reads.append(len(recs))
            return recs, complete

        store._read_local = spy
        try:
            store.search("staging", limit=10)
        finally:
            store._read_local = real_scan
        self.assertEqual(len(reads), 1,
                         f"search scanned the store {len(reads)} times")

    def test_t12_duplicate_ids_unresolved(self):
        # forge a duplicate-id B: alive copy + dead copy -> UNRESOLVED,
        # never last-wins, never "deleted"
        dupdir = Path(self.dir)
        live = MemoryRecord.from_markdown(
            (dupdir / self.b.filename()).read_text(encoding="utf-8"))
        twin = _rec("Stg cluster twin", "Duplicate id twin.", type_="decision")
        twin.id = live.id
        (dupdir / twin.filename()).write_text(twin.to_markdown(),
                                              encoding="utf-8")
        store.set_status(twin.filename(), "archived")
        ctx = inv.ReadContext.for_store()
        outcome, detail = ctx.resolve(live.id)
        self.assertEqual(outcome, inv.UNRESOLVED)
        self.assertIn("ambiguous", detail)
        diag = []
        store.search("staging url", limit=5, collect_invalidated=diag)
        self.assertTrue(diag)
        self.assertEqual(diag[0][1], inv.UNRESOLVED)
        self.assertIn("ambiguous", diag[0][2])


class TestT13Expiry(InvBase):
    def test_t13_future_expiry_does_not_invalidate(self):
        rec = store.get(self.b.filename())
        rec.expires_at = datetime.now(timezone.utc) + timedelta(days=10)
        rec.source_path = rec.source_path or rec.filename()
        store.write_memory(rec)
        store.rebuild_index()
        hits = [m.filename() for m in store.search("staging url", limit=5)]
        self.assertIn(self.a.filename(), hits)

    def test_t13_past_expiry_invalidates(self):
        self._kill_b("expire")
        hits = [m.filename() for m in store.search("staging url", limit=5)]
        self.assertNotIn(self.a.filename(), hits)


class TestT14AuthzPrecedence(InvBase):
    def test_t14_grant_with_contract_shows_both(self):
        ev = _rec("Standup approved", "Standup: agent-a may deploy.",
                  type_="event")
        store.write_memory(ev)
        g = MemoryRecord(
            title="Deploy access", content="agent-a may deploy to prod.",
            type="authorization", grants="may deploy to prod",
            granted_to="agent-a", backed_by=ev.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            provenance="explicit_statement")
        from foldcrumbs import authz
        authz.mint(g)
        # grant ALSO carries an invalidation contract toward B
        relations.add_relation(
            g.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="access tied to the cluster", confidence=0.9,
            prov="manual")
        self._kill_b("supersede")
        # backing dies too -> UNBACKED; contract dead -> INVALIDATED label
        store.set_status(ev.filename(), "archived")
        section = authz.render_authorization_section()
        self.assertIn("UNBACKED", section)
        self.assertIn("INVALIDATED", section)


class TestT15DanglingDoctor(InvBase):
    def test_t15_doctor_lists_dangling(self):
        self._kill_b("forget")
        report = inv.doctor_report()
        self.assertTrue(any("does not resolve" in line for line in report),
                        report)


class TestRtRound1P0(InvBase):
    """RT GPT round 1 on 4954fd8 (card t_145de292): F1-F4, all
    probe-reproduced."""

    def test_f1_index_never_publishes_grants(self):
        # F1 (regression of the authz snapshot rule): rebuild_index must
        # keep excluding authorization records — the delta dropped the
        # type filter when it added the contract filter, re-injecting
        # grants into SessionStart/PostCompact snapshots.
        ev = _rec("Approval", "Approved: agent-a may deploy.", type_="event")
        store.write_memory(ev)
        g = MemoryRecord(
            title="Deploy access", content="agent-a may deploy.",
            type="authorization", grants="may deploy", granted_to="agent-a",
            backed_by=ev.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            provenance="explicit_statement")
        from foldcrumbs import authz
        authz.mint(g)
        store.rebuild_index()
        idx = (Path(self.dir) / _c.INDEX_NAME).read_text(encoding="utf-8")
        self.assertNotIn(g.title, idx)
        self.assertNotIn(g.filename(), idx)
        # the backing EVENT is an ordinary memory and stays listed; the
        # grant itself must appear only via the pointer line, never as a
        # memory row
        self.assertNotIn(f"[{g.title}]", idx)

    def test_f2_source_path_does_not_excuse_foreign_identity(self):
        # F2: write_memory skipped the contract check whenever
        # rec.source_path was set — but source_path proves nothing about
        # WHICH record is being rewritten. A loaded record C, retitled to
        # collide with A's destination, must not overwrite A's contract.
        self._kill_b("supersede")
        c = _rec("Unrelated", "Totally different content here.", type_="fact")
        store.write_memory(c)
        loaded = store.get(c.filename())
        loaded.source_path = loaded.source_path or loaded.filename()
        # now forge the collision: same title/type as A -> same destination
        loaded.title = self.a.title
        loaded.content = self.a.content
        loaded.type = self.a.type
        p = Path(self.dir) / self.a.filename()
        before = p.read_bytes()
        with self.assertRaises(store.ContractProtectedError):
            store.write_memory(loaded)
        self.assertEqual(p.read_bytes(), before)

    def test_f2_legitimate_maintenance_still_works(self):
        # the exemption must survive for the SAME record: load A, edit A,
        # write A back (relations edits, archive) — identity matches.
        self._kill_b("supersede")
        rec = store.get(self.a.filename())
        rec.source_path = rec.source_path or rec.filename()
        rec.description = "annotated during repair"
        store.write_memory(rec)     # must NOT raise
        after = store.get(self.a.filename())
        self.assertEqual(after.id, self.a.id)
        self.assertEqual(after.description, "annotated during repair")

    def test_f3_incomplete_scan_never_valid(self):
        # F3: resolve() declared VALID from an incomplete scan — loss of
        # evidence turned an ambiguous state into served truth.
        ctx = inv.ReadContext(
            [_rec_live_target()], complete=False)
        outcome, _detail = ctx.resolve("some-id-not-in-list")
        self.assertEqual(outcome, inv.UNRESOLVED)
        # and a FOUND-alive target in an incomplete context is still not
        # proof of uniqueness:
        t = _rec_live_target()
        ctx2 = inv.ReadContext([t], complete=False)
        outcome2, detail2 = ctx2.resolve(t.id)
        self.assertEqual(outcome2, inv.UNRESOLVED,
                         f"incomplete scan yielded {outcome2}")
        self.assertIn("verifi", detail2.lower())

    def test_f4_fetch_grant_shows_both_envelopes(self):
        # F4: fetch returned early for grants — the invalidation envelope
        # never composed with the authz one.
        ev = _rec("Approval2", "Approved: agent-b may deploy.", type_="event")
        store.write_memory(ev)
        g = MemoryRecord(
            title="Grant with contract", content="agent-b may deploy.",
            type="authorization", grants="may deploy", granted_to="agent-b",
            backed_by=ev.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            provenance="explicit_statement")
        from foldcrumbs import authz
        authz.mint(g)
        relations.add_relation(
            g.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="tied to cluster", confidence=0.9, prov="manual")
        self._kill_b("supersede")
        txt = _text(_call(2, "fetch", names=[g.filename()]))
        self.assertIn("authorization:", txt)        # authz envelope
        self.assertIn("not served as current", txt)  # invalidation envelope

    def test_f4_fetch_foreign_contract_flagged(self):
        # F4: a foreign record with a contract was served raw. From a
        # non-owner reader the contract is UNRESOLVED — the envelope must
        # say so (raw still served; the refusal of foreign GRANTS stays).
        import importlib
        from foldcrumbs import federation
        fed_home = Path(self._state) / "f4_fed"
        fed_home.mkdir(parents=True)
        saved = {k: os.environ.get(k) for k in
                 ("FOLDCRUMBS_STATE_DIR", "CLAUDE_CONFIG_DIR",
                  "FOLDCRUMBS_DIR", "ENGRAM_DIR", "ENGRAM_STATE_DIR")}
        state = Path(self._state) / "f4_fstate"
        state.mkdir(parents=True)
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(fed_home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ.pop("ENGRAM_STATE_DIR", None)
        importlib.reload(_c)
        old_cwd = os.getcwd()
        try:
            mine = federation.register(fed_home / ".claude")
            theirs = federation.register(fed_home / ".claude-work")
            proj = fed_home / "proj"
            proj.mkdir(parents=True, exist_ok=True)
            my_dir = mine.memory_dir(proj)
            my_dir.mkdir(parents=True, exist_ok=True)
            their_dir = theirs.memory_dir(proj)
            their_dir.mkdir(parents=True, exist_ok=True)
            # their pair: A_f contracted on B_f, B_f then archived IN THEIR
            # store (the contract dies where it lives)
            tb = MemoryRecord(title="Their cluster",
                              content="Their staging cluster lives.",
                              type="decision")
            (their_dir / tb.filename()).write_text(tb.to_markdown(),
                                                   encoding="utf-8")
            ta = MemoryRecord(title="Their URL",
                              content="Their staging URL is https://x.example.",
                              type="fact")
            (their_dir / ta.filename()).write_text(ta.to_markdown(),
                                                   encoding="utf-8")
            # write the edge by hand (canonical JSON) — same shape relate
            # would produce; avoids env juggling for their-store writes
            import json as _json
            edge = [{"p": "invalidated_by",
                     "t": {"k": "m", "id": tb.id},
                     "c": 0.9, "d": "2026-09-07T00:00:00+00:00",
                     "e": "their contract", "prov": "manual"}]
            txt_f = (their_dir / ta.filename()).read_text(encoding="utf-8")
            # schema serializes relations_json RAW (no extra quoting) and
            # from_markdown reads it back with .strip() — match that shape
            txt_f = txt_f.replace(
                "---\n", f"---\nrelations_json: {_json.dumps(edge)}\n", 1)
            (their_dir / ta.filename()).write_text(txt_f, encoding="utf-8")
            tb2 = MemoryRecord.from_markdown(
                (their_dir / tb.filename()).read_text(encoding="utf-8"))
            tb2.status = "archived"
            tb2.source_path = tb2.filename()
            (their_dir / tb.filename()).write_text(tb2.to_markdown(),
                                                   encoding="utf-8")
            # a local record so fetch has a home root
            loc = MemoryRecord(title="Local note", content="Mine.",
                               type="fact")
            (my_dir / loc.filename()).write_text(loc.to_markdown(),
                                                 encoding="utf-8")
            os.chdir(proj)
            out = _text(_call(3, "fetch",
                              names=[f"{theirs.id}:{ta.filename()}"]))
        finally:
            os.chdir(old_cwd)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(_c)
        self.assertIn("x.example", out)   # raw still served
        self.assertTrue(
            "not served as current" in out or "unverified" in out.lower()
            or "could not verify" in out.lower(),
            f"foreign contracted memory served without envelope: {out[:200]}")


def _rec_live_target():
    return _rec("Live target", "A live target memory.", type_="decision")


class TestT15BenchS7(unittest.TestCase):
    def test_bench_scenario_exists(self):
        import json
        p = REPO / "benchmarks" / "evolving_state" / "scenarios.json"
        suite = json.loads(p.read_text(encoding="utf-8"))
        ids = [s["id"] for s in suite["scenarios"]]
        self.assertTrue(any(i.startswith("S7") for i in ids), ids)


if __name__ == "__main__":
    unittest.main()
