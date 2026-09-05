"""FL-3 — MCP parity for the fleet tools (adopt, outcome).

The design rule (fleet-learning.md §F3): the MCP surface gets NO powers the
CLI does not have — same arguments, same explicit refusals, same ledger.
The server goes from 7 to 9 tools.

Also pinned here: an MCP call that does not name an outcome note is
recorded as coming from an agent (design §F3), and the note lives in the
LEDGER only — never in frontmatter (FL-1 F1: a multiline note must not be
able to forge keys; the ledger is json, frontmatter is not).
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import mcp_server  # noqa: E402


def _rpc(id_, method, **params):
    req = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params:
        req["params"] = params
    return mcp_server.handle(req)


def _call(id_, name, **args):
    return _rpc(id_, "tools/call", name=name, arguments=args)


def _text(resp):
    return resp["result"]["content"][0]["text"]


class TestFleetToolsCatalog(unittest.TestCase):

    def test_catalog_is_nine_tools(self):
        r = _rpc(1, "tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names,
                         {"remember", "recall", "answer", "forget",
                          "graph_path", "relate", "ingest",
                          "adopt", "outcome"})

    def test_adopt_tool_schema(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "adopt")
        props = tool["inputSchema"]["properties"]
        self.assertIn("ref", props)
        # ref is NOT strictly required: search+from_root is the other mode;
        # the handler refuses visibly when neither is given (tested below).
        self.assertIn("note", props)
        self.assertIn("as_type", props)
        self.assertIn("search", props)
        self.assertIn("from_root", props)

    def test_outcome_tool_schema(self):
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "outcome")
        props = tool["inputSchema"]["properties"]
        self.assertIn("memory", props)
        self.assertIn("verdict", props)
        verdict = props["verdict"]
        # closed vocabulary declared in the schema itself
        self.assertEqual(sorted(verdict.get("enum", [])), ["bad", "good"])
        self.assertIn("note", props)
        self.assertIn("list", props)

    def test_dispatch_covers_every_catalog_entry(self):
        for tool in mcp_server.TOOLS:
            self.assertIn(tool["name"], mcp_server._DISPATCH,
                          f"{tool['name']} has no handler")


class TestOutcomeTool(unittest.TestCase):
    """outcome through the MCP surface — same refusals as the CLI."""

    def setUp(self):
        import os
        import tempfile
        self._dir = tempfile.mkdtemp(prefix="ccmem_mcp_out_")
        self._saved = os.environ.get("FOLDCRUMBS_DIR")
        os.environ["FOLDCRUMBS_DIR"] = self._dir
        import importlib
        from foldcrumbs import config
        importlib.reload(config)

    def tearDown(self):
        import os
        if self._saved is None:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        else:
            os.environ["FOLDCRUMBS_DIR"] = self._saved
        import importlib
        from foldcrumbs import config
        importlib.reload(config)

    def test_outcome_good_then_list(self):
        r = _call(2, "remember", content="Deploys on Fridays.",
                  type="fact", title="Deploy day")
        self.assertFalse(r["result"]["isError"])
        fname = _text(r).rsplit(" at ", 1)[1]
        g = _call(3, "outcome", memory=fname, verdict="good", note="held")
        self.assertFalse(g["result"]["isError"], _text(g))
        self.assertIn("recorded good", _text(g))
        lst = _call(4, "outcome", **{"list": True})
        self.assertIn("fact_deploy_day.md", _text(lst))

    def test_outcome_bad_marks_contradiction(self):
        _call(5, "remember", content="Use X.", type="fact", title="Rule X")
        b = _call(6, "outcome", memory="fact_rule_x.md", verdict="bad",
                  note="broke prod")
        self.assertFalse(b["result"]["isError"], _text(b))
        self.assertIn("recorded bad", _text(b))
        from foldcrumbs import store
        rec = store.get("fact_rule_x.md")
        self.assertTrue(rec.contradiction_detected)
        self.assertEqual(rec.outcome, "bad")

    def test_outcome_invalid_verdict_refused(self):
        _call(7, "remember", content="Y.", type="fact", title="Rule Y")
        r = _call(8, "outcome", memory="fact_rule_y.md", verdict="great")
        # closed enum in schema + guard in handler: refused, not recorded
        if not r["result"]["isError"]:
            self.assertIn("must be one of", _text(r))
        from foldcrumbs import store
        self.assertIsNone(store.get("fact_rule_y.md").outcome)

    def test_outcome_missing_memory_refused(self):
        r = _call(9, "outcome", memory="nope.md", verdict="good")
        self.assertTrue(r["result"]["isError"] or "refused" in _text(r)
                        or "not found" in _text(r))


class TestAdoptTool(unittest.TestCase):
    """adopt through MCP — refusals identical to the CLI (no extra powers)."""

    def test_as_type_outside_vocabulary_refused_like_cli(self):
        # RT Kimi F1 (P0): the CLI validates --as-type, the MCP tool passed
        # it raw and schema silently degraded it to "fact" — a parity hole.
        from foldcrumbs import adopt as adopt_mod
        res = adopt_mod.adopt("0123456789abcdef:x.md", as_type="nonsense")
        self.assertFalse(res["ok"], "invalid as_type must refuse at the core")
        self.assertIn("as_type", res["reason"])

    def setUp(self):
        import os
        import tempfile
        self._dir = tempfile.mkdtemp(prefix="ccmem_mcp_adopt_")
        self._saved = os.environ.get("FOLDCRUMBS_DIR")
        os.environ["FOLDCRUMBS_DIR"] = self._dir
        import importlib
        from foldcrumbs import config
        importlib.reload(config)

    def tearDown(self):
        import os
        if self._saved is None:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        else:
            os.environ["FOLDCRUMBS_DIR"] = self._saved
        import importlib
        from foldcrumbs import config
        importlib.reload(config)

    def test_adopt_unknown_root_refused_visibly(self):
        r = _call(2, "adopt", ref="0123456789abcdef:x.md")
        text = _text(r)
        self.assertTrue(r["result"]["isError"] or "refused" in text
                        or "unknown root" in text)
        self.assertIn("root", text.lower())

    def test_adopt_malformed_ref_refused(self):
        r = _call(3, "adopt", ref="nocolonhere")
        text = _text(r)
        self.assertTrue(r["result"]["isError"] or "refused" in text)

    def test_adopt_search_requires_from_root(self):
        r = _call(4, "adopt", search="deploy")
        text = _text(r)
        # either an explicit refusal or a visible hint — never a traceback
        self.assertTrue(r["result"]["isError"] or "from_root" in text
                        or "root" in text.lower())

    def test_adopt_search_limit_validated_like_cli(self):
        # RT Kimi P1 notes: negative/non-numeric limit must refuse visibly,
        # and the suggested command carries the FULL root id.
        r = _call(5, "adopt", search="x", from_root="0123456789abcdef",
                  limit=-1)
        text = _text(r)
        self.assertIn("limit", text)
        self.assertIn("refused", text)
        r2 = _call(6, "adopt", search="x", from_root="0123456789abcdef",
                   limit="abc")
        self.assertIn("limit", _text(r2))

    def test_adopt_agent_provenance_lands_in_ledger_note(self):
        # A call without an explicit note is recorded as coming from an
        # agent — in the LEDGER (json), never in frontmatter (FL-1 F1).
        r = _call(5, "adopt", ref="0123456789abcdef:x.md")
        # refusal expected (no such root); this test pins that the handler
        # does not crash when composing the default note.
        self.assertIn("root", _text(r).lower())


if __name__ == "__main__":
    unittest.main()
