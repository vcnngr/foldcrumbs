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
        # readable prefix preserved, digest appended (r2: identity is the digest)
        seg = bridge.scope_cwd("acme").rsplit("/", 1)[-1]
        self.assertTrue(seg.startswith("acme--"), seg)

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


class TestRT83R2ScopeCollisions(BridgeBase):
    """RT #83 P0-1 PoCs, pinned: distinct raw ids must never share a store."""

    def test_slash_vs_underscore_companies_do_not_share_store(self):
        bridge.cmd_ingest({"company": "a/b", "text": "tenant a-slash-b secret"})
        # the colliding id: sanitizes to the same prefix, different digest
        got = bridge.cmd_query({"company": "a_b", "query": "tenant"})
        self.assertEqual(got["count"], 0,
                         "a_b must NOT read a/b's store (cross-tenant leak)")
        # and the real tenant still sees its own
        own = bridge.cmd_query({"company": "a/b", "query": "tenant"})
        self.assertEqual(own["count"], 1)

    def test_dots_vs_underscore_do_not_share_store(self):
        bridge.cmd_ingest({"company": "..", "text": "dotdot tenant secret"})
        got = bridge.cmd_query({"company": "_", "query": "dotdot"})
        self.assertEqual(got["count"], 0)

    def test_agent_hyphen_vs_agent_level_do_not_share_store(self):
        # company "a-agent-b" vs company "a" + agent "b": the r1 encoding
        # produced distinct PATHS but memory_dir flattened both onto one
        # store (the reviewer's memory_dir_equal=True PoC).
        bridge.cmd_ingest({"company": "a-agent-b", "text": "flat tenant"})
        got = bridge.cmd_query({"company": "a", "agent": "b",
                                "query": "flat"})
        self.assertEqual(got["count"], 0,
                         "company a+agent b must not read company a-agent-b")

    def test_project_hyphen_vs_project_level_do_not_share_store(self):
        bridge.cmd_ingest({"company": "a-project-b", "text": "proj flat"})
        got = bridge.cmd_query({"company": "a", "project": "b",
                                "query": "proj flat"})
        self.assertEqual(got["count"], 0)

    def test_scope_segment_is_length_bounded(self):
        # RT r2 P1-2: a 300-char id must not blow the 255-byte filename
        # limit downstream. Prefix is capped, digest fixed → segment bounded.
        seg = bridge._scope_seg("x" * 300)
        self.assertLessEqual(len(seg), 64,
                             f"segment not bounded: {len(seg)} chars")
        # and it actually works end-to-end (no Errno 63)
        long_co = "y" * 300
        bridge.cmd_ingest({"company": long_co, "text": "long id works"})
        got = bridge.cmd_query({"company": long_co, "query": "long id"})
        self.assertEqual(got["count"], 1)

    def test_digest_is_128_bit_not_64(self):
        # RT r2 P1-1: the isolation digest must be >=128 bits, not a 64-bit
        # truncation claimed as "injective".
        seg = bridge._scope_seg("acme")
        digest = seg.rsplit("--", 1)[-1]
        self.assertEqual(len(digest), 32, "digest must be 32 hex = 128 bits")

    def test_case_and_unicode_normalization_are_distinct_stores(self):
        # RT r2 P1-3: raw-id policy is case- and NFC/NFD-sensitive — distinct
        # raw ids must be distinct stores (documented, now pinned).
        nfc = "\u00e9"          # é as one codepoint
        nfd = "e\u0301"         # é as e + combining acute
        self.assertNotEqual(bridge.scope_cwd("ACME"), bridge.scope_cwd("acme"))
        self.assertNotEqual(bridge.scope_cwd(nfc), bridge.scope_cwd(nfd))
        # and they don't cross-read
        bridge.cmd_ingest({"company": nfc, "text": "nfc tenant secret"})
        got = bridge.cmd_query({"company": nfd, "query": "nfc tenant"})
        self.assertEqual(got["count"], 0)

    def test_collision_pinned_at_memory_dir_level(self):
        # the ultimate invariant: distinct scope triples → distinct PHYSICAL
        # store dirs (not just distinct synthetic cwds — memory_dir's own
        # "/"→"-" flattening is what bit r1).
        from foldcrumbs import config as cfg
        triples = [("a/b", "", ""), ("a_b", "", ""), ("a-agent-b", "", ""),
                   ("a", "b", ""), ("a", "", "b"), ("a-project-b", "", ""),
                   ("..", "", ""), ("_", "", ""), ("ACME", "", ""),
                   ("acme", "", "")]
        dirs = {str(cfg.memory_dir(bridge.scope_cwd(*t))) for t in triples}
        # The digest makes even "ACME" vs "acme" distinct stores (raw ids
        # differ → digests differ), and digests differ in the filename itself,
        # so a case-insensitive FS cannot merge them either. Policy: company
        # ids are case-SENSITIVE tenants. All 10 triples → 10 stores.
        self.assertEqual(len(dirs), 10,
                         f"scope triples collapsed onto one store: {sorted(dirs)}")


