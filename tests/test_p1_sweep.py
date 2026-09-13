"""P1 sweep — red-first tests pinning the reviewer-declared P1 backlog.

Sources (all declared P1/non-blocking by the RT gates, none ever vetoed):
- PR #64 RT r1 F5 (card t_b119870a→r1 t_145de292 report §F5): the recall
  diagnostics collector ran BEFORE query/types/tags selection, so a record
  irrelevant to the query could appear as "matched but not served".
- PR #64 RT r1 F6: find_conflict_candidates used only _visible — an
  invalidated memory stayed in the conflict queue.
- PR #64 RT r1 F7: three fetches = three full scans; the authz ledger
  built one ReadContext per grant; timeline anchor and rows built
  separate contexts.
- PR #66 RT r2 residual (report §5): a legacy source without updated_at
  kept status=fresh — the uncertainty lived only in the detail string.
- FL-3 backlog: MCP adopt repo-note test coverage beyond refusal.
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

from foldcrumbs import config as _c  # noqa: E402,F401
from foldcrumbs import relations, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402
from test_recall_layers import _call, _text  # noqa: E402
from test_adopt_check_fresh import CheckFreshEnv  # noqa: E402


def _rec(title, content, type_="fact", **kw):
    return MemoryRecord(title=title, content=content, type=type_, **kw)


class _ContractedPair(TmpStore):
    """B alive; A contracted on B; helper to kill B."""

    def setUp(self):
        super().setUp()
        self.b = _rec("Stg cluster", "We use the stg cluster for staging.",
                      type_="decision")
        store.write_memory(self.b)
        self.a = _rec("Staging URL",
                      "The staging URL is https://stg.example.com.",
                      type_="fact")
        store.write_memory(self.a)
        relations.add_relation(
            self.a.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="URL exists only while the cluster does",
            confidence=0.9, prov="manual")
        store.rebuild_index()

    def _kill_b(self):
        nb = _rec("Stg cluster v2", "Staging moved to k8s.", type_="decision")
        store.write_memory(nb)
        self.assertTrue(store.supersede(self.b.filename(), nb.filename()))
        store.rebuild_index()


class TestF5CollectorRespectsFilters(_ContractedPair):
    """PR64-F5: the diagnostics tail must not claim 'matched' for records
    the query/types/tags would never have served."""

    def test_irrelevant_query_produces_no_diagnostic(self):
        self._kill_b()
        diag = []
        # query unrelated to A's content: A must NOT appear as "matched"
        store.search("kubernetes ingress controller",
                     limit=5, collect_invalidated=diag)
        for rec, _outcome, _detail in diag:
            self.assertNotEqual(rec.filename(), self.a.filename(),
                                "collector listed a record the query "
                                "does not match")

    def test_type_filter_applies_to_collector(self):
        self._kill_b()
        diag = []
        # A is a fact; asking for decisions must not surface it
        store.search("staging url", limit=5, types=["decision"],
                     collect_invalidated=diag)
        for rec, _o, _d in diag:
            self.assertNotEqual(rec.type, "fact",
                                "collector bypassed the type filter")

    def test_relevant_query_still_diagnoses(self):
        # the honest case must survive the fix
        self._kill_b()
        diag = []
        store.search("staging url", limit=5, collect_invalidated=diag)
        self.assertTrue(any(r.filename() == self.a.filename()
                            for r, _o, _d in diag),
                        "relevant invalidated record missing from tail")


class TestF6ConflictCandidatesDerive(_ContractedPair):
    """PR64-F6: the conflict queue must not contain invalidated memories."""

    def test_invalidated_candidate_excluded(self):
        self._kill_b()
        probe = _rec("Staging endpoint",
                     "Something about the staging URL on stg.example.com.",
                     type_="fact")
        cands = store.find_conflict_candidates(probe, limit=5)
        self.assertNotIn(self.a.filename(),
                         [m.filename() for m in cands],
                         "invalidated memory offered as conflict candidate")

    def test_valid_candidate_still_found(self):
        probe = _rec("Staging endpoint",
                     "Something about the staging URL on stg.example.com.",
                     type_="fact")
        cands = store.find_conflict_candidates(probe, limit=5)
        self.assertIn(self.a.filename(), [m.filename() for m in cands],
                      "healthy contracted memory dropped from candidates")


class TestF7ContextReuse(_ContractedPair):
    """PR64-F7: one ReadContext per OPERATION, not per record/fetch."""

    def test_multi_fetch_single_scan(self):
        from foldcrumbs import invalidation as inv
        # a second contracted memory: fetch of TWO contract-carrying
        # names must still build at most ONE context (per operation)
        a2 = _rec("Staging port", "Staging runs on port 8443 at stg.",
                  type_="fact")
        store.write_memory(a2)
        relations.add_relation(
            a2.id, "invalidated_by",
            target={"k": "m", "id": self.b.id},
            evidence="port dies with the cluster", confidence=0.9,
            prov="manual")
        self._kill_b()
        c = _rec("Payments note", "Payments oncall notes.", type_="fact")
        store.write_memory(c)
        calls = []
        real = inv.ReadContext.for_store

        def spy(cwd=None):
            calls.append(cwd)
            return real(cwd)

        inv.ReadContext.for_store = classmethod(
            lambda cls, cwd=None: spy(cwd) or real(cwd))
        try:
            _text(_call(1, "fetch", names=[
                self.a.filename(), a2.filename(), c.filename()]))
        finally:
            inv.ReadContext.for_store = real
        self.assertLessEqual(len(calls), 1,
                             f"fetch built {len(calls)} contexts for 3 names")

    def test_ledger_single_context(self):
        from foldcrumbs import authz, invalidation as inv
        ev = _rec("Approval", "Approved: agent-a may deploy.", type_="event")
        store.write_memory(ev)
        grants = []
        for i in range(3):
            g = MemoryRecord(
                title=f"Grant {i}", content=f"agent-a may do {i}.",
                type="authorization", grants=f"may do {i}",
                granted_to="agent-a", backed_by=ev.id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
                provenance="explicit_statement")
            authz.mint(g)
            grants.append(g)
        calls = []
        real = inv.ReadContext.for_store

        def spy(cwd=None):
            calls.append(cwd)
            return real(cwd)

        inv.ReadContext.for_store = classmethod(
            lambda cls, cwd=None: spy(cwd) or real(cwd))
        try:
            authz.render_authorization_section()
        finally:
            inv.ReadContext.for_store = real
        self.assertLessEqual(len(calls), 1,
                             f"ledger built {len(calls)} contexts for 3 grants")


class TestPr66LegacyTimestampStatus(unittest.TestCase):
    """PR66 residual: a legacy source without updated_at must not claim
    'fresh' in the status field — uncertainty belongs in the status."""

    def test_missing_updated_at_not_fresh_status(self):
        import importlib
        import tempfile
        from foldcrumbs import adopt as adopt_mod, federation
        from foldcrumbs import config as cfg

        state = Path(tempfile.mkdtemp(prefix="p1s_state_"))
        home = Path(tempfile.mkdtemp(prefix="p1s_home_"))
        saved = {k: os.environ.get(k) for k in
                 ("FOLDCRUMBS_STATE_DIR", "CLAUDE_CONFIG_DIR",
                  "FOLDCRUMBS_DIR", "ENGRAM_DIR", "ENGRAM_STATE_DIR")}
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ.pop("ENGRAM_STATE_DIR", None)
        importlib.reload(cfg)
        try:
            mine = federation.register(home / ".claude")
            theirs = federation.register(home / ".claude-work")
            proj = home / "proj"
            proj.mkdir(parents=True, exist_ok=True)
            my_dir = mine.memory_dir(proj)
            my_dir.mkdir(parents=True, exist_ok=True)
            their_dir = theirs.memory_dir(proj)
            their_dir.mkdir(parents=True, exist_ok=True)
            src = MemoryRecord(title="Legacy note",
                               content="A note without updated_at.",
                               type="fact")
            (their_dir / src.filename()).write_text(src.to_markdown(),
                                                    encoding="utf-8")
            res = adopt_mod.adopt(f"{theirs.id}:{src.filename()}",
                                  cwd=proj)
            self.assertTrue(res["ok"], res)
            # strip updated_at from the source (legacy record)
            p = their_dir / src.filename()
            text = p.read_text(encoding="utf-8")
            stripped = "\n".join(ln for ln in text.splitlines()
                                 if not ln.startswith("updated_at:"))
            p.write_text(stripped, encoding="utf-8")
            rows = adopt_mod.check_fresh(cwd=proj)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertNotEqual(r["status"], "fresh",
                                "legacy timestamp uncertainty not in status")
            self.assertIn("timestamp", r["detail"].lower())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(cfg)


class TestFl3McpAdoptNote(CheckFreshEnv):
    """FL-3 P1 backlog: the MCP adopt note was only tested on the refusal
    path; pin the happy path — the note lands in the ledger."""

    def test_mcp_adopt_note_persisted(self):
        import json as _json
        src2 = self._theirs(title="Retry policy",
                            content="Retries use exponential backoff.",
                            type_="decision")
        old_cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            out = _text(_call(9, "adopt",
                              ref=f"{self.theirs.id}:{src2.filename()}",
                              note="held in the oncall retro"))
        finally:
            os.chdir(old_cwd)
        self.assertIn("adopted", out)
        led = _json.loads(self._ledger_path().read_text(encoding="utf-8"))
        entry = next(e for e in led.values()
                     if e["memory_id"] == src2.id)
        self.assertEqual(entry["note"], "held in the oncall retro")

    def test_mcp_adopt_default_note(self):
        src3 = self._theirs(title="Timeout policy",
                            content="Requests time out at 30s.",
                            type_="decision")
        old_cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            out = _text(_call(10, "adopt",
                              ref=f"{self.theirs.id}:{src3.filename()}"))
        finally:
            os.chdir(old_cwd)
        self.assertIn("adopted", out)
        import json as _json
        led = _json.loads(self._ledger_path().read_text(encoding="utf-8"))
        entry = next(e for e in led.values()
                     if e["memory_id"] == src3.id)
        self.assertEqual(entry["note"], "adopted via MCP (agent)")


if __name__ == "__main__":
    unittest.main()
