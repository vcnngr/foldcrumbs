"""AUTH — authorization integrity (docs/design/authorization-integrity.md rev 2).

Acceptance matrix T1-T20 from the design. The honest ledger contract:
a grant exists only with a live backing event/decision, a mandatory aware
future expiry, human provenance — and EVERY served read derives its state
(ACTIVE/EXPIRED/RETIRED/UNBACKED) or excludes it. Minting is locked and
fail-closed; maintenance (retiring a dead grant) is never blocked by the
minting gates.
"""

import argparse
import contextlib
import io
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import config as _c  # noqa: E402
from foldcrumbs import store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


def _grant(cwd=None, title="Deploy access", grants="may deploy to prod",
           granted_to="agent-a", backed_by=None, expires_in_days=30,
           provenance="explicit_statement", type_="authorization", **kw):
    return MemoryRecord(
        title=title, content=f"{granted_to} {grants}.", type=type_,
        grants=grants, granted_to=granted_to, backed_by=backed_by,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        provenance=provenance, **kw)


class AuthBase(TmpStore):
    """A store with one live backing event."""

    def setUp(self):
        super().setUp()
        self.event = MemoryRecord(
            title="Ops standup approved deploy access",
            content="Standup 2026-09-01: agent-a may deploy to prod until month end.",
            type="event")
        store.write_memory(self.event)
        store.rebuild_index()


class TestT1T2CreationGates(AuthBase):

    def test_t1_missing_grants_refused(self):
        g = _grant(backed_by=self.event.id)
        g.grants = ""
        with self.assertRaises(Exception) as ctx:
            store.write_memory(g)
        self.assertIn("grants", str(ctx.exception).lower())
        self.assertIsNone(store.get(g.filename()))

    def test_t1_missing_granted_to_refused(self):
        g = _grant(backed_by=self.event.id)
        g.granted_to = ""
        with self.assertRaises(Exception):
            store.write_memory(g)

    def test_t1_missing_backed_by_refused(self):
        g = _grant(backed_by=None)
        with self.assertRaises(Exception) as ctx:
            store.write_memory(g)
        self.assertIn("backed_by", str(ctx.exception).lower())

    def test_t1_missing_expiry_refused(self):
        g = _grant(backed_by=self.event.id)
        g.expires_at = None
        with self.assertRaises(Exception) as ctx:
            store.write_memory(g)
        self.assertIn("expires", str(ctx.exception).lower())

    def test_t1_naive_expiry_refused(self):
        g = _grant(backed_by=self.event.id)
        g.expires_at = datetime(2026, 12, 1)  # naive
        with self.assertRaises(Exception):
            store.write_memory(g)

    def test_t1_past_expiry_refused(self):
        g = _grant(backed_by=self.event.id, expires_in_days=-1)
        with self.assertRaises(Exception):
            store.write_memory(g)

    def test_t2_backing_matrix(self):
        # nonexistent
        with self.assertRaises(Exception) as ctx:
            store.write_memory(_grant(backed_by="no-such-id"))
        self.assertIn("backing", str(ctx.exception).lower())
        # wrong type (fact)
        f = MemoryRecord(title="A fact", content="x.", type="fact")
        store.write_memory(f)
        with self.assertRaises(Exception):
            store.write_memory(_grant(backed_by=f.id))
        # superseded
        s = MemoryRecord(title="Old decision", content="y.", type="decision")
        store.write_memory(s)
        newer = MemoryRecord(title="New decision", content="z.", type="decision")
        store.write_memory(newer)
        store.supersede(s.filename(), newer.filename())
        with self.assertRaises(Exception):
            store.write_memory(_grant(backed_by=s.id))
        # expired backing
        e = MemoryRecord(title="Expired event", content="w.", type="event",
                         expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        store.write_memory(e)
        with self.assertRaises(Exception):
            store.write_memory(_grant(backed_by=e.id))

    def test_t2_decision_backing_ok_event_backing_ok(self):
        d = MemoryRecord(title="Board decision", content="Approved.", type="decision")
        store.write_memory(d)
        g1 = _grant(title="Grant on event", backed_by=self.event.id)
        store.write_memory(g1)
        g2 = _grant(title="Grant on decision", backed_by=d.id)
        store.write_memory(g2)
        self.assertIsNotNone(store.get(g1.filename()))
        self.assertIsNotNone(store.get(g2.filename()))

    def test_t19_provenance_matrix(self):
        for bad in ("inferred", "imported", "observed", "corrected", "validated"):
            g = _grant(title=f"Grant {bad}", backed_by=self.event.id,
                       provenance=bad)
            with self.assertRaises(Exception, msg=f"provenance {bad} passed"):
                store.write_memory(g)

    def test_t19_corrupted_stored_grant_reads_expired(self):
        # hand-corrupted: valid at write, then expiry line removed on disk
        g = _grant(title="Corrupt later", backed_by=self.event.id)
        store.write_memory(g)
        path = Path(self.dir) / (g.source_path or g.filename())
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(
            f"expires_at: {g.expires_at.isoformat()}", "expires_at: "),
            encoding="utf-8")
        rec = store.get(path.name)
        from foldcrumbs import authz
        self.assertEqual(authz.derived_state(rec), "EXPIRED")

    def test_upsert_gate_too(self):
        # upsert is a creation path: same gates
        g = _grant(backed_by=None)
        with self.assertRaises(Exception):
            store.upsert(g)


