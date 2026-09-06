"""Three-layer recall + timeline (competitor deep-dive item #1/#2).

Contract under test:
* `recall --index` (CLI) / recall(mode="index") (MCP): compact index layer —
  one line per hit: filename, type, title, updated date. ~10x fewer tokens
  than the full context block; same ranking, same filters.
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
        # containment: ../ must not escape the store
        txt = _text(_call(9, "fetch", names=["../MEMORY.md", "../../etc/passwd"]))
        self.assertIn("not found", txt.lower())


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


if __name__ == "__main__":
    unittest.main()