class TestRT83R2FailSoft(BridgeBase):
    """RT #83 P0-2 PoCs, pinned: handled errors are JSON ok:false, never
    tracebacks — via dispatch() AND via the CLI (main())."""

    HOSTILE = [
        # (op, request) — every case the reviewer crashed r1 with, plus more
        ("query", "not a dict"),
        ("ingest", {"company": 42, "text": "x"}),
        ("ingest", {"company": "c", "text": 42}),
        ("get", {"company": "c", "handle": 42}),
        ("query", {"company": "c", "query": {"nested": "dict"}}),
        ("query", {"company": "c", "query": "x", "limit": object()}),
        ("query", {"company": "c", "query": "x", "limit": -1}),
        ("query", {"company": "c", "query": "x", "limit": 10**9}),
        ("ingest", {"company": "c", "text": "x", "confidence": float("nan")}),
        ("ingest", {"company": "c", "text": "x", "confidence": "NaN"}),
        ("ingest", {"company": "c", "text": "x", "confidence": 2.5}),
        ("ingest", {"company": "c", "text": "x", "tags": 7}),
        ("browse", {"company": "c", "include_inactive": "yes"}),
        ("forget", {"company": "c", "handle": "h", "hard": "yes"}),
        ("ingest", {"company": "c", "text": "x", "issue": 42}),
        ("query", {}),                      # missing company
        ("get", {"company": "c"}),          # missing handle
        ("nope", {"company": "c"}),         # unknown op
    ]

    def test_dispatch_never_raises_on_hostile_requests(self):
        for op, req in self.HOSTILE:
            with self.subTest(op=op, req=repr(req)[:60]):
                out = bridge.dispatch(op, req)
                self.assertIsInstance(out, dict)
                self.assertFalse(out["ok"], f"{op} {req!r} should be refused")
                self.assertIn("error", out)

    def test_cli_never_tracebacks_on_hostile_requests(self):
        # Contract split (documented in bridge main()): rc1 = op refused with
        # JSON ok:false; rc2 = malformed ENVELOPE (non-object JSON payload,
        # or unknown op — main() validates by hand so even that answers as
        # JSON, never an argparse SystemExit). No Python traceback anywhere.
        import contextlib
        import io as _io
        import json as _json
        for op, req in self.HOSTILE:
            with self.subTest(op=op, req=repr(req)[:60]):
                payload = _json.dumps(req, default=repr) \
                    if isinstance(req, dict) else '"str"'
                # r3 contract (RT r2 P0): envelope = malformed PAYLOAD only
                # (non-object JSON). An unknown op is an OPERATION refusal
                # → rc1, not envelope. r2 wrongly lumped them together —
                # that masking is what the reviewer caught.
                envelope = not isinstance(req, dict)
                buf_out, buf_err = _io.StringIO(), _io.StringIO()
                with contextlib.redirect_stdout(buf_out), \
                        contextlib.redirect_stderr(buf_err):
                    rc = bridge.main(["call", op, payload])
                expected = 2 if envelope else 1
                self.assertEqual(rc, expected, f"{op} rc mismatch")
                self.assertNotIn("Traceback", buf_err.getvalue(),
                                 "no traceback may reach stderr")
                if not envelope:
                    body = _json.loads(buf_out.getvalue())
                    self.assertFalse(body["ok"])
                    self.assertIn("error", body)

    def test_cli_malformed_argv_is_json_rc2_never_systemexit(self):
        # RT r2 P0 PoCs: main([]), main(["call"]), main(["call","query"]),
        # main(["bogus"]) raised SystemExit(2) with prose stderr. Every
        # invocation shape must answer JSON on stdout with rc2.
        import contextlib
        import io as _io
        import json as _json
        for argv in ([], ["call"], ["call", "query"], ["bogus"],
                     ["capabilities", "extra"]):
            with self.subTest(argv=argv):
                buf_out, buf_err = _io.StringIO(), _io.StringIO()
                with contextlib.redirect_stdout(buf_out), \
                        contextlib.redirect_stderr(buf_err):
                    rc = bridge.main(argv)       # must NOT raise SystemExit
                self.assertEqual(rc, 2, f"argv {argv} must be rc2")
                body = _json.loads(buf_out.getvalue())
                self.assertFalse(body["ok"])
                self.assertIn("error", body)

    def test_cli_unknown_op_is_rc1_json(self):
        # RT r2 P0: `call nope {}` has a VALID envelope → operation refusal
        # rc1 (r1 wrongly returned rc2), always JSON.
        import contextlib
        import io as _io
        import json as _json
        buf_out, buf_err = _io.StringIO(), _io.StringIO()
        with contextlib.redirect_stdout(buf_out), \
                contextlib.redirect_stderr(buf_err):
            rc = bridge.main(["call", "nope", "{}"])
        self.assertEqual(rc, 1)
        self.assertFalse(_json.loads(buf_out.getvalue())["ok"])

    def test_valid_requests_still_work_after_hardening(self):
        # hardening must not break the happy path (guards against over-tight)
        ing = bridge.cmd_ingest({"company": "acme", "text": "still works",
                                 "confidence": 1.0, "tags": ["t"],
                                 "issue": "PAP-1"})
        self.assertTrue(ing["ok"])
        q = bridge.cmd_query({"company": "acme", "query": "still works",
                              "limit": 200})
        self.assertEqual(q["count"], 1)
        b = bridge.cmd_browse({"company": "acme", "include_inactive": True})
        self.assertEqual(b["count"], 1)


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
        # r2 contract: cmd_* validate centrally (raise _BadRequest); the
        # JSON ok:false surface is dispatch(). Test both levels.
        with self.assertRaises(ValueError):
            bridge.cmd_ingest({"company": "acme", "text": ""})
        out = bridge.dispatch("ingest", {"company": "acme", "text": ""})
        self.assertFalse(out["ok"])
        self.assertIn("text", out["error"])

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

    def test_cli_missing_company_is_rc1_json_error(self):
        # r2 contract: rc2 = malformed ENVELOPE only. A valid JSON object
        # missing "company" is an op-level refusal → rc1 + JSON ok:false
        # (was rc2 before validation was centralized).
        rc, out = self._run(["call", "query", json.dumps({"query": "x"})])
        self.assertEqual(rc, 1)
        body = json.loads(out)
        self.assertFalse(body["ok"])
        self.assertIn("company", body["error"])


class TestStdlibOnly(unittest.TestCase):
    def test_bridge_imports_no_third_party(self):
        # the constitution: the bridge must not drag in onnxruntime/numpy/etc.
        src = (ROOT / "integrations" / "paperclip" / "bridge.py").read_text()
        for banned in ("import numpy", "import onnxruntime", "import torch",
                       "import requests", "import httpx"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