class TestT14T18IdentityDedup(AuthBase):

    def test_t14_same_title_after_retirement_keeps_predecessor(self):
        g = _grant(title="Prod deploy", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Access withdrawn",
                               content="Withdrawn at standup.", type="event")
        store.write_memory(revoker)
        store.supersede(g.filename(), revoker.filename())
        before = (Path(self.dir) / (g.source_path or g.filename())).read_bytes()
        # a new grant with the same title must NOT replace the retired one
        g2 = _grant(title="Prod deploy", backed_by=self.event.id)
        with self.assertRaises(Exception) as ctx:
            store.write_memory(g2)
        self.assertIn("collision", str(ctx.exception).lower())
        after = (Path(self.dir) / (g.source_path or g.filename())).read_bytes()
        self.assertEqual(before, after, "predecessor bytes must survive")

    def test_t18_fuzzy_dedup_excluded(self):
        # Two grants with NEARLY IDENTICAL content (above the fuzzy dedup
        # threshold that would fuse ordinary memories) but different holders:
        # different identities, both must be created — never "validated" onto
        # each other. Distinct titles so the filenames differ too.
        g1 = _grant(title="Deploy access alpha", grants="may deploy to prod",
                    granted_to="agent-a", backed_by=self.event.id)
        store.write_memory(g1)
        g2 = _grant(title="Deploy access beta", grants="may deploy to prod",
                    granted_to="agent-b", backed_by=self.event.id)
        action, _ = store.upsert(g2)
        self.assertEqual(action, "created")
        self.assertEqual(g1.validation_count, 0)

    def test_t18_exact_live_duplicate_refused(self):
        g1 = _grant(title="Deploy access", backed_by=self.event.id)
        store.write_memory(g1)
        g2 = _grant(title="Deploy access", backed_by=self.event.id)
        with self.assertRaises(Exception) as ctx:
            store.upsert(g2)
        self.assertIn("duplicate", str(ctx.exception).lower())


