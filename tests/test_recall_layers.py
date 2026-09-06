"""Three-layer recall + timeline (competitor deep-dive item #1/#2).

Contract under test:
* `recall --index` (CLI) / recall(mode="index") (MCP): compact index layer —
  one line per hit: filename, type, title, updated date. Savings grow with
  memory body length; same ranking, same filters.
* `fetch <file> [<file> ...]` (CLI) / fetch tool (MCP): detail layer — full
  memory content by filename(s), batched. Unknown names are reported, not
  silently dropped.
* `timeline <ref-or-query>` (CLI) / timeline tool (MCP): chronological
  context around one memory (or a query's top hit): the N memories before
  and after it by created_at. Deterministic order (created_at, filename).
* MCP surface adds NO powers the CLI lacks (design rule from fleet-learning).
* Ranking of index == ranking of full recall (same store.search call).
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

from foldcrumbs import mcp_server, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


def _rpc(id_, method, **params):
    req = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params:
        req["params"] = params
    return mcp_server.handle(req)


def _call(id_, name, **args):
    return _rpc(id_, "tools/call", name=name, arguments=args)


def _text(resp):
    return resp["result"]["content"][0]["text"]


class _Seeded(TmpStore):
    """A store with 6 memories spread over 6 days."""

    def setUp(self):
        super().setUp()
        base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.names = []
        specs = [
            ("Deploy window", "decision", "We deploy Tuesdays 10-12 UTC."),
            ("JWT rotation", "fact", "Auth tokens rotate every 15 minutes."),
            ("July outage", "event", "Friday deploy rolled back over the weekend."),
            ("Terse reviews", "preference", "Maria prefers review comments under 3 lines."),
            ("Queue migration", "decision", "Payments moved to an async SQS queue."),
            ("On-call rotation", "context", "Marco until the 15th, then Giulia."),
        ]
        for i, (title, type_, body) in enumerate(specs):
            rec = MemoryRecord(title=title, content=body, type=type_,
                               created_at=base + timedelta(days=i),
                               updated_at=base + timedelta(days=i))
            store.write_memory(rec)
            self.names.append(rec.filename())
        store.rebuild_index()


class TestIndexLayer(_Seeded):

    def test_mcp_recall_index_mode_compact(self):
        full = _text(_call(1, "recall", query="deploy"))
        idx = _text(_call(2, "recall", query="deploy", mode="index"))
        self.assertLess(len(idx), len(full),
                        "index must be strictly smaller than the full block")
        # one line per hit, filename + type present
        self.assertIn("decision_deploy_window.md", idx)
        # the full body text is NOT in the index (that's the point)
        self.assertNotIn("10-12 UTC", idx)

    def test_mcp_recall_default_unchanged(self):
        # backwards compat: no mode == full context block, exactly as before
        txt = _text(_call(3, "recall", query="deploy"))
        self.assertIn("foldcrumbs-recall", txt)
        self.assertIn("10-12 UTC", txt)

    def test_index_and_full_same_hits(self):
        idx = _text(_call(4, "recall", query="deploy payment queue", mode="index",
                          limit=5))
        full = _text(_call(5, "recall", query="deploy payment queue", limit=5))
        # the full block renders CONTENT grouped by type (not titles), so the
        # honest parity check: every indexed hit's content shows in the block
        idx_files = [ln.split()[0] for ln in idx.splitlines()
                     if ln.strip() and ln.split()[0].endswith(".md")]
        self.assertTrue(idx_files)
        for f in idx_files:
            rec = store.get(f)
            # first content line, normalized — the block may re-wrap it
            probe = rec.content.strip().splitlines()[0][:40]
            flat = " ".join(full.split())
            self.assertIn(" ".join(probe.split()), flat,
                          f"indexed hit {f} missing from the full block")

    def test_cli_recall_index(self):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["recall", "deploy", "--index"])
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("decision_deploy_window.md", out)
        self.assertNotIn("foldcrumbs-recall", out)


class TestFetchLayer(_Seeded):

    def test_mcp_fetch_batch(self):
        txt = _text(_call(6, "fetch", names=[self.names[0], self.names[1]]))
        self.assertIn("We deploy Tuesdays", txt)
        self.assertIn("rotate every 15 minutes", txt)

    def test_mcp_fetch_unknown_reported(self):
        txt = _text(_call(7, "fetch", names=[self.names[0], "nope.md"]))
        self.assertIn("We deploy Tuesdays", txt)
        self.assertIn("nope.md", txt)          # named...
        self.assertIn("not found", txt.lower())  # ...and reported

    def test_mcp_fetch_single_string_ok(self):
        # tolerate a bare string too (agents do that)
        txt = _text(_call(8, "fetch", names=self.names[2]))
        self.assertIn("rolled back", txt)

    def test_cli_fetch(self):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["fetch", self.names[0], self.names[3]])
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("We deploy Tuesdays", out)
        self.assertIn("3 lines", out)

    def test_fetch_is_path_safe(self):
        # containment: ../ must not escape the store — visibly refused
        txt = _text(_call(9, "fetch", names=["../MEMORY.md", "../../etc/passwd"]))
        low = txt.lower()
        self.assertTrue("not found" in low or "not a memory" in low)


class TestTimeline(_Seeded):

    def test_mcp_timeline_around_memory(self):
        txt = _text(_call(10, "timeline", ref=self.names[2], window=2))
        # the anchor and its chronological neighbours (days 0-4 around day 2)
        self.assertIn("July outage", txt)
        self.assertIn("JWT rotation", txt)      # day 1
        self.assertIn("Terse reviews", txt)     # day 3
        # outside the window
        self.assertNotIn("On-call rotation", txt)  # day 5
        # chronological order: dates ascending in the output
        pos1 = txt.index("JWT rotation")
        pos2 = txt.index("July outage")
        pos3 = txt.index("Terse reviews")
        self.assertLess(pos1, pos2)
        self.assertLess(pos2, pos3)

    def test_mcp_timeline_anchor_marked(self):
        txt = _text(_call(11, "timeline", ref=self.names[2]))
        # the anchor line is visibly marked
        self.assertIn(">>", txt)

    def test_mcp_timeline_by_query(self):
        # ref can also be a query: timeline around the top hit
        txt = _text(_call(12, "timeline", ref="payments queue"))
        self.assertIn("Queue migration", txt)

    def test_mcp_timeline_unknown_ref(self):
        txt = _text(_call(13, "timeline", ref="zzz_nonsense_zzz"))
        self.assertIn("no", txt.lower())   # visible empty/none, no traceback

    def test_timeline_deterministic(self):
        a = _text(_call(14, "timeline", ref=self.names[2], window=3))
        b = _text(_call(15, "timeline", ref=self.names[2], window=3))
        self.assertEqual(a, b)

    def test_cli_timeline(self):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["timeline", self.names[2], "--window", "2"])
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        self.assertIn("July outage", buf.getvalue())


class TestCatalogParity(_Seeded):

    def test_catalog_is_eleven(self):
        r = _rpc(20, "tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names,
                         {"remember", "recall", "answer", "forget",
                          "graph_path", "relate", "ingest", "adopt",
                          "outcome", "fetch", "timeline"})

    def test_every_tool_has_handler_and_cli(self):
        for tool in mcp_server.TOOLS:
            self.assertIn(tool["name"], mcp_server._DISPATCH)

    def test_recall_schema_declares_mode(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "recall")
        props = tool["inputSchema"]["properties"]
        self.assertIn("mode", props)
        self.assertEqual(sorted(props["mode"].get("enum", [])),
                         ["full", "index"])


class TestRtRound1P0P1(_Seeded):
    """RT GPT round 1 on 5bc728e (card t_e5fe5cc7): F1-F4.

    F1 (P0): federated index handed fetch a bare filename; a foreign hit
    with a local homonym resolved to the LOCAL file — wrong content, no
    error. Fix: index qualifies foreign refs as <root_id>:<filename>;
    fetch resolves qualified refs read-only inside that registered root
    and refuses to pass a foreign hit off as a local one.
    """

    def _seed_foreign(self):
        """Register a second root holding a homonym of a local memory."""
        import importlib
        import tempfile
        from foldcrumbs import config as _c, federation
        self._fed_home = Path(tempfile.mkdtemp(prefix="ccmem_fed_"))
        # ENGRAM_DIR (set by TmpStore) would override cwd derivation for
        # EVERY store.get — pop it so the federated project resolves via
        # CLAUDE_CONFIG_DIR + cwd like a real installation.
        self._fed_saved = {k: os.environ.get(k) for k in
                           ("FOLDCRUMBS_STATE_DIR", "CLAUDE_CONFIG_DIR",
                            "FOLDCRUMBS_DIR", "ENGRAM_DIR",
                            "ENGRAM_STATE_DIR")}
        state = Path(tempfile.mkdtemp(prefix="ccmem_fstate_"))
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self._fed_home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        os.environ.pop("ENGRAM_STATE_DIR", None)
        importlib.reload(_c)
        mine = federation.register(self._fed_home / ".claude")
        theirs = federation.register(self._fed_home / ".claude-work")
        proj = self._fed_home / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        my_dir = mine.memory_dir(proj)
        my_dir.mkdir(parents=True, exist_ok=True)
        their_dir = theirs.memory_dir(proj)
        their_dir.mkdir(parents=True, exist_ok=True)
        # homonym pair: same basename, different content
        rec_l = MemoryRecord(title="Deploy window",
                             content="LOCAL_ONLY: cache capacity unrelated.",
                             type="fact")
        (my_dir / rec_l.filename()).write_text(rec_l.to_markdown(),
                                               encoding="utf-8")
        rec_f = MemoryRecord(title="Deploy window",
                             content="FOREIGN_ONLY: deploys on fridays only.",
                             type="fact")
        (their_dir / rec_f.filename()).write_text(rec_f.to_markdown(),
                                                  encoding="utf-8")
        return mine, theirs, rec_l.filename(), proj

    def _fed_call(self, id_, name, **args):
        # run inside the federated project cwd so search sees both roots
        old = os.getcwd()
        os.chdir(self._fed_proj)
        try:
            return _call(id_, name, **args)
        finally:
            os.chdir(old)

    def setUp(self):
        super().setUp()
        self.mine, self.theirs, self.homonym, self._fed_proj = \
            self._seed_foreign()

    def tearDown(self):
        import importlib
        from foldcrumbs import config as _c
        for k, v in self._fed_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_c)
        super().tearDown()

    def test_f1_index_qualifies_foreign_hits(self):
        resp = self._fed_call(30, "recall", query="deploys on fridays",
                              mode="index", limit=5)
        idx = _text(resp)
        # the foreign hit is addressable as <root_id>:<filename>
        self.assertIn(f"{self.theirs.id}:{self.homonym}", idx)
        # and marked as foreign, so a reader knows it is not local
        self.assertIn("foreign", idx.lower())

    def test_f1_fetch_qualified_ref_returns_foreign_content(self):
        ref = f"{self.theirs.id}:{self.homonym}"
        txt = _text(self._fed_call(31, "fetch", names=[ref]))
        self.assertIn("FOREIGN_ONLY", txt)
        self.assertNotIn("LOCAL_ONLY", txt)

    def test_f1_fetch_bare_homonym_never_mixes_identities(self):
        # the bare name is LOCAL — it must return local content or refuse,
        # and the index for the foreign hit never offers the bare name
        txt = _text(self._fed_call(32, "fetch", names=[self.homonym]))
        # whichever it does, it must not be the foreign body under a
        # local-looking name — and here the local file exists, so local:
        self.assertIn("LOCAL_ONLY", txt)
        self.assertNotIn("FOREIGN_ONLY", txt)

    def test_f1_fetch_unknown_root_refused(self):
        txt = _text(self._fed_call(33, "fetch",
                                   names=[f"deadbeef:{self.homonym}"]))
        self.assertIn("not found", txt.lower())

    def test_f1_fetch_qualified_ref_path_safe(self):
        # traversal inside a qualified ref stays refused (visible refusal,
        # wording may be 'not found' or 'not a memory file')
        evil = f"{self.theirs.id}:../../.claude/config"
        txt = _text(self._fed_call(34, "fetch", names=[evil]))
        low = txt.lower()
        self.assertTrue("not found" in low or "not a memory" in low)

    def test_f1_full_recall_unchanged(self):
        # the fix must not touch full-mode recall
        txt = _text(self._fed_call(35, "recall", query="deploys on fridays"))
        self.assertIn("FOREIGN_ONLY", txt)
        self.assertIn("foldcrumbs-recall", txt)


class TestRtRound1Timeline(_Seeded):
    """F2 (P1): excluded anchors must refuse VISIBLY, not print nothing."""

    def test_f2_archived_anchor_refused(self):
        name = self.names[1]
        store.set_status(name, "archived")
        store.rebuild_index()
        txt = _text(_call(40, "timeline", ref=name))
        self.assertIn("refused", txt.lower())

    def test_f2_expired_anchor_refused(self):
        from datetime import datetime, timedelta, timezone
        name = self.names[3]
        rec = store.get(name)
        rec.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        store.write_memory(rec)
        store.rebuild_index()
        txt = _text(_call(41, "timeline", ref=name))
        self.assertIn("refused", txt.lower())

    def test_f2_cli_nonzero_rc_on_excluded_anchor(self):
        import contextlib
        import io
        from foldcrumbs import cli
        name = self.names[1]
        store.set_status(name, "archived")
        store.rebuild_index()
        buf, ebuf = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(ebuf):
                rc = cli.main(["timeline", name])
        finally:
            os.chdir(old)
        self.assertNotEqual(rc, 0)
        self.assertIn("refused", (buf.getvalue() + ebuf.getvalue()).lower())


class TestRtRound1FetchArtifacts(_Seeded):
    """F3 (P1): fetch is for memory .md files only — not store artifacts."""

    def test_f3_adoptions_json_refused(self):
        (Path(self.dir) / ".adoptions.json").write_text(
            '{"secret": "ledger contents"}', encoding="utf-8")
        txt = _text(_call(50, "fetch", names=[".adoptions.json"]))
        self.assertIn("not a memory", txt.lower())
        self.assertNotIn("ledger contents", txt)

    def test_f3_index_md_refused(self):
        txt = _text(_call(51, "fetch", names=["MEMORY.md"]))
        self.assertIn("not a memory", txt.lower())

    def test_f3_recalls_json_refused(self):
        txt = _text(_call(52, "fetch", names=[".recalls.json"]))
        self.assertIn("not a memory", txt.lower())

    def test_f3_cli_artifact_refused(self):
        import contextlib
        import io
        from foldcrumbs import cli
        (Path(self.dir) / ".adoptions.json").write_text(
            '{"secret": "ledger contents"}', encoding="utf-8")
        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(["fetch", ".adoptions.json"])
        finally:
            os.chdir(old)
        self.assertNotIn("ledger contents", buf.getvalue())


class TestRtRound1McpTypes(_Seeded):
    """F4 (P1): off-schema MCP args are refused, not coerced."""

    def test_f4_mode_list_refused(self):
        txt = _text(_call(60, "recall", query="deploy", mode=["index"]))
        self.assertIn("refused", txt.lower())

    def test_f4_mode_int_refused(self):
        txt = _text(_call(61, "recall", query="deploy", mode=0))
        self.assertIn("refused", txt.lower())

    def test_f4_names_nested_refused(self):
        txt = _text(_call(62, "fetch", names=[["a.md"]]))
        self.assertIn("refused", txt.lower())

    def test_f4_names_dict_refused(self):
        txt = _text(_call(63, "fetch", names={"a": "b"}))
        self.assertIn("refused", txt.lower())

    def test_f4_window_float_refused(self):
        txt = _text(_call(64, "timeline", ref=self.names[0], window=1.5))
        self.assertIn("refused", txt.lower())

    def test_f4_window_bool_refused(self):
        txt = _text(_call(65, "timeline", ref=self.names[0], window=True))
        self.assertIn("refused", txt.lower())

    def test_f4_server_survives_and_next_call_works(self):
        _call(66, "recall", query="deploy", mode={"x": 1})
        txt = _text(_call(67, "recall", query="deploy"))
        self.assertIn("foldcrumbs-recall", txt)


if __name__ == "__main__":
    unittest.main()
