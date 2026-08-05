"""Tests for the optional semantic channel (stdlib unittest, no external deps).

The contract under test:
* off by default — a machine that never opted in makes zero embedding calls;
* never blocking — an absent or failing endpoint falls back to lexical silently;
* capped — no vector similarity can outrank a perfect word match;
* rescuing — paraphrases with zero word overlap can still enter recall;
* cached machine-locally — a warm store costs no second request.
"""

import contextlib
import importlib
import io
import json
import os
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Before foldcrumbs: this module is routinely run on its own, and semantic
# caching writes into STATE_DIR — without the sandbox it would be the
# developer's real ~/.foldcrumbs.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import config, embeddings, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


def _haystack(rec: MemoryRecord) -> str:
    """Exactly what store.search() embeds for a record."""
    return f"{rec.title}\n{rec.content}\n{' '.join(rec.tags)}".lower()


def _vec(cosine: float) -> list[float]:
    """A unit 2-vector making exactly ``cosine`` with (1, 0)."""
    import math
    c = max(-1.0, min(1.0, cosine))
    return [c, math.sqrt(max(0.0, 1.0 - c * c))]


class TestSemanticSwitchParsing(unittest.TestCase):
    """The switch is explicit: every off-ish spelling stays off."""

    _VARS = ("FOLDCRUMBS_SEMANTIC", "FOLDCRUMBS_EMBEDDING_ENDPOINT",
             "FOLDCRUMBS_EMBEDDING_MODEL", "FOLDCRUMBS_EMBEDDING_TIMEOUT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._VARS}
        for k in self._VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)

    def _semantic(self, value: str | None) -> bool:
        if value is None:
            os.environ.pop("FOLDCRUMBS_SEMANTIC", None)
        else:
            os.environ["FOLDCRUMBS_SEMANTIC"] = value
        importlib.reload(config)
        return config.SEMANTIC

    def test_unset_is_off(self):
        self.assertFalse(self._semantic(None))

    def test_off_spellings_stay_off(self):
        for value in ("", "0", "false", "no", "off"):
            self.assertFalse(self._semantic(value), f"{value!r} switched it on")

    def test_on_spellings_switch_on(self):
        for value in ("1", "true", "yes"):
            self.assertTrue(self._semantic(value), f"{value!r} stayed off")

    def test_endpoint_defaults_to_the_llm_endpoint(self):
        importlib.reload(config)
        self.assertEqual(config.EMBEDDING_ENDPOINT, config.LLM_ENDPOINT)
        # No separate model by default: the request falls back to LLM_MODEL.
        self.assertEqual(config.EMBEDDING_MODEL, "")
        self.assertEqual(embeddings._model(), config.LLM_MODEL)


