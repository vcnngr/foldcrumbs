"""Tests for the Paperclip memory-provider bridge (stdlib, no network).

These run against a real temporary store under a scoped cwd — the bridge's
whole point is scope isolation, so the tests assert isolation, not just
round-trips. Each test drives the SAME functions a Paperclip adapter shells
into, and checks their portable-core contract (doc #1155).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "paperclip"))

import bridge  # noqa: E402


class BridgeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_root = os.environ.get(bridge._ROOT_ENV)
        # CRITICAL (RT lesson from PR #82): the physical store resolves under
        # claude_config_dir(), NOT under the paperclip root — so setting only
        # _ROOT_ENV would write test scopes into the user's REAL ~/.claude.
        # Redirect CLAUDE_CONFIG_DIR to the tmpdir too: every store then lands
        # inside self._tmp and the real store is never touched.
        self._saved_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self._tmp.name
        os.environ[bridge._ROOT_ENV] = self._tmp.name

    def tearDown(self):
        for k, v in ((bridge._ROOT_ENV, self._saved_root),
                     ("CLAUDE_CONFIG_DIR", self._saved_cfg)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestScoping(BridgeBase):
    def test_scope_maps_to_isolated_dirs(self):
        a = bridge.scope_cwd("acme", "cto", "web")
        b = bridge.scope_cwd("acme", "cto", "api")
        c = bridge.scope_cwd("globex", "cto", "web")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        # company alone is a valid, narrower scope
        self.assertTrue(bridge.scope_cwd("acme").endswith("acme"))

    def test_company_required(self):
        with self.assertRaises(ValueError):
            bridge.scope_cwd("")

    def test_scope_id_cannot_traverse_root(self):
        # a malicious scope id with slashes/dots must stay inside the root:
        # separators are flattened to '_', so no PATH SEGMENT can be "..".
        # (Asserting on segments, not the raw substring: '_.._etc' is one
        # harmless filename, not a traversal.)
        for evil_scope in ("../../etc/passwd", "..", "a/../..", "/etc", "..."):
            evil = bridge.scope_cwd(evil_scope, "a/b", "..")
            self.assertTrue(Path(evil).is_relative_to(bridge._root()),
                            f"escaped root with {evil_scope!r}: {evil}")
            rel_segments = Path(evil).relative_to(bridge._root()).parts
            self.assertNotIn("..", rel_segments,
                             f"traversal segment with {evil_scope!r}")

    def test_stores_are_actually_isolated(self):
        # a memory in one company scope is invisible in another
        bridge.cmd_ingest({"company": "acme", "text": "acme secret fact"})
        got = bridge.cmd_query({"company": "globex", "query": "acme secret"})
        self.assertEqual(got["count"], 0)
        got2 = bridge.cmd_query({"company": "acme", "query": "acme secret"})
        self.assertEqual(got2["count"], 1)

    def test_store_lands_in_tmpconfig_not_real_claude_dir(self):
        # RT lesson (PR #82): the physical store must NOT be the user's real
        # ~/.claude. Prove every scope store resolves inside our tmpdir.
        cwd = bridge.scope_cwd("acme", "cto")
        bridge.cmd_ingest({"company": "acme", "agent": "cto", "text": "x"})
        from foldcrumbs import config as cfg
        real_dir = cfg.memory_dir(cwd)
        self.assertTrue(
            Path(real_dir).is_relative_to(Path(self._tmp.name)),
            f"store escaped tmpdir into real ~/.claude: {real_dir}")

    def test_query_is_not_federated_cross_tenant_leak(self):
        # RT: store.search defaults to federated=True. A Paperclip company
        # scope must NOT surface memories from unrelated federated roots —
        # that would break "complete data isolation". Prove query is scoped:
        # two companies, same word; each sees only its own.
        bridge.cmd_ingest({"company": "acme", "text": "needle in acme only"})
        bridge.cmd_ingest({"company": "globex", "text": "needle in globex"})
        # acme must see only its own needle, never globex's
        res = bridge.cmd_query({"company": "acme", "query": "needle"})
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["results"][0]["paperclip"]["company"], "acme")
        # and the underlying search call is federated=False (no leak path)
        import inspect
        self.assertIn("federated=False",
                      inspect.getsource(bridge.cmd_query))

    def test_scope_key_must_be_consistent_across_ops(self):
        # The documented behavior: the scope is the exact company/agent/
        # project triple. Ingesting under project=web is a DIFFERENT store
        # than querying with no project — proved, not just documented.
        bridge.cmd_ingest({"company": "acme", "agent": "cto", "project": "web",
                           "text": "deploy tuesdays"})
        with_project = bridge.cmd_query({"company": "acme", "agent": "cto",
                                         "project": "web", "query": "deploy"})
        without_project = bridge.cmd_query({"company": "acme", "agent": "cto",
                                            "query": "deploy"})
        self.assertEqual(with_project["count"], 1)
        self.assertEqual(without_project["count"], 0,
                         "a different scope key must not see the web store")


class TestPortableCore(BridgeBase):
    def test_ingest_query_get_roundtrip(self):
        ing = bridge.cmd_ingest({
            "company": "acme", "agent": "cto",
            "text": "We deploy Tuesdays 10-12 UTC.",
            "type": "decision", "title": "Deploy window",
            "issue": "PAP-530", "run": "run-42",
        })
        self.assertTrue(ing["ok"])
        self.assertIn(ing["action"], ("created", "validated"))
        handle = ing["handle"]

        q = bridge.cmd_query({"company": "acme", "agent": "cto",
                              "query": "deploy window"})
        self.assertEqual(q["count"], 1)
        mem = q["results"][0]
        # provenance back to Paperclip entities survived
        self.assertEqual(mem["paperclip"].get("issue"), "PAP-530")
        self.assertEqual(mem["paperclip"].get("run"), "run-42")
        self.assertEqual(mem["paperclip"].get("company"), "acme")

        g = bridge.cmd_get({"company": "acme", "agent": "cto",
                            "handle": handle})
        self.assertTrue(g["ok"])
        self.assertEqual(g["memory"]["title"], "Deploy window")

    def test_ingest_requires_text(self):
        r = bridge.cmd_ingest({"company": "acme", "text": ""})
        self.assertFalse(r["ok"])

    def test_query_missing_scope_is_error_via_cli(self):
        # dispatch-level: query with no company raises through scope_cwd
        with self.assertRaises(ValueError):
            bridge.cmd_query({"query": "x"})

    def test_browse_lists_scope(self):
        bridge.cmd_ingest({"company": "acme", "text": "fact one"})
        bridge.cmd_ingest({"company": "acme", "text": "fact two"})
        b = bridge.cmd_browse({"company": "acme"})
        self.assertTrue(b["ok"])
        self.assertEqual(b["count"], 2)

    def test_forget_soft_then_hard(self):
        ing = bridge.cmd_ingest({"company": "acme", "text": "to be forgotten"})
        h = ing["handle"]
        soft = bridge.cmd_forget({"company": "acme", "handle": h})
        self.assertTrue(soft["ok"])
        self.assertEqual(soft["action"], "deleted")   # soft by default
        # honest get-by-handle: still fetchable, but flagged NOT served, and
        # gone from the served browse view (visibility over silent deletion)
        g = bridge.cmd_get({"company": "acme", "handle": h})
        self.assertTrue(g["ok"])
        self.assertFalse(g["served"])
        self.assertEqual(g["memory"]["status"], "deleted")
        self.assertEqual(bridge.cmd_browse({"company": "acme"})["count"], 0)
        self.assertEqual(
            bridge.cmd_browse({"company": "acme",
                               "include_inactive": True})["count"], 1)
        hard = bridge.cmd_forget({"company": "acme", "handle": h, "hard": True})
        self.assertEqual(hard["action"], "removed")
        # after hard delete the file is gone: get-by-handle is now a miss
        self.assertFalse(bridge.cmd_get({"company": "acme", "handle": h})["ok"])

    def test_forget_unknown_handle(self):
        r = bridge.cmd_forget({"company": "acme", "handle": "nope.md"})
        self.assertFalse(r["ok"])

    def test_usage_reports_structure_not_fake_cost(self):
        bridge.cmd_ingest({"company": "acme", "text": "a", "type": "fact"})
        bridge.cmd_ingest({"company": "acme", "text": "b", "type": "decision"})
        u = bridge.cmd_usage({"company": "acme"})
        self.assertTrue(u["ok"])
        self.assertEqual(u["count"], 2)
        self.assertEqual(u["by_type"].get("fact"), 1)
        self.assertEqual(u["by_type"].get("decision"), 1)
        self.assertGreaterEqual(u["store_bytes"], 0)
        # must NOT invent a token cost it cannot measure
        self.assertIn("note", u)
        self.assertNotIn("tokens", u)
        self.assertNotIn("cost_usd", u)


class TestCapabilities(BridgeBase):
    def test_capabilities_advertise_core_ops(self):
        caps = bridge.CAPABILITIES
        for op in ("ingest", "query", "get", "browse", "forget", "usage"):
            self.assertIn(op, caps["core_ops"])
        self.assertTrue(caps["no_cloud"])
        self.assertTrue(caps["no_api_key"])
        self.assertIn("company", caps["scoping"])


class TestCLI(BridgeBase):
    def _run(self, argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bridge.main(argv)
        return rc, buf.getvalue()

    def test_cli_capabilities_is_valid_json(self):
        rc, out = self._run(["capabilities"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["provider"], "foldcrumbs")

    def test_cli_call_ingest_then_query(self):
        rc, out = self._run(["call", "ingest", json.dumps(
            {"company": "acme", "text": "cli ingested fact"})])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        rc2, out2 = self._run(["call", "query", json.dumps(
            {"company": "acme", "query": "cli ingested"})])
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(out2)["count"], 1)

    def test_cli_bad_json_is_rc2(self):
        rc, out = self._run(["call", "ingest", "{not json"])
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_cli_missing_company_is_rc2(self):
        rc, out = self._run(["call", "query", json.dumps({"query": "x"})])
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out)["ok"])


class TestStdlibOnly(unittest.TestCase):
    def test_bridge_imports_no_third_party(self):
        # the constitution: the bridge must not drag in onnxruntime/numpy/etc.
        src = (ROOT / "integrations" / "paperclip" / "bridge.py").read_text()
        for banned in ("import numpy", "import onnxruntime", "import torch",
                       "import requests", "import httpx"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
