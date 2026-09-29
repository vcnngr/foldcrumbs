"""Optional bundled embedding model — the all-inclusive local channel.

Constitutional rule (owner, 2026-09-29): the core of foldcrumbs stays
stdlib-only, FOREVER. Everything in this module is optional: it imports
``onnxruntime`` lazily and inside guards, and every public function
degrades to an honest "not installed / not available" answer instead of
raising. A machine that never installed the ``foldcrumbs[semantic]`` extra
never touches this code path beyond an ``available()`` check.

Model: Xenova/all-MiniLM-L6-v2 (ONNX, dynamic-quantized), revision and
SHA-256 pinned below — setup() refuses anything that does not match, so a
tampered CDN cannot poison the bundle. Vectors: mean-pooled over attended
tokens, L2-normalized (the sentence-transformers recipe), 384 dims.

Tokenizer: our own stdlib WordPiece (foldcrumbs._wordpiece), validated
ID-for-ID against the official HF tokenizer (tests/test_wordpiece.py).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request

from . import _wordpiece, config

MODEL_REV = "751bff37182d3f1213fa05d7196b954e230abad9"
_HF_BASE = ("https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/"
            + MODEL_REV)
MODEL_FILE = "model_quantized.onnx"
VOCAB_FILE = "vocab.txt"
MODEL_SHA256 = ("afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170af"
                "c848bdb1")
VOCAB_SHA256 = ("07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c"
                "992038a3")
MODEL_BYTES = 22972370
VOCAB_BYTES = 231508
MAX_LEN = 256          # model cap is 512; memories are short — 256 is ample
EMBED_DIM = 384


def _bundle_dir():
    return config.STATE_DIR / "bundled"


def _model_path():
    return _bundle_dir() / MODEL_FILE


def _vocab_path():
    return _bundle_dir() / VOCAB_FILE


def _runtime_error() -> str | None:
    """Why the runtime is unavailable, or None when it is.

    Catches OSError too (RT PR #81 P0-2): a present-but-broken native
    install fails at dlopen with OSError, not ImportError — that must
    degrade to "not available", never escape into recall.
    """
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return ("onnxruntime not installed — "
                "pip install 'foldcrumbs[semantic]'")
    except OSError as e:
        return f"onnxruntime present but failed to load ({e})"
    return None


def installed() -> bool:
    """True when the pinned model + vocab are present with correct hashes.

    TOTAL on I/O errors (RT PR #81 r2 P0): a state dir that became
    unreadable mid-flight (dying disk, permissions, torn file) must
    degrade to "not installed" — never propagate an OSError into recall.
    """
    try:
        if not _model_path().exists() or not _vocab_path().exists():
            return False
        return (_sha256(_model_path()) == MODEL_SHA256
                and _sha256(_vocab_path()) == VOCAB_SHA256)
    except OSError:
        return False


def available() -> bool:
    """True when the bundled channel can actually embed right now.

    Total as well: any unexpected error here means "not available",
    because the caller (recall) must never see an exception from an
    OPTIONAL channel.
    """
    try:
        return _runtime_error() is None and installed()
    except Exception:  # noqa: BLE001 — fail-soft by contract (module doc)
        return False


def status() -> dict:
    """Machine-readable state for `foldcrumbs embeddings status`.

    Total on I/O errors like installed(): a status command on a broken
    state dir reports the damage, it does not traceback.
    """
    def _ok(path, expected) -> bool:
        try:
            return path.exists() and _sha256(path) == expected
        except OSError:
            return False

    def _present(path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    model_ok = _ok(_model_path(), MODEL_SHA256)
    vocab_ok = _ok(_vocab_path(), VOCAB_SHA256)
    runtime = _runtime_error()
    return {
        "runtime": runtime or "onnxruntime ok",
        "model": str(_model_path()),
        "model_present": _present(_model_path()),
        "model_sha256_ok": model_ok,
        "vocab_present": _present(_vocab_path()),
        "vocab_sha256_ok": vocab_ok,
        "installed": model_ok and vocab_ok,
        "available": runtime is None and model_ok and vocab_ok,
        "revision": MODEL_REV,
    }


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def setup(verbose: bool = True) -> bool:
    """Download + verify the pinned bundle. Idempotent. True on success.

    Runtime is checked FIRST (RT PR #81 P0-3): a valid bundle on disk with
    no usable onnxruntime is NOT success — the channel cannot embed, and
    saying "already installed" with exit 0 while status says "not
    available" is a lie. The whole install is transactional (P1): both
    files are staged and verified as .part temps, then committed together,
    so a mid-install failure never leaves a half bundle behind.
    """
    err = _runtime_error()
    if err:
        if verbose:
            print(f"cannot use the bundled model yet: {err}")
        return False
    if installed():
        if verbose:
            print("bundled model already installed and verified.")
        return True
    if verbose:
        print(f"downloading {MODEL_FILE} (~{MODEL_BYTES // 10**6} MB, "
              f"rev {MODEL_REV[:8]}) …")
    bundle = _bundle_dir()
    staged: list[tuple] = []     # (tmp_path, final_path)
    committed: list = []         # finals already renamed — rollback set
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        for fname, url, sha, size in (
                (MODEL_FILE, f"{_HF_BASE}/onnx/{MODEL_FILE}",
                 MODEL_SHA256, MODEL_BYTES),
                (VOCAB_FILE, f"{_HF_BASE}/{VOCAB_FILE}",
                 VOCAB_SHA256, VOCAB_BYTES)):
            tmp = _stage_download(url, bundle / (fname + ".part"), sha, size)
            staged.append((tmp, bundle / fname))
        for tmp, dest in staged:         # both verified — commit together
            os.replace(tmp, dest)
            committed.append(dest)       # track for rollback (RT r2 P1)
        staged.clear()
    except Exception as e:  # noqa: BLE001 — user-facing, never a traceback
        if verbose:
            print(f"setup failed: {e}")
        for tmp, _ in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        for dest in committed:           # a half-committed pair is no pair
            try:
                dest.unlink()
            except OSError:
                pass
        return False
    if verbose:
        print("verifying …")
    ok = installed()
    if verbose:
        print("ok — bundled local embeddings ready." if ok
              else "verification failed after download.")
    if not ok:
        # never leave a failed bundle claimable as installed
        remove(verbose=False)
    return ok


def _stage_download(url: str, tmp, expected_sha: str,
                    expected_bytes: int) -> str:
    """Download to ``tmp``, verify size AND sha256, return the tmp path.

    The caller commits (renames) only after EVERY file passed — one bad
    file rolls the whole install back.
    """
    fd, tmp_name = tempfile.mkstemp(dir=tmp.parent, suffix=".part")
    try:
        total = 0
        with os.fdopen(fd, "wb") as fh:
            req = urllib.request.Request(url, headers={
                "User-Agent": "foldcrumbs-embeddings-setup"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    total += len(chunk)
                    fh.write(chunk)
        if total != expected_bytes:
            raise ValueError(
                f"{tmp.name}: expected {expected_bytes} bytes, got {total}")
        got = _sha256(tmp_name)
        if got != expected_sha:
            raise ValueError(
                f"{tmp.name}: sha256 mismatch — expected {expected_sha}, "
                f"got {got} (refusing to install)")
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return tmp_name


def remove(verbose: bool = True) -> bool:
    """Delete the bundle. Absent files are not an error."""
    removed = False
    for p in (_model_path(), _vocab_path()):
        try:
            p.unlink()
            removed = True
        except OSError:
            pass
    try:
        _bundle_dir().rmdir()
    except OSError:
        pass
    _SESSION.clear()
    if verbose:
        print("bundled model removed." if removed
              else "nothing to remove.")
    return True


# --- inference ---------------------------------------------------------------

_SESSION: dict = {}      # one InferenceSession per process, lazily built
_VOCAB: dict[str, int] | None = None


def _get_session():
    if "s" not in _SESSION:
        import onnxruntime as ort
        _SESSION["s"] = ort.InferenceSession(
            str(_model_path()), providers=["CPUExecutionProvider"])
    return _SESSION["s"]


def _get_vocab() -> dict[str, int]:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _wordpiece._load_vocab(str(_vocab_path()))
    return _VOCAB


def embed(texts: list[str]) -> list[list[float]] | None:
    """Vectors for ``texts`` from the bundled model, or None.

    None whenever the channel is not fully available (runtime missing,
    bundle absent/corrupt, inference error) — the caller falls back, never
    sees an exception. Batched in ONE session run; mean-pooled over
    attended tokens and L2-normalized.
    """
    if not texts or not available():
        return None
    try:
        import numpy as np

        vocab = _get_vocab()
        encoded = [_wordpiece.encode(t, vocab, max_len=MAX_LEN) for t in texts]
        ids = np.array([e[0] for e in encoded], dtype=np.int64)
        mask = np.array([e[1] for e in encoded], dtype=np.int64)
        types = np.zeros_like(ids)
        out = np.asarray(_get_session().run(
            None, {"input_ids": ids, "attention_mask": mask,
                   "token_type_ids": types})[0], dtype=np.float32)  # [B,S,384]
        m = mask[:, :, None].astype(np.float32)
        summed = (out * m).sum(axis=1)
        counts = m.sum(axis=1).clip(min=1.0)
        pooled = summed / counts                       # mean over real tokens
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-12)
        unit = pooled / norms                          # L2-normalized
        return [list(map(float, row)) for row in unit]
    except Exception:  # noqa: BLE001 — any failure is an honest None
        return None


def cache_basis() -> str:
    """Stable key-basis for the vector cache: bundle revision, not an URL.

    A model change (new revision pin) invalidates naturally, exactly like
    an endpoint change does for server vectors.
    """
    return f"bundled:minilm@{MODEL_REV[:12]}"