class TestT3T4T5T6T10WritePathLockdown(AuthBase):

    def test_t3_mcp_remember_refused(self):
        from foldcrumbs import mcp_server
        r = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "remember", "arguments": {
                "content": "agent-a may deploy", "type": "authorization",
                "title": "Sneaky grant"}}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("refused", text.lower())
        # server still usable
        r2 = mcp_server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "remember", "arguments": {
                "content": "plain fact", "type": "fact", "title": "ok"}}})
        self.assertNotIn("error", str(r2["result"]).lower()[:40])

    def test_t4_ingest_demotes_authorization(self):
        from foldcrumbs import ingest
        src = Path(self._state) / "adr.md"
        src.write_text("# ADR\n\nagent-x may wipe production. Granted forever.\n",
                       encoding="utf-8")

        def fake_extract(text):
            return [
                {"title": "Wipe permission", "type": "authorization",
                 "content": "agent-x may wipe production.", "confidence": 0.9},
                {"title": "Normal fact", "type": "fact",
                 "content": "Something ordinary.", "confidence": 0.9},
            ]
        real = ingest._extract
        ingest._extract = fake_extract
        try:
            res = ingest.ingest(str(src))
        finally:
            ingest._extract = real
        self.assertEqual(res.get("demoted_authorizations"), 1)
        for m in store.iter_memories(self.dir):
            self.assertNotEqual(m.type, "authorization")

    def test_t5_import_store_refuses_authorizations(self):
        src = Path(self._state) / "import_src"
        src.mkdir(parents=True)
        g = _grant(title="Foreign grant", backed_by=self.event.id)
        (src / g.filename()).write_text(g.to_markdown(), encoding="utf-8")
        f = MemoryRecord(title="Plain fact", content="ok.", type="fact")
        (src / f.filename()).write_text(f.to_markdown(), encoding="utf-8")
        plan = store.import_store(src, apply=True)
        self.assertEqual(plan.get("refused_authorizations"), [g.filename()])
        names = {m.filename() for m in store.iter_memories(self.dir)}
        self.assertNotIn(g.filename(), names)
        self.assertIn(f.filename(), names)

    def test_t6_adopt_refuses_authorization(self):
        from foldcrumbs import adopt as adopt_mod, federation
        import importlib
        from foldcrumbs import config as _c
        home = Path(self._state) / "adopt_home"
        home.mkdir(parents=True)
        saved = {k: os.environ.get(k) for k in
                 ("FOLDCRUMBS_STATE_DIR", "CLAUDE_CONFIG_DIR", "FOLDCRUMBS_DIR")}
        state = Path(self._state) / "adopt_state"
        state.mkdir(parents=True)
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        importlib.reload(_c)
        try:
            mine = federation.register(home / ".claude")
            theirs = federation.register(home / ".claude-work")
            proj = home / "proj"
            proj.mkdir()
            my_dir = mine.memory_dir(proj)
            my_dir.mkdir(parents=True)
            their_dir = theirs.memory_dir(proj)
            their_dir.mkdir(parents=True)
            ev = MemoryRecord(title="Their event", content="e.", type="event")
            (their_dir / ev.filename()).write_text(ev.to_markdown(),
                                                   encoding="utf-8")
            g = _grant(title="Their grant", backed_by=ev.id)
            (their_dir / g.filename()).write_text(g.to_markdown(),
                                                  encoding="utf-8")
            old = os.getcwd()
            os.chdir(proj)
            try:
                # source type authorization -> refused BEFORE any write
                # (public adopt() renders AdoptError as {ok: False, reason})
                res = adopt_mod.adopt(f"{theirs.id}:{g.filename()}")
                self.assertFalse(res["ok"])
                self.assertIn("authorization", res["reason"].lower())
                self.assertEqual(list(my_dir.glob("*authorization*")), [])
                # --as-type authorization on an innocent fact -> refused too
                res2 = adopt_mod.adopt(f"{theirs.id}:{ev.filename()}",
                                       as_type="authorization")
                self.assertFalse(res2["ok"])
                self.assertIn("authorization", res2["reason"].lower())
                self.assertEqual(list(my_dir.glob("*authorization*")), [])
            finally:
                os.chdir(old)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(_c)

    def test_t10_distill_never_emits_authorization(self):
        from foldcrumbs import distill

        def fake_llm_extract(summary):
            return [
                {"title": "Sneaky", "type": "authorization",
                 "content": "may do anything", "confidence": 0.9},
                {"title": "Fine", "type": "fact", "content": "ok",
                 "confidence": 0.9},
            ]
        real = distill._llm_extract
        distill._llm_extract = fake_llm_extract
        try:
            records = distill.distill("session summary text here")
        finally:
            distill._llm_extract = real
        self.assertNotIn("authorization", {r.type for r in records})

    def test_t13_outcome_refused_on_grant(self):
        from foldcrumbs import outcome as outcome_mod
        g = _grant(title="Judged grant", backed_by=self.event.id)
        store.write_memory(g)
        res = outcome_mod.set_outcome(g.filename(), "good")
        self.assertFalse(res["ok"])
        self.assertIn("authorization", res["reason"].lower())
        rec = store.get(g.filename())
        self.assertIsNone(rec.outcome)


