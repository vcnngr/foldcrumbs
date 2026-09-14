"""Contested visibility in recall (mini-feature, TDD).

Gap surfaced publicly (@oaleviola on X, 2026-09-14): recall hides a
contested record silently — the agent never learns there was a dispute.
Invalidation contracts got an honest diagnostics tail (INV design §D3);
contested records get the same treatment here: a "matched but not
served" line naming the claim, capped at 3, obeying the same filters
and relevance as the served list (PR64 RT F5 lesson), and NEVER part
of answer's LLM context.

Claims stay directional and unsigned-verdicts stay out: the line says
who claimed, not who was right; exiting the contested state remains a
human verb via `foldcrumbs conflicts`.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from test_foldcrumbs import _FederationEnv  # noqa: E402

from foldcrumbs import cli, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class _ContestedEnv(_FederationEnv):
    """Two instances; 'other' holds a memory THIS store declared obsolete
    via supersedes_external — the exact shape of the public gap.

    Mirrors TestFederatedSearch's scaffold without inheriting its tests.
    """

    def setUp(self):
        super().setUp()
        from foldcrumbs import store as _store
        self.store = _store
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)
        self.federation.register(self._root(".claude"))
        self.other = self.federation.register(self._root(".claude-work"))
        self.stale = self._write(
            self.other, "Deploy on Mondays",
            "Deploys run Mondays only, every monday morning.")
        self.claim = MemoryRecord(
            title="Deploys moved to Friday",
            content="Deploys run Fridays now, not monday morning.",
            type="fact")
        self.claim.supersedes_external = [
            f"{self.other.id}:{self.stale.filename()}"]
        store.write_memory(self.claim, self.proj)

    def _drain_scans(self, release=None):
        if release is not None:
            release.set()
        with self.store._pending_lock:
            threads = [v["thread"]
                       for v in self.store._pending_scans.values()]
            threads += [t for ts in self.store._stuck_roots.values()
                        for t in ts]
            threads += list(self.store._root_busy.values())
        for t in threads:
            t.join(10)
        with self.store._pending_lock:
            self.store._pending_scans.clear()
            self.store._stuck_roots.clear()
            self.store._root_busy.clear()

    def _write(self, ref, title, content, type_="fact"):
        d = ref.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title=title, content=content, type=type_)
        (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        return rec

    def tearDown(self):
        self._drain_scans()
        super().tearDown()


class TestSearchCollector(_ContestedEnv):
    """C1-C3: the store-level collector."""

    def test_c1_contested_hit_lands_in_collector(self):
        collected: list = []
        top = store.search("deploys run monday morning", cwd=self.proj,
                           collect_contested=collected)
        served = [m.title for m in top]
        self.assertIn("Deploys moved to Friday", served)
        self.assertNotIn("Deploy on Mondays", served)
        # the gap: the dispute is now VISIBLE to the caller
        self.assertEqual(len(collected), 1)
        rec, claim_title = collected[0]
        self.assertEqual(rec.title, "Deploy on Mondays")
        self.assertEqual(claim_title, "Deploys moved to Friday")

    def test_c2_irrelevant_contested_not_collected(self):
        # F5 lesson: the collector obeys the same relevance as the served
        # list — a contested record the query would never surface is not
        # "matched but not served"
        collected: list = []
        store.search("completely unrelated topic xyz", cwd=self.proj,
                     collect_contested=collected)
        self.assertEqual(collected, [])

    def test_c3_include_contested_disables_collection(self):
        # explicit opt-in serves the record; nothing is withheld, so
        # nothing is collected
        collected: list = []
        top = store.search("deploys run monday morning", cwd=self.proj,
                           include_contested=True,
                           collect_contested=collected)
        self.assertIn("Deploy on Mondays", [m.title for m in top])
        self.assertEqual(collected, [])

    def test_c4_no_collector_no_behavior_change(self):
        # default call: identical served list with or without the feature
        top = store.search("deploys run monday morning", cwd=self.proj)
        self.assertIn("Deploys moved to Friday", [m.title for m in top])
        self.assertNotIn("Deploy on Mondays", [m.title for m in top])


class TestCliRecallTail(_ContestedEnv):
    """C5-C6: the CLI recall renders the honest line."""

    def _recall(self, argv):
        buf = io.StringIO()
        cwd = __import__("os").getcwd()
        __import__("os").chdir(self.proj)
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(argv)
        finally:
            __import__("os").chdir(cwd)
        return buf.getvalue()

    def test_c5_full_recall_names_the_dispute(self):
        out = self._recall(["recall", "deploys run monday morning"])
        self.assertIn("Deploy on Mondays", out)
        self.assertIn("matched but not served", out)
        self.assertIn("contested", out)
        # names the claimer — the dispute has a WHO, it is not unsigned
        self.assertIn("Deploys moved to Friday", out)
        # points at the human exit
        self.assertIn("conflicts", out)

    def test_c6_index_recall_counts_withheld(self):
        out = self._recall(["recall", "deploys run monday morning",
                            "--index"])
        self.assertIn("contested", out)
        self.assertIn("1", out)


class TestMcpRecallTail(_ContestedEnv):
    """C7-C9: the MCP surface serves the same honest view."""

    def _call(self, id_, tool, **args):
        # MCP tools resolve cwd from the process — same scaffold as
        # test_recall_layers (chdir into the project store)
        import os
        from test_recall_layers import _call as _c, _text as _t
        old = os.getcwd()
        os.chdir(self.proj)
        try:
            return _t(_c(id_, tool, **args))
        finally:
            os.chdir(old)

    def test_c7_full_mode_contains_dispute_line(self):
        out = self._call(21, "recall",
                         query="deploys run monday morning")
        self.assertIn("matched but not served", out)
        self.assertIn("Deploy on Mondays", out)
        self.assertIn("Deploys moved to Friday", out)

    def test_c8_index_mode_counts_withheld(self):
        out = self._call(22, "recall",
                         query="deploys run monday morning",
                         mode="index")
        self.assertIn("contested", out)

    def test_c9_answer_context_never_sees_the_tail(self):
        # same posture as INV §D3: diagnostics ride the recall tail, but
        # answer's LLM context is served memories only — a withheld-dispute
        # line is not evidence
        from foldcrumbs import mcp_server
        seen = {}

        def fake_search(query, limit=10, types=None, tags=None,
                        collect_invalidated=None, collect_contested=None,
                        **kw):
            seen["contested_passed"] = collect_contested is not None
            return store.search(query, limit=limit, types=types, tags=tags,
                                collect_invalidated=collect_invalidated,
                                collect_contested=collect_contested,
                                cwd=self.proj)

        real = mcp_server._search
        mcp_server._search = fake_search
        try:
            from foldcrumbs import llm
            real_chat = llm.chat
            captured = {}

            def cap(messages=None, **kw):
                captured["prompt"] = "\n".join(
                    m.get("content", "") for m in (messages or []))
                return "answer text"
            llm.chat = cap
            try:
                import os
                cwd = os.getcwd()
                os.chdir(self.proj)
                try:
                    mcp_server.tool_answer(
                        {"question": "when do deploys run"})
                finally:
                    os.chdir(cwd)
            finally:
                llm.chat = real_chat
        finally:
            mcp_server._search = real
        # answer must NOT collect (and therefore not render) the tail
        self.assertFalse(seen.get("contested_passed", False),
                         "answer passed a contested collector to search")


class TestCapThree(_ContestedEnv):
    """C10: max 3 lines + 'showing 3 of N' — same contract as INV §D3."""

    def test_ten_contested_cap_at_three(self):
        # create 9 more contested foreign records + claims
        for i in range(9):
            stale = self._write(
                self.other, f"Stale fact {i}",
                f"old deploy rule number {i} monday morning.")
            claim = MemoryRecord(
                title=f"New rule {i}",
                content=f"deploy rule {i} moved to friday.", type="fact")
            claim.supersedes_external = [
                f"{self.other.id}:{stale.filename()}"]
            store.write_memory(claim, self.proj)
        collected: list = []
        store.search("deploy rule monday morning", cwd=self.proj,
                     collect_contested=collected)
        self.assertGreaterEqual(len(collected), 4)
        buf = io.StringIO()
        cwd = __import__("os").getcwd()
        __import__("os").chdir(self.proj)
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(["recall", "deploy rule monday morning"])
        finally:
            __import__("os").chdir(cwd)
        out = buf.getvalue()
        self.assertIn("showing 3 of", out)
        self.assertEqual(out.count("matched but not served"), 3)


if __name__ == "__main__":
    unittest.main()
