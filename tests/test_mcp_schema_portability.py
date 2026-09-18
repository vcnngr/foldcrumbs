"""MCP schema portability + graph_path source/target rename.

Issue (2026-09-17, found integrating foldcrumbs into Hermes profiles):
`graph_path` declared `"from"` as a parameter name. `from` is a Python
reserved word — an MCP host that builds a runtime function per tool
(`inspect.Parameter(name, ...)`) raises ValueError while assembling its
tool list, so the BRIDGE dies: the host agent loses ALL tools, and
foldcrumbs looks healthy from its side. A tool that kills its host is a
portability defect, not a style preference.

Fix: schema declares `source`/`target`; the historical `from`/`to` stay
accepted as undeclared input aliases (existing clients keep working).
Guard: EVERY tool schema must use portable parameter names, so this can
never regress silently.
"""
import unittest
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
# Before foldcrumbs: config resolves STATE_DIR at import time, and this
# module reloads config per test — without the sandbox first, the reload
# would pin the developer's real ~/.foldcrumbs into the shared process
# (and break TestMcpSandbox in test_mcp.py when the suite runs together).
from _sandbox import SANDBOX, is_inside  # noqa: E402

from foldcrumbs import mcp_server
from foldcrumbs import relations
from foldcrumbs import store
from foldcrumbs.schema import MemoryRecord


class TestSchemaPortability(unittest.TestCase):
    """The regression guard from the issue: no tool may declare a
    parameter name that cannot become an identifier in code-generating
    hosts."""

    def test_all_tool_params_are_portable_identifiers(self):
        # Oracle = the exact call that killed the host (inspect.Parameter),
        # not a hand-maintained word list: soft keywords (`type`, `match`)
        # ARE valid identifiers and must stay usable; only hard keywords
        # (`from`, `class`, ...) break code-generating hosts.
        import inspect
        offenders = []
        for tool in mcp_server.TOOLS:
            schema = tool.get("inputSchema") or {}
            names = set(schema.get("properties") or {})
            names |= set(schema.get("required") or [])
            for name in sorted(names):
                try:
                    inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY)
                except ValueError:
                    offenders.append(f"{tool['name']}: {name!r}")
        self.assertEqual(offenders, [],
                         "non-portable MCP parameter names — a host that "
                         "generates code from the schema cannot load the "
                         f"server: {offenders}")

    def test_graph_path_declares_source_target(self):
        tool = next(t for t in mcp_server.TOOLS
                    if t["name"] == "graph_path")
        props = tool["inputSchema"]["properties"]
        self.assertIn("source", props)
        self.assertIn("target", props)
        self.assertEqual(tool["inputSchema"]["required"], ["source", "target"])
        # the historical names must NOT be declared (that's the bug)
        self.assertNotIn("from", props)
        self.assertNotIn("to", props)


class _GraphPathBase(unittest.TestCase):
    def setUp(self):
        self._old = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX, prefix="gp_")
        self.dir = tempfile.mkdtemp(dir=self._tmp.name, prefix="src_")
        state = tempfile.mkdtemp(dir=self._tmp.name, prefix="state_")
        os.environ["FOLDCRUMBS_DIR"] = self.dir
        os.environ["FOLDCRUMBS_STATE_DIR"] = state
        assert is_inside(self.dir) and is_inside(state)
        from foldcrumbs import config
        import importlib
        importlib.reload(config)
        # seed: A caused_by B
        a = MemoryRecord(title="Release slipped",
                         content="The release slipped by a week.",
                         type="event")
        b = MemoryRecord(title="Supplier delay",
                         content="Supplier delay pushed everything.",
                         type="event")
        store.write_memory(a)
        store.write_memory(b)
        ok = relations.add_relation(
            a.id, "caused_by", {"k": "m", "id": b.id},
            evidence="supplier delay pushed the release",
            prov="manual")
        self.assertTrue(ok, "fixture relation was not attached")
        self.a, self.b = a, b

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        from foldcrumbs import config
        import importlib
        importlib.reload(config)
        self._tmp.cleanup()

    def _call(self, **arguments):
        r = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "graph_path", "arguments": arguments}})
        return r["result"]["content"][0]["text"], r["result"].get("isError")


class TestGraphPathNames(_GraphPathBase):
    def test_new_names_work(self):
        text, err = self._call(source="Release slipped",
                               target="Supplier delay")
        self.assertFalse(err)
        self.assertIn("FOUND", text)
        self.assertIn("caused_by", text)

    def test_historical_aliases_still_work(self):
        # backward compat: existing clients sending from/to keep working
        text, err = self._call(**{"from": "Release slipped",
                                  "to": "Supplier delay"})
        self.assertFalse(err)
        self.assertIn("FOUND", text)

    def test_new_names_win_over_aliases(self):
        # both present: declared names take precedence (deterministic,
        # never an ambiguous merge). RT P1 (card t_ac2538c3): the aliases
        # must be UNRESOLVABLE — with a reversible path and real refs on
        # both sides, FOUND alone could not tell precedence from its
        # inverse (mutant reproduced: from/to-privileging wrapper passed).
        text, err = self._call(source="Release slipped",
                               target="Supplier delay",
                               **{"from": "Nonexistent memory zzz",
                                  "to": "Another ghost yyy"})
        self.assertFalse(err)
        self.assertIn("FOUND", text,
                      "declared names must win over the aliases")

    def test_missing_target_refused_not_crash(self):
        text, _err = self._call(source="Release slipped")
        self.assertIn("refused", text.lower())

    def test_missing_both_refused(self):
        text, _err = self._call()
        self.assertIn("refused", text.lower())


if __name__ == "__main__":
    unittest.main()
