"""Tests for opt-in local embedding-endpoint discovery + the
understand→work→update AGENTS.md loop (0.12.0, via-B design).

Contract under test:
* discovery is OPT-IN (FOLDCRUMBS_EMBEDDING_AUTO=1) — off by default:
  a machine that never asked gets exactly today's behaviour;
* loopback-only: candidates are 127.0.0.1/localhost URLs, never remote;
* precedence: explicit env endpoint > state-file override > discovered;
* lazy + once-per-process: probing happens at first embed() use, not at
  import; the winner is cached in the state dir and reused;
* honest failure: nothing answers → embed() returns None → the existing
  lexical fallback engages (gate 2 of embeddings.py, unchanged);
* the installed AGENTS.md block carries the understand→work→update loop
  and install stays idempotent.
"""

import contextlib
import importlib
import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import config, embeddings, install  # noqa: E402


class _EmbedHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible /v1/embeddings that answers with a fixed
    vector, and records that it was probed."""

    hits = 0

    def do_POST(self):  # noqa: N802
        type(self).hits += 1
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        n = len(body.get("input") or ["x"])
        out = {"data": [{"index": i, "embedding": [0.0, 1.0]} for i in range(n)],
               "model": body.get("model", "test")}
        payload = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002 — silence
        pass


@contextlib.contextmanager
def _fake_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedHandler)
    t = __import__("threading").Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


_DISCOVERY_VARS = ("FOLDCRUMBS_EMBEDDING_AUTO", "FOLDCRUMBS_EMBEDDING_ENDPOINT",
                   "FOLDCRUMBS_EMBEDDING_MODEL", "FOLDCRUMBS_SEMANTIC",
                   "FOLDCRUMBS_LLM_ENDPOINT")


class _EnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _DISCOVERY_VARS}
        for k in _DISCOVERY_VARS:
            os.environ.pop(k, None)
        # config reads env at import time: reload so this test's env is law
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()


class TestDiscoveryOptIn(_EnvCase):
    def _set(self, **env):
        """Set env vars AND reload config/embeddings so the new env is law."""
        for k, v in env.items():
            os.environ[k] = v
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()

    def test_off_by_default_no_probe(self):
        # AUTO unset: discover_local_endpoint() never probes anything.
        probed = []
        result = embeddings.discover_local_endpoint(
            probe=lambda base: probed.append(base) or None)
        self.assertIsNone(result)
        self.assertEqual(probed, [], "off by default must not probe")

    def test_auto_on_probes_loopback_candidates_only(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1")
        probed = []
        embeddings.discover_local_endpoint(
            probe=lambda base: probed.append(base) or None)
        self.assertTrue(probed, "AUTO=1 must probe candidates")
        for base in probed:
            self.assertTrue(
                base.startswith("http://127.0.0.1") or
                base.startswith("http://localhost"),
                f"discovery must stay loopback, got {base}")

    def test_first_responder_wins_and_is_cached(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1")
        with _fake_server() as base:
            candidates = [base + "/v1"]
            found = embeddings.discover_local_endpoint(candidates=candidates)
            self.assertEqual(found, base + "/v1")
            # cached in state dir: second call with dead candidates still wins
            found2 = embeddings.discover_local_endpoint(
                candidates=["http://127.0.0.1:1/v1"])
            self.assertEqual(found2, base + "/v1")

    def test_explicit_endpoint_wins_over_discovery(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1",
                  FOLDCRUMBS_EMBEDDING_ENDPOINT="http://example.invalid")
        probed = []
        # explicit endpoint: discovery is a no-op and never probes
        self.assertIsNone(embeddings.discover_local_endpoint(
            probe=lambda b: probed.append(b) or None))
        self.assertEqual(probed, [], "explicit endpoint must disable probing")
        # _post goes to the user's endpoint: no answer → None (gate 2 intact)
        self.assertIsNone(embeddings.embed(["hello"]))
        self.assertTrue(
            embeddings._endpoint().startswith("http://example.invalid"))

    def test_nothing_answers_returns_none_lexical_fallback(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1", FOLDCRUMBS_SEMANTIC="1",
                  FOLDCRUMBS_EMBEDDING_MODEL="test-model")
        found = embeddings.discover_local_endpoint(
            candidates=["http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1"])
        self.assertIsNone(found)
        got = embeddings.embed(["anything"])
        self.assertIsNone(got)  # gate 2: lexical fallback, unchanged

    def test_end_to_end_discovery_serves_embed(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1", FOLDCRUMBS_SEMANTIC="1",
                  FOLDCRUMBS_EMBEDDING_MODEL="test-model")
        with _fake_server() as base:
            got = embeddings.embed(["query text"],
                                   candidates=[base + "/v1"])
        self.assertIsNotNone(got)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], [0.0, 1.0])


class TestAgentsMdLoop(unittest.TestCase):
    def test_block_contains_understand_work_update(self):
        block = install.AGENTS_MD_BLOCK
        for word in ("understand", "work", "update"):
            self.assertIn(word, block)
        # the loop must stay agent-agnostic (MCP tools or CLI fallback)
        self.assertIn("foldcrumbs recall", block)

    def test_block_still_idempotent_marker(self):
        # append_agents_md guards on the marker — unchanged behaviour
        self.assertIn("Memory (foldcrumbs)", install.AGENTS_MD_BLOCK)

    def test_append_writes_loop_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "AGENTS.md"
            install.append_agents_md(p)
            install.append_agents_md(p)  # second call must not duplicate
            text = p.read_text(encoding="utf-8")
            self.assertEqual(text.count("Memory (foldcrumbs)"), 1)
            self.assertEqual(text.count("understand → work → update"), 1)


if __name__ == "__main__":
    unittest.main()
