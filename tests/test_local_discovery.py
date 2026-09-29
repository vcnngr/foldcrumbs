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
                   "FOLDCRUMBS_LLM_ENDPOINT", "FOLDCRUMBS_LLM_API_KEY")


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

    def _set(self, **env):
        """Set env vars AND reload config/embeddings so the new env is law."""
        for k, v in env.items():
            os.environ[k] = v
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()


class TestDiscoveryOptIn(_EnvCase):
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
            embeddings._resolve()[0].startswith("http://example.invalid"))

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


class TestRT80P0Regressions(_EnvCase):
    """I quattro P0 della RT t_4b1afee2, ognuno con la sua regressione."""

    def test_p0_1_poisoned_remote_cache_never_probed(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1")
        cache = embeddings._discovery_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("https://attacker.example/v1", encoding="utf-8")
        probed = []
        found = embeddings.discover_local_endpoint(
            probe=lambda b: probed.append(b) or None,
            candidates=["http://127.0.0.1:1/v1"])
        self.assertIsNone(found)
        self.assertNotIn("https://attacker.example/v1", probed)
        for b in probed:
            self.assertTrue(embeddings._loopback_ok(b))
        self.assertFalse(cache.exists(), "invalid cache must be deleted")

    def test_p0_1_loopback_validator_cases(self):
        ok = embeddings._loopback_ok
        self.assertTrue(ok("http://127.0.0.1:11434/v1"))
        self.assertTrue(ok("http://localhost:8081/v1"))
        self.assertFalse(ok("https://127.0.0.1/v1"))       # https
        self.assertFalse(ok("http://127.0.0.1.evil.com/v1"))
        self.assertFalse(ok("http://user:***@127.0.0.1/v1"))
        self.assertTrue(ok("http://[::1]:8080/v1"))
        self.assertFalse(ok("file:///etc/passwd"))
        self.assertFalse(ok("http://192.168.1.10/v1"))     # LAN host
        self.assertFalse(ok("not a url"))

    def test_p0_2_failure_memoised_no_repeat_probes(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1")
        probed = []
        probe = lambda b: probed.append(b) or None  # noqa: E731
        embeddings.discover_local_endpoint(probe=probe)
        first = len(probed)
        self.assertGreater(first, 0)
        embeddings.discover_local_endpoint(probe=probe)
        embeddings.discover_local_endpoint(probe=probe)
        self.assertEqual(len(probed), first,
                         "failure must be memoised: no re-probe in-process")

    def test_p0_2_embed_post_counts(self):
        # warm cache → zero network; miss → exactly ONE embeddings POST
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1", FOLDCRUMBS_SEMANTIC="1",
                  FOLDCRUMBS_EMBEDDING_MODEL="test-model")
        _EmbedHandler.hits = 0
        with _fake_server() as base:
            got1 = embeddings.embed(["alpha", "beta"], candidates=[base + "/v1"])
            hits_after_first = _EmbedHandler.hits
            got2 = embeddings.embed(["alpha", "beta"], candidates=[base + "/v1"])
            hits_after_second = _EmbedHandler.hits
        self.assertIsNotNone(got1)
        # 1 probe + 1 batched embed per la prima chiamata (2 testi, 1 POST)
        self.assertEqual(hits_after_first, 2)
        # seconda chiamata tutta warm-cache: ZERO POST ulteriori
        self.assertEqual(hits_after_second, 2)
        self.assertEqual(got1, got2)

    def test_p0_3_historical_cache_key_unchanged_with_auto_off(self):
        # AUTO off + endpoint esplicito: la key deve essere byte-identica
        # a quella storica (basis = raw config endpoint, non normalizzato).
        # P1 r2: pin the PRODUCTION call shape _key(text) — no explicit
        # second argument, exactly like _embed_inner does.
        import hashlib
        self._set(FOLDCRUMBS_EMBEDDING_ENDPOINT="http://127.0.0.1:9999",
                  FOLDCRUMBS_EMBEDDING_MODEL="m", FOLDCRUMBS_SEMANTIC="1")
        text = "deploy window"
        historical = hashlib.sha256(
            f"http://127.0.0.1:9999\x00m\x00{text}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(embeddings._key(text), historical)


class _RedirectHandler(BaseHTTPRequestHandler):
    """302s every POST to a forbidden sink URL (set as class attr)."""

    sink = "http://127.0.0.1:1/v1"

    def do_POST(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", type(self).sink)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


class _SinkHandler(BaseHTTPRequestHandler):
    """Records any request that reaches the forbidden target."""

    hits = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_GET

    def log_message(self, format, *args):  # noqa: A002
        pass


class TestRedirectNeverFollowed(_EnvCase):
    """New P0 (RT t_01a931cf): a loopback endpoint must not be able to
    302 the request — and the Bearer key — to a host outside loopback.
    The reviewer's counterexample, as a regression."""

    def test_redirect_to_forbidden_host_is_refused_no_key_leak(self):
        self._set(FOLDCRUMBS_EMBEDDING_AUTO="1", FOLDCRUMBS_SEMANTIC="1",
                  FOLDCRUMBS_EMBEDDING_MODEL="m",
                  FOLDCRUMBS_LLM_API_KEY="***")
        threading_ = __import__("threading")
        sink_srv = ThreadingHTTPServer(("127.0.0.1", 0), _SinkHandler)
        red_srv = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        _SinkHandler.hits = []
        _RedirectHandler.sink = f"http://127.0.0.1:{sink_srv.server_address[1]}/v1"
        # NOTE: the sink is loopback in this test (a real remote host in CI
        # would be flaky); what matters is the redirect is NEVER followed:
        # zero hits on the sink, no Authorization header anywhere.
        for srv in (sink_srv, red_srv):
            threading_.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{red_srv.server_address[1]}/v1"
            # probe refuses the redirecting endpoint
            self.assertFalse(embeddings._probe_endpoint(base))
            # discovery does not accept it
            self.assertIsNone(embeddings.discover_local_endpoint(
                candidates=[base]))
            # direct embed POST does not follow it either → None, no leak
            self.assertIsNone(embeddings._post(["secret text"], base))
        finally:
            sink_srv.shutdown()
            sink_srv.server_close()
            red_srv.shutdown()
            red_srv.server_close()
        self.assertEqual(_SinkHandler.hits, [],
                         "redirect target must receive ZERO requests")


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