class TestEmbeddingsModule(TmpStore):
    """Cache, gating and math — no network anywhere in this class."""

    def test_embed_refuses_without_the_switch_and_never_calls(self):
        calls = []
        real_post = embeddings._post
        embeddings._post = lambda texts: calls.append(texts) or [[1.0]]
        prev = config.SEMANTIC
        config.SEMANTIC = False
        try:
            self.assertIsNone(embeddings.embed(["hello"]))
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual(calls, [], "an uninterested machine must not call out")

    def test_embed_empty_input(self):
        self.assertEqual(embeddings.embed([]), [])

    def test_cache_hit_skips_the_network(self):
        posts = []
        real_post = embeddings._post

        def fake_post(texts):
            posts.append(list(texts))
            return [_vec(0.5) for _ in texts]

        prev = config.SEMANTIC
        config.SEMANTIC = True
        embeddings._post = fake_post
        try:
            first = embeddings.embed(["a", "b"])
            second = embeddings.embed(["a", "b", "a"])
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual(len(posts), 1, "the second read must be cache-only")
        self.assertEqual(first, [first[0], first[1]])
        self.assertEqual(second, [first[0], first[1], first[0]])

    def test_corrupt_cache_only_costs_a_refetch(self):
        real_post = embeddings._post
        embeddings._post = lambda texts: [_vec(0.4) for _ in texts]
        prev = config.SEMANTIC
        config.SEMANTIC = True
        try:
            self.assertIsNotNone(embeddings.embed(["x"]))
            # TmpStore points STATE_DIR at its own temp dir (outside the
            # module SANDBOX): assert we write there, never anywhere else.
            self.assertTrue(str(config.STATE_DIR).startswith(self._state),
                            "the cache must live in the test's state dir")
            embeddings._cache_path().write_text("{not json", encoding="utf-8")
            self.assertIsNotNone(embeddings.embed(["x"]),
                                 "a torn cache must degrade to a refetch")
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev

    def test_cache_key_separates_endpoint_and_model(self):
        base = embeddings._key("same text")
        prev_ep, prev_model = config.EMBEDDING_ENDPOINT, config.EMBEDDING_MODEL
        try:
            config.EMBEDDING_ENDPOINT = prev_ep + "/other"
            self.assertNotEqual(embeddings._key("same text"), base)
            config.EMBEDDING_ENDPOINT = prev_ep
            config.EMBEDDING_MODEL = "other-model"
            self.assertNotEqual(embeddings._key("same text"), base)
        finally:
            config.EMBEDDING_ENDPOINT, config.EMBEDDING_MODEL = prev_ep, prev_model

    def test_cosine_is_total_and_never_raises(self):
        self.assertAlmostEqual(embeddings.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(embeddings.cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(embeddings.cosine([], []), 0.0)
        self.assertEqual(embeddings.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)
        self.assertEqual(embeddings.cosine([1.0], [1.0, 0.0]), 0.0)


class _EmbedHandler(BaseHTTPRequestHandler):
    """Serves /v1/embeddings with deterministic vectors, rows reversed."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.last_payload = body
        self.server.last_auth = self.headers.get("Authorization")
        texts = body.get("input", [])
        # Deliberately reversed: the client must re-align by `index`.
        rows = [{"index": i, "embedding": _vec(0.1 * (i + 1))}
                for i in range(len(texts))][::-1]
        data = json.dumps({"data": rows}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TestEmbeddingWire(unittest.TestCase):
    """The urllib path against a real local server (ephemeral port)."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedHandler)
        self.server.last_payload = None
        self.server.last_auth = None
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._prev = (config.SEMANTIC, config.EMBEDDING_ENDPOINT,
                      config.EMBEDDING_MODEL, config.LLM_API_KEY)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (config.SEMANTIC, config.EMBEDDING_ENDPOINT,
         config.EMBEDDING_MODEL, config.LLM_API_KEY) = self._prev

    def _point_at_server(self):
        config.SEMANTIC = True
        config.EMBEDDING_ENDPOINT = f"http://127.0.0.1:{self.server.server_address[1]}"
        config.EMBEDDING_MODEL = "test-embed"
        config.LLM_API_KEY = ""

    def test_roundtrip_aligns_by_index_and_sends_model(self):
        self._point_at_server()
        embeddings.clear_cache()
        vecs = embeddings.embed(["alpha", "beta", "gamma"])
        self.assertIsNotNone(vecs)
        self.assertEqual(len(vecs), 3)
        # Row i carries cosine 0.1*(i+1) with (1,0) — check alignment survived
        # the reversed response.
        for i, vec in enumerate(vecs):
            self.assertAlmostEqual(vec[0], 0.1 * (i + 1), places=6)
        self.assertEqual(self.server.last_payload["model"], "test-embed")
        self.assertEqual(self.server.last_payload["input"], ["alpha", "beta", "gamma"])
        self.assertIsNone(self.server.last_auth)

    def test_api_key_travels_as_bearer(self):
        self._point_at_server()
        config.LLM_API_KEY = "sekrit"
        embeddings.clear_cache()
        self.assertIsNotNone(embeddings.embed(["alpha"]))
        self.assertEqual(self.server.last_auth, "Bearer sekrit")

    def test_dead_endpoint_is_silent_and_fast_enough(self):
        config.SEMANTIC = True
        config.EMBEDDING_ENDPOINT = "http://127.0.0.1:1"  # nothing listens here
        config.EMBEDDING_MODEL = "test-embed"
        embeddings.clear_cache()
        self.assertIsNone(embeddings.embed(["alpha"]))


class TestSemanticRecall(TmpStore):
    """The channel as search() uses it: off, failing, capped, rescuing, cached."""

    def _put(self, title, body):
        rec = MemoryRecord(title=title, content=body, type="fact")
        store.write_memory(rec)
        return rec

    def test_off_by_default_makes_zero_embedding_calls(self):
        self._put("Deploy day", "The deployment pipeline runs on Tuesdays.")
        calls = []
        real_post = embeddings._post
        embeddings._post = lambda texts: calls.append(texts) or [[1.0]]
        try:
            store.search("when do releases go out", federated=False)
        finally:
            embeddings._post = real_post
        self.assertEqual(calls, [], "semantic off must never reach the network")

    def test_endpoint_failure_falls_back_to_the_lexical_result(self):
        self._put("Deploy day", "The deployment pipeline runs on Tuesdays.")
        self._put("Snack order", "The team prefers salty snacks.")
        baseline = [m.title for m in
                    store.search("deployment pipeline", federated=False)]
        real_post = embeddings._post
        embeddings._post = lambda texts: None       # endpoint unreachable
        prev = config.SEMANTIC
        config.SEMANTIC = True
        try:
            hits = store.search("deployment pipeline", federated=False)
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual([m.title for m in hits], baseline,
                         "a dead endpoint must change nothing, silently")

    def test_a_paraphrase_with_no_word_overlap_is_rescued(self):
        wanted = self._put("Deploy day", "The deployment pipeline runs on Tuesdays.")
        self._put("Snack order", "The team prefers salty snacks.")
        q = "when do releases go out"
        hay_wanted = _haystack(wanted)

        def fake_post(texts):
            out = []
            for t in texts:
                if t == q:
                    out.append([1.0, 0.0])
                elif t == hay_wanted:
                    out.append(_vec(0.9))      # strong semantic match
                else:
                    out.append(_vec(0.05))     # irrelevant
            return out

        real_post = embeddings._post
        embeddings._post = fake_post
        prev = config.SEMANTIC
        config.SEMANTIC = True
        try:
            # Lexically this query shares no word with the memory (below the
            # threshold); only the capped semantic score 0.9*0.8=0.72 rescues it.
            hits = store.search(q, federated=False)
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual([m.title for m in hits], ["Deploy day"])
        # And with the switch off the same query finds nothing at all.
        self.assertEqual(store.search(q, federated=False), [])

    def test_a_perfect_lexical_match_outranks_a_higher_semantic_rival(self):
        exact = self._put("Rollback", "rollback is one command: make undo")
        self._put("Undo flow", "entirely different wording, shares nothing")
        q = "rollback is one command"

        def fake_post(texts):
            # The rival gets a near-perfect vector similarity; the cap must
            # still keep it below the exact word match.
            return [[1.0, 0.0] if t == q else _vec(0.99) for t in texts]

        real_post = embeddings._post
        embeddings._post = fake_post
        prev = config.SEMANTIC
        config.SEMANTIC = True
        try:
            hits = store.search(q, federated=False)
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual(hits[0].title, "Rollback",
                         "a vector outranked an exact word match")

    def test_a_warm_cache_costs_no_second_request(self):
        self._put("Deploy day", "The deployment pipeline runs on Tuesdays.")
        posts = []

        def fake_post(texts):
            posts.append(list(texts))
            return [[1.0, 0.0]] + [_vec(0.5) for _ in texts[1:]]

        real_post = embeddings._post
        embeddings._post = fake_post
        prev = config.SEMANTIC
        config.SEMANTIC = True
        try:
            store.search("deployment", federated=False)
            store.search("deployment", federated=False)
        finally:
            embeddings._post = real_post
            config.SEMANTIC = prev
        self.assertEqual(len(posts), 1,
                         "query and haystacks must be cached after one batch")


class TestStatusShowsSemanticState(TmpStore):
    def test_status_reports_the_switch(self):
        import argparse
        from foldcrumbs import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._cmd_status(argparse.Namespace())
        out = buf.getvalue()
        self.assertIn("semantic recall: off", out)
        self.assertIn("FOLDCRUMBS_SEMANTIC=1", out)


if __name__ == "__main__":
    unittest.main()