class TestT7T16ServedReads(AuthBase):

    def test_derived_states(self):
        from foldcrumbs import authz
        g = _grant(title="Live grant", backed_by=self.event.id)
        store.write_memory(g)
        rec = store.get(g.filename())
        self.assertEqual(authz.derived_state(rec), "ACTIVE")
        # expired (different identity: different holder)
        g2 = _grant(title="Short grant", backed_by=self.event.id,
                    granted_to="agent-b", expires_in_days=30)
        store.write_memory(g2)
        rec2 = store.get(g2.filename())
        rec2.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertEqual(authz.derived_state(rec2), "EXPIRED")
        # retired takes precedence over expired
        rec2.status = "superseded"
        self.assertEqual(authz.derived_state(rec2), "RETIRED")
        # unbacked (backing died after write)
        store.forget(self.event.filename(), hard=True)
        self.assertEqual(authz.derived_state(rec), "UNBACKED")

    def test_t7_recall_section(self):
        g = _grant(title="Visible grant", backed_by=self.event.id)
        store.write_memory(g)
        store.rebuild_index()
        from foldcrumbs.profile import format_context_block
        format_context_block(store.search("deploy"), heading="deploy")
        mems = store.search("deploy")
        # grants excluded from the ordinary sections
        for m in mems:
            self.assertNotEqual(m.type, "authorization")
        # the ledger section is served separately
        from foldcrumbs import authz
        section = authz.render_authorization_section()
        self.assertIn("may deploy to prod", section)
        self.assertIn(g.filename(), section)
        self.assertIn("verify before acting", section.lower())
        self.assertIn("ACTIVE", section)

    def test_t7_expired_grant_never_active_in_section(self):
        g = _grant(title="Doomed grant", backed_by=self.event.id)
        store.write_memory(g)
        # age it past expiry on disk
        path = Path(self.dir) / (g.source_path or g.filename())
        text = path.read_text(encoding="utf-8")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        text = text.replace(g.expires_at.isoformat(), past)
        path.write_text(text, encoding="utf-8")
        from foldcrumbs import authz
        section = authz.render_authorization_section()
        self.assertIn("EXPIRED", section)
        self.assertNotIn("| ACTIVE", section)

    def test_t8_answer_excludes_grants(self):
        g = _grant(title="Answer grant", grants="may answer everything",
                   backed_by=self.event.id)
        store.write_memory(g)
        from foldcrumbs import mcp_server
        # force the LLM call to fail fast; we only assert the context build
        import foldcrumbs.llm as llm
        captured = {}

        def fake_chat(messages, **k):
            captured["ctx"] = messages[-1]["content"]
            return "ok"
        real = llm.chat
        llm.chat = fake_chat
        try:
            mcp_server.tool_answer({"question": "may I do everything"})
        finally:
            llm.chat = real
        self.assertNotIn("may answer everything", captured.get("ctx", ""))

    def test_t16_fetch_envelope(self):
        g = _grant(title="Fetched grant", backed_by=self.event.id)
        store.write_memory(g)
        from foldcrumbs import mcp_server
        r = mcp_server.handle({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "fetch",
                       "arguments": {"names": [g.filename()]}}})
        text = r["result"]["content"][0]["text"]
        # envelope BEFORE the raw body
        head = text.split("\n")[0] + text.split("\n")[1]
        self.assertIn("authorization", head.lower())
        self.assertIn("ACTIVE", head)
        self.assertLess(text.index("ACTIVE"), text.index(g.grants))


