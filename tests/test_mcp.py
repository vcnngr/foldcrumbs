"""MCP server tests: drive the JSON-RPC stdio protocol directly (no real client).

Tests the in-process handler (fast) plus a full subprocess round-trip over
stdin/stdout to prove the wire protocol works end-to-end.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: this module is routinely run on its own, and MCP recall
# is federated, so without it the registry would resolve to the developer's
# real ~/.foldcrumbs and read their actual stores.
from _sandbox import SANDBOX, is_inside  # noqa: E402

import foldcrumbs  # noqa: E402
from foldcrumbs import install, mcp_server  # noqa: E402


class TestMcpSandbox(unittest.TestCase):
    """This module is routinely run on its own; the sandbox must hold then."""

    def test_never_resolves_to_a_real_store(self):
        from foldcrumbs import config
        for path in (config.STATE_DIR, config.claude_config_dir()):
            self.assertTrue(is_inside(path),
                            f"{path} is outside the sandbox {SANDBOX}")


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="foldcrumbs_mcp_")
        os.environ["FOLDCRUMBS_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("FOLDCRUMBS_DIR", None)

    def test_initialize_echoes_protocol(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "foldcrumbs")
        # Server version must track the package, not a hardcoded literal.
        self.assertEqual(r["result"]["serverInfo"]["version"], foldcrumbs.__version__)

    def test_initialized_notification_no_response(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, {"remember", "recall", "answer", "forget"})

    def test_forget_by_filename(self):
        rem = mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                 "params": {"name": "remember", "arguments": {
                                     "content": "We deploy on Fridays.",
                                     "type": "fact", "title": "Deploy day"}}})
        # remember reports "... at <filename>"; forget takes that filename.
        fname = rem["result"]["content"][0]["text"].rsplit(" at ", 1)[1]
        fg = mcp_server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                "params": {"name": "forget",
                                           "arguments": {"name": fname}}})
        self.assertFalse(fg["result"]["isError"])
        self.assertIn("deleted", fg["result"]["content"][0]["text"])
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "deploy fridays"}}})
        self.assertIn("no matching", rec["result"]["content"][0]["text"])

    def test_forget_wrong_name_suggests_candidates(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                           "params": {"name": "remember", "arguments": {
                               "content": "We deploy on Fridays.",
                               "type": "fact", "title": "Deploy day"}}})
        fg = mcp_server.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                "params": {"name": "forget",
                                           "arguments": {"name": "deploy day"}}})
        text = fg["result"]["content"][0]["text"]
        self.assertIn("exact filename", text)
        self.assertIn("fact_deploy_day.md", text)

    def test_remember_then_recall(self):
        rem = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "remember", "arguments": {
                                     "content": "Recall is grep, no vector DB.",
                                     "type": "decision"}}})
        self.assertFalse(rem["result"]["isError"])
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "vector db"}}})
        text = rec["result"]["content"][0]["text"]
        self.assertIn("grep", text)

    # --- recall filters (type / tags): MCP-only agents like OpenCode used to
    # have no way to narrow a search, while the CLI had --type/--tag ---

    def _seed(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 30, "method": "tools/call",
                           "params": {"name": "remember", "arguments": {
                               "content": "We deploy on Fridays via the pipeline.",
                               "type": "fact", "title": "Deploy day",
                               "tags": ["ops", "deploy"]}}})
        mcp_server.handle({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                           "params": {"name": "remember", "arguments": {
                               "content": "Deploy reviews are mandatory before Friday.",
                               "type": "decision", "title": "Deploy review"}}})

    def test_recall_type_filter_as_string(self):
        self._seed()
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 32, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "deploy", "type": "decision"}}})
        text = rec["result"]["content"][0]["text"]
        self.assertIn("Deploy reviews", text)
        self.assertNotIn("We deploy on Fridays", text,
                         "a type filter returned memories of another type")

    def test_recall_type_filter_as_array(self):
        self._seed()
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 33, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "deploy", "type": ["decision"]}}})
        text = rec["result"]["content"][0]["text"]
        self.assertIn("Deploy reviews", text)
        self.assertNotIn("We deploy on Fridays", text)

    def test_recall_tags_filter(self):
        self._seed()
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 34, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "deploy", "tags": ["ops"]}}})
        text = rec["result"]["content"][0]["text"]
        self.assertIn("We deploy on Fridays", text)
        self.assertNotIn("Deploy reviews", text,
                         "a tags filter returned an untagged memory")

    def test_recall_without_filters_is_unchanged(self):
        self._seed()
        rec = mcp_server.handle({"jsonrpc": "2.0", "id": 35, "method": "tools/call",
                                 "params": {"name": "recall", "arguments": {
                                     "query": "deploy"}}})
        text = rec["result"]["content"][0]["text"]
        self.assertIn("We deploy on Fridays", text)
        self.assertIn("Deploy reviews", text)

    def test_recall_schema_declares_the_filters(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 36, "method": "tools/list"})
        recall = next(t for t in r["result"]["tools"] if t["name"] == "recall")
        props = recall["inputSchema"]["properties"]
        self.assertIn("type", props)
        self.assertIn("tags", props)
        self.assertEqual(recall["inputSchema"]["required"], ["query"])

    def test_unknown_tool_is_error(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_method(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 6, "method": "bogus/x"})
        self.assertEqual(r["error"]["code"], -32601)


class TestSubprocessRoundTrip(unittest.TestCase):
    def test_full_stdio_session(self):
        d = tempfile.mkdtemp(prefix="foldcrumbs_mcp_sp_")
        env = {**os.environ, "FOLDCRUMBS_DIR": d}
        proc = subprocess.Popen(
            [sys.executable, "-m", "foldcrumbs.mcp_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, cwd=str(REPO), env=env,
        )
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "remember",
                        "arguments": {"content": "Hooks must exit 0.", "type": "instruction"}}},
        ]
        out, _ = proc.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=30)
        responses = [json.loads(line) for line in out.splitlines() if line.strip()]
        by_id = {r.get("id"): r for r in responses}
        # initialize, tools/list, tools/call answered; notification got no response.
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "foldcrumbs")
        self.assertEqual({t["name"] for t in by_id[2]["result"]["tools"]},
                         {"remember", "recall", "answer", "forget"})
        self.assertFalse(by_id[3]["result"]["isError"])
        self.assertEqual(len(responses), 3)  # no response for the notification

    def test_staged_runtime_works_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix="foldcrumbs_mcp_runtime_") as d:
            runtime = Path(d) / "runtime"
            memory = Path(d) / "memory"
            cmd = install._mcp_command(runtime)
            proc = subprocess.run(
                cmd,
                input=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }) + "\n",
                capture_output=True,
                text=True,
                cwd="/",
                env={**os.environ, "FOLDCRUMBS_DIR": str(memory)},
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            response = json.loads(proc.stdout)
            self.assertEqual(response["result"]["serverInfo"]["name"], "foldcrumbs")


if __name__ == "__main__":
    unittest.main()
