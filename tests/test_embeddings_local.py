"""Tests for the OPTIONAL bundled embedding channel (foldcrumbs[semantic]).

The constitution under test (owner, 2026-09-29): the core stays stdlib —
every test here runs WITHOUT onnxruntime installed and without any model
on disk. The bundled channel must then be a polite ghost: available()
False, embed() None, the recall path byte-identical to lexical, and the
CLI commands printing honest guidance instead of crashing.

The happy path (real inference) needs the extra; it is exercised by the
opt-in CI job and locally, and pinned here only as far as pure functions
(hash verification, cache basis, pooling math with a fake session) allow.
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import config, embeddings, embeddings_local  # noqa: E402


class TestWithoutTheExtra(unittest.TestCase):
    """No onnxruntime, no bundle on disk: everything degrades politely."""

    def setUp(self):
        embeddings_local._SESSION.clear()
        embeddings_local._VOCAB = None

    def test_available_is_false_without_runtime_or_bundle(self):
        with mock.patch.object(embeddings_local, "_runtime_error",
                               return_value="onnxruntime not installed"):
            self.assertFalse(embeddings_local.available())

    def test_installed_false_when_files_absent(self):
        self.assertFalse(embeddings_local.installed())

    def test_embed_returns_none_never_raises(self):
        self.assertIsNone(embeddings_local.embed(["anything"]))
        self.assertIsNone(embeddings_local.embed([]))

    def test_status_reports_honest_guidance(self):
        st = embeddings_local.status()
        self.assertFalse(st["available"])
        self.assertFalse(st["installed"])
        self.assertEqual(st["revision"], embeddings_local.MODEL_REV)

    def test_setup_without_runtime_is_honest_failure(self):
        with mock.patch.object(embeddings_local, "_runtime_error",
                               return_value="onnxruntime not installed"):
            self.assertFalse(embeddings_local.setup(verbose=False))

    def test_bundled_basis_none_when_unavailable(self):
        self.assertIsNone(embeddings._bundled_basis())

    def test_recall_chain_untouched_semantic_off(self):
        # SEMANTIC off: embed() is None before any channel logic — the
        # bundled code is never even imported by this path.
        prev = config.SEMANTIC
        config.SEMANTIC = False
        try:
            self.assertIsNone(embeddings.embed(["x"]))
        finally:
            config.SEMANTIC = prev


class TestDownloadVerification(unittest.TestCase):
    """setup() must refuse tampered downloads — hash AND size pinned."""

    def test_sha_mismatch_refuses_install(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "model.onnx"
            # a fake downloader writes wrong bytes
            def fake_urlopen(req, timeout=None):
                class R:
                    def read(self, n):
                        if not hasattr(self, "done"):
                            self.done = True
                            return b"corrupted payload"
                        return b""
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return R()
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                # size correct (17 bytes), hash wrong → sha256 mismatch
                with self.assertRaises(ValueError) as ctx:
                    embeddings_local._download(
                        "https://example.invalid/model.onnx", dest,
                        "0" * 64, 17)
            self.assertIn("sha256 mismatch", str(ctx.exception))
            self.assertFalse(dest.exists(),
                             "a rejected download must not land at dest")

    def test_size_mismatch_refuses_install(self):
        import tempfile
        real_sha = __import__("hashlib").sha256(b"x" * 10).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "model.onnx"
            def fake_urlopen(req, timeout=None):
                class R:
                    def read(self, n):
                        if not hasattr(self, "done"):
                            self.done = True
                            return b"x" * 10
                        return b""
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return R()
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(ValueError):
                    embeddings_local._download(
                        "https://example.invalid/m.onnx", dest,
                        real_sha, 999)   # right hash, wrong size
            self.assertFalse(dest.exists())


class TestCacheBasis(unittest.TestCase):
    def test_basis_is_pinned_to_revision(self):
        basis = embeddings_local.cache_basis()
        self.assertTrue(basis.startswith("bundled:minilm@"))
        self.assertIn(embeddings_local.MODEL_REV[:12], basis)

    def test_basis_differs_from_server_basis(self):
        # A bundled vector must never share a cache key with a server one:
        # different spaces, different keys — even for the same text.
        server_key = embeddings._key("hello", "http://127.0.0.1:11434/v1")
        bundle_key = embeddings._key("hello", embeddings_local.cache_basis())
        self.assertNotEqual(server_key, bundle_key)


# numpy may be absent in the core environment: the pooling test only runs
# when numpy exists (the bundled channel itself requires numpy anyway).
try:
    import numpy  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


@unittest.skipUnless(_HAS_NUMPY, "numpy not installed (core env)")
class TestPoolingWithFakeSession(unittest.TestCase):
    def setUp(self):
        embeddings_local._SESSION.clear()
        embeddings_local._VOCAB = None

    def test_embed_uses_attention_mask_and_l2(self):
        import numpy as np

        # fake vocab covering the test sentence
        vocab = {"[PAD]": 0, "[UNK]": 100, "[CLS]": 101, "[SEP]": 102,
                 "a": 10, "b": 11}
        # fake session: deterministic 'hidden states' = one-hot of ids
        class FakeSession:
            def run(self, _names, feeds):
                ids = feeds["input_ids"]
                b, s = ids.shape
                out = np.zeros((b, s, 4), dtype=np.float32)
                for bi in range(b):
                    for si in range(s):
                        out[bi, si, ids[bi, si] % 4] = 1.0
                return [out]

        with mock.patch.object(embeddings_local, "available",
                               return_value=True), \
             mock.patch.object(embeddings_local, "_get_session",
                               return_value=FakeSession()), \
             mock.patch.object(embeddings_local, "_get_vocab",
                               return_value=vocab):
            vecs = embeddings_local.embed(["a b", "a"])
        self.assertIsNotNone(vecs)
        self.assertEqual(len(vecs), 2)
        for v in vecs:
            self.assertEqual(len(v), 4)
            norm = sum(x * x for x in v) ** 0.5
            self.assertAlmostEqual(norm, 1.0, places=5)  # L2-normalized
        # padding must not contribute: "a b" vs "a" differ
        self.assertNotEqual(vecs[0], vecs[1])

    def test_embed_none_on_inference_error(self):
        class Boom:
            def run(self, *a, **k):
                raise RuntimeError("onnx exploded")

        vocab = {"[PAD]": 0, "[UNK]": 100, "[CLS]": 101, "[SEP]": 102,
                 "a": 10}
        with mock.patch.object(embeddings_local, "available",
                               return_value=True), \
             mock.patch.object(embeddings_local, "_get_session",
                               return_value=Boom()), \
             mock.patch.object(embeddings_local, "_get_vocab",
                               return_value=vocab):
            self.assertIsNone(embeddings_local.embed(["a"]))


class TestChainChannel3(unittest.TestCase):
    """The fallback chain: server first; bundle only when server is down."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("FOLDCRUMBS_SEMANTIC", "FOLDCRUMBS_EMBEDDING_ENDPOINT",
                        "FOLDCRUMBS_EMBEDDING_MODEL")}
        for k in self._saved:
            os.environ.pop(k, None)
        os.environ["FOLDCRUMBS_SEMANTIC"] = "1"
        os.environ["FOLDCRUMBS_EMBEDDING_MODEL"] = "m"
        os.environ["FOLDCRUMBS_EMBEDDING_ENDPOINT"] = "http://127.0.0.1:1"
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)
        importlib.reload(embeddings)
        embeddings._discovery_reset()

    def test_bundle_used_only_after_server_fails(self):
        calls = []

        def fake_post(texts, url_base=None):
            calls.append("server")
            return None

        def fake_bundle(texts):
            calls.append("bundle")
            return [[1.0, 0.0] for _ in texts]

        with mock.patch.object(embeddings, "_post", fake_post), \
             mock.patch.object(embeddings, "_embed_bundled", fake_bundle), \
             mock.patch.object(embeddings, "_bundled_basis",
                               return_value="bundled:test@rev"):
            got = embeddings.embed(["deploy day"])
        self.assertEqual(calls, ["server", "bundle"])
        self.assertEqual(got, [[1.0, 0.0]])
        # cached under the BUNDLE basis, not the server's
        cache = embeddings._load_cache()
        self.assertIn(embeddings._key("deploy day", "bundled:test@rev"), cache)
        self.assertNotIn(embeddings._key("deploy day",
                                         "http://127.0.0.1:1"), cache)

    def test_no_bundle_available_server_down_lexical(self):
        with mock.patch.object(embeddings, "_post", lambda t, u=None: None), \
             mock.patch.object(embeddings, "_bundled_basis",
                               return_value=None):
            self.assertIsNone(embeddings.embed(["x"]))

    def test_warm_bundle_cache_skips_both_channels(self):
        calls = []
        basis = "bundled:test@rev"
        key = embeddings._key("warm text", basis)
        cache = embeddings._load_cache()
        cache[key] = [0.5, 0.5]
        embeddings._save_cache(cache)
        with mock.patch.object(embeddings, "_post",
                               lambda t, u=None: calls.append("server")), \
             mock.patch.object(embeddings, "_embed_bundled",
                               lambda t: calls.append("bundle")), \
             mock.patch.object(embeddings, "_bundled_basis",
                               return_value=basis):
            got = embeddings.embed(["warm text"])
        self.assertEqual(got, [[0.5, 0.5]])
        self.assertEqual(calls, [], "warm cache must hit no channel at all")


if __name__ == "__main__":
    unittest.main()