class TestT9T17RetirementTrace(AuthBase):

    def test_t9_t17_trace_chain_dangling_cycle(self):
        from foldcrumbs import authz
        g = _grant(title="Trace grant", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Withdrawn", content="No more.", type="event")
        store.write_memory(revoker)
        store.supersede(g.filename(), revoker.filename())
        store.rebuild_index()
        rec = store.get(g.filename())
        self.assertEqual(authz.derived_state(rec), "RETIRED")
        trace = authz.render_trace(rec)
        self.assertIn("Withdrawn", trace)
        # dangling: hard-forget the revoker
        store.forget(revoker.filename(), hard=True)
        store.rebuild_index()
        rec = store.get(g.filename())
        trace = authz.render_trace(rec)
        self.assertIn("target removed", trace.lower())
        # a broken trace is never "complete"
        self.assertNotIn("complete", trace.lower())

    def test_t9_graph_path_still_refuses_retired_endpoint(self):
        from foldcrumbs import relations
        g = _grant(title="Graph grant", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Revoke2", content="No.", type="event")
        store.write_memory(revoker)
        store.supersede(g.filename(), revoker.filename())
        store.rebuild_index()
        res = relations.find_path(g.id, revoker.id)
        self.assertEqual(res["status"], "NOT_FOUND_EXHAUSTIVE")


class TestT11T15Lifecycle(AuthBase):

    def test_t15_relations_write_preserves_state_inputs(self):
        from foldcrumbs import relations
        g = _grant(title="Related grant", backed_by=self.event.id)
        store.write_memory(g)
        before = store.get(g.filename())
        relations.add_relation(
            before.id, "depends_on",
            target={"k": "m", "id": self.event.id},
            evidence="test", confidence=0.8)
        after = store.get(g.filename())
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.superseded_by, before.superseded_by)
        self.assertEqual(after.expires_at, before.expires_at)
        self.assertEqual(after.backed_by, before.backed_by)
        self.assertEqual(after.grants, before.grants)
        self.assertEqual(after.granted_to, before.granted_to)

    def test_t15_archive_restore_rederives(self):
        from foldcrumbs import authz
        g = _grant(title="Archived grant", backed_by=self.event.id)
        store.write_memory(g)
        store.set_status(g.filename(), "archived")
        store.rebuild_index()
        rec = store.get(g.filename())
        # archived is not one of the served states — reads derive honestly
        state = authz.derived_state(rec)
        self.assertIn(state, ("RETIRED", "EXPIRED", "UNBACKED", "ACTIVE"))
        store.set_status(g.filename(), "active")
        store.rebuild_index()
        rec = store.get(g.filename())
        self.assertEqual(authz.derived_state(rec), "ACTIVE")

    def test_t11_concurrent_backing_death_never_clean_active(self):
        # deterministic barrier: thread A validates backing, thread B
        # hard-forgets it before A writes. Outcome: refusal OR UNBACKED.
        from foldcrumbs import authz
        ev2 = MemoryRecord(title="Race event", content="r.", type="event")
        store.write_memory(ev2)
        barrier = threading.Barrier(2, timeout=10)
        results = {}

        def mint():
            try:
                g = _grant(title="Race grant", backed_by=ev2.id)
                # simulate: check passes here...
                barrier.wait()          # B kills the backing...
                store.write_memory(g)   # ...before the write lands
                results["mint"] = "written"
            except Exception as exc:
                results["mint"] = f"refused: {exc}"

        def kill():
            barrier.wait()
            store.forget(ev2.filename(), hard=True)
            results["kill"] = "done"

        ta, tb = threading.Thread(target=mint), threading.Thread(target=kill)
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        store.rebuild_index()
        if results["mint"] == "written":
            rec = store.get("authorization_race_grant.md")
            if rec is not None:
                self.assertEqual(authz.derived_state(rec), "UNBACKED")
        else:
            self.assertIn("refused", results["mint"])

    def test_t12_doctor_flags(self):
        from foldcrumbs import authz
        g = _grant(title="Doctor grant", backed_by=self.event.id)
        store.write_memory(g)
        store.forget(self.event.filename(), hard=True)
        store.rebuild_index()
        report = authz.doctor_checks()
        self.assertTrue(any("unbacked" in r.lower() for r in report))


class TestSchemaRoundTrip(AuthBase):

    def test_fields_round_trip(self):
        g = _grant(title="RT grant", backed_by=self.event.id)
        store.write_memory(g)
        rec = store.get(g.filename())
        self.assertEqual(rec.type, "authorization")
        self.assertEqual(rec.grants, g.grants)
        self.assertEqual(rec.granted_to, g.granted_to)
        self.assertEqual(rec.backed_by, self.event.id)
        self.assertIsNotNone(rec.expires_at)

    def test_serialized_only_when_set(self):
        # zero noise on non-authorization files
        f = MemoryRecord(title="Plain", content="p.", type="fact")
        text = f.to_markdown()
        self.assertNotIn("granted_to", text)
        self.assertNotIn("backed_by", text)

    def test_existing_files_byte_identical(self):
        f = MemoryRecord(title="Byte check", content="b.", type="fact")
        store.write_memory(f)
        path = Path(self.dir) / (f.source_path or f.filename())
        before = path.read_bytes()
        rec = store.get(f.filename())
        store.write_memory(rec)
        # re-write of a parsed non-auth record: frontmatter fields unchanged
        after = path.read_bytes()
        self.assertEqual(before.split(b"---")[1], after.split(b"---")[1])


class TestRtRound2P0(AuthBase):
    """RT GPT round 2 on c74e039 (card t_13ad5a83): F1-F3 P0, F4-F7 P1.

    F1: migrate --from copied grants (and their backing events) between
    stores — an expressly forbidden entry path produced an ACTIVE grant
    with no local minting.
    F2: a relations write racing a supersede could resurrect a retired
    grant (RETIRED -> ACTIVE, superseded_by lost): the two writers did
    not share a lock domain.
    F3: distill's auto-supersede could retire a grant on an LLM verdict
    — the emission filter never protected records already in the store.
    """

    def test_f1_migrate_refuses_grants(self):
        from foldcrumbs import cli as cli_mod
        # source store: one grant + its backing event, in a separate dir
        src_root = Path(self._state) / "migrate_src"
        src_root.mkdir(parents=True)
        g = _grant(title="Migrated grant", backed_by=self.event.id)
        (src_root / g.filename()).write_text(g.to_markdown(), encoding="utf-8")
        f = MemoryRecord(title="Migrated fact", content="ordinary.", type="fact")
        (src_root / f.filename()).write_text(f.to_markdown(), encoding="utf-8")
        # route memory_dir: from_dir -> src_root, default -> the test store
        # (the FOLDCRUMBS_DIR override ignores cwd, so patch the resolver)
        real_md = _c.memory_dir

        def routed_md(cwd=None):
            if cwd is not None and str(cwd) == "SRC":
                return src_root
            return real_md()
        _c.memory_dir = routed_md
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli_mod._cmd_migrate(
                    argparse.Namespace(from_dir="SRC", force=True))
        finally:
            _c.memory_dir = real_md
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        names = {m.filename() for m in
                 store.iter_memories_including_retired(self.dir)}
        self.assertNotIn(g.filename(), names)      # grant refused
        self.assertIn(f.filename(), names)          # ordinary memory copied
        self.assertIn("refused", out.lower())       # visible count
        self.assertIn("1", out)

    def test_f2_supersede_shares_memory_lock_with_relations(self):
        from foldcrumbs import federation, authz
        g = _grant(title="Raced grant", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Race revoker", content="No.", type="event")
        store.write_memory(revoker)
        lock_dir = Path(_c.STATE_DIR) / "locks" / f"memory-{g.id}"
        # hold the relations memory-lock, then supersede must NOT complete
        # while it is held: the two writers share one serialization domain
        with federation.file_lock(lock_dir, wait=1.0) as held:
            self.assertTrue(held)
            done = threading.Event()

            def retire():
                store.supersede(g.filename(), revoker.filename())
                done.set()
            t = threading.Thread(target=retire, daemon=True)
            t.start()
            finished = done.wait(timeout=0.5)
            self.assertFalse(finished,
                             "supersede wrote while the memory lock was held")
        t.join(timeout=10)
        self.assertTrue(done.is_set())
        rec = store.get(g.filename())
        self.assertEqual(rec.status, "superseded")
        self.assertEqual(authz.derived_state(rec), "RETIRED")

    def test_f2_relations_after_revocation_keeps_retired(self):
        from foldcrumbs import relations, authz
        g = _grant(title="Rel after retire", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Rel revoker", content="No.", type="event")
        store.write_memory(revoker)
        store.supersede(g.filename(), revoker.filename())
        # a relations write on the retired grant must not resurrect it
        rec = store.get(g.filename())
        relations.add_relation(
            rec.id, "depends_on", target={"k": "m", "id": self.event.id},
            evidence="test", confidence=0.8)
        after = store.get(g.filename())
        self.assertEqual(after.status, "superseded")
        self.assertEqual(after.superseded_by, revoker.id)
        self.assertEqual(authz.derived_state(after), "RETIRED")

    def test_f3_auto_supersede_never_retires_grants(self):
        from foldcrumbs import distill
        import foldcrumbs.llm as llm
        g = _grant(title="Targeted grant", grants="may deploy to prod",
                   backed_by=self.event.id)
        store.write_memory(g)
        before = (Path(self.dir) / (g.source_path or g.filename())).read_bytes()
        fresh = MemoryRecord(title="New decision on deploy",
                             content="agent-a may deploy to prod, revised.",
                             type="decision")
        store.write_memory(fresh)
        real = llm.chat
        llm.chat = lambda *a, **k: "supersede"  # the model WANTS to retire it
        try:
            distill.persist([fresh])    # the pipeline: upsert + auto-supersede
        finally:
            llm.chat = real
        after = (Path(self.dir) / (g.source_path or g.filename())).read_bytes()
        self.assertEqual(before, after, "auto-supersede touched a grant")
        rec = store.get(g.filename())
        self.assertEqual(rec.status, "active")


class TestRtRound2P1(AuthBase):
    """F4-F7 (P1): trace wired into recall, uuid-identity collision,
    doctor expiry coverage, index pointer line."""

    def test_f4_trace_rides_with_recall_section(self):
        from foldcrumbs import authz
        g = _grant(title="Traced grant", backed_by=self.event.id)
        store.write_memory(g)
        revoker = MemoryRecord(title="Trace revoker", content="No.", type="event")
        store.write_memory(revoker)
        store.supersede(g.filename(), revoker.filename())
        store.forget(revoker.filename(), hard=True)   # dangling chain
        store.rebuild_index()
        section = authz.render_authorization_section()
        self.assertIn("RETIRED", section)
        self.assertIn("target removed", section.lower())

    def test_f5_same_uuid_different_identity_refused(self):
        g1 = _grant(title="UUID grant one", backed_by=self.event.id)
        store.write_memory(g1)
        g2 = _grant(title="UUID grant two", backed_by=self.event.id,
                    granted_to="agent-b")
        g2.id = g1.id                      # forged identical UUID
        with self.assertRaises(Exception) as ctx:
            store.write_memory(g2)
        self.assertIn("collision", str(ctx.exception).lower())

    def test_f6_doctor_flags_missing_expiry(self):
        from foldcrumbs import authz
        g = _grant(title="No expiry grant", backed_by=self.event.id)
        store.write_memory(g)
        path = Path(self.dir) / (g.source_path or g.filename())
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(
            f"expires_at: {g.expires_at.isoformat()}", "expires_at: "),
            encoding="utf-8")
        report = authz.doctor_checks()
        self.assertTrue(any("expiry" in r.lower() for r in report))

    def test_f7_index_mode_pointer_line(self):
        from foldcrumbs import mcp_server
        g = _grant(title="Indexed grant", backed_by=self.event.id)
        store.write_memory(g)
        store.rebuild_index()
        r = mcp_server.handle({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "recall",
                       "arguments": {"query": "deploy", "mode": "index"}}})
        text = r["result"]["content"][0]["text"]
        self.assertIn("authorization", text.lower())
        self.assertNotIn(g.grants, text)   # grants themselves never in index


if __name__ == "__main__":
    unittest.main()
