"""Optional embedding support for recall — pure stdlib, opt-in, fail-soft.

Recall is lexical by design: substring, word overlap and difflib fuzzy, so it
works on any machine with a Python and never blocks on a service. Semantic
scoring is an *additional* signal on top of that, never a replacement, and it
is guarded by two gates the user controls:

1. ``FOLDCRUMBS_SEMANTIC=1`` — the explicit switch. Without it nothing in this
   module is ever called, so an uninterested machine behaves exactly as before:
   no extra requests, no new failure modes, no latency.
2. An embedding endpoint that actually answers. The request goes to
   ``EMBEDDING_ENDPOINT/v1/embeddings`` (OpenAI-compatible — the same protocol
   the distillation endpoint already speaks), with a short timeout. If the
   endpoint is absent, slow, or errors, the caller gets ``None`` and recall
   falls back to lexical silently. Never blocking, never raising.

Vectors are cached in the machine-local state dir (NOT the memory store: a
store may be synced across machines whose embedding endpoints differ, so a
synced cache would poison the others). The cache key folds in endpoint + model
+ text, so changing either invalidates naturally.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import urllib.error
import urllib.request

from . import config


def _cache_path():
    return config.STATE_DIR / "semantic-cache.json"


def _load_cache() -> dict[str, list[float]]:
    """Best-effort: a missing or torn cache only costs a re-fetch."""
    try:
        with _cache_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if isinstance(v, list) and v
                    and all(isinstance(x, (int, float)) for x in v)}
    except (OSError, ValueError):
        pass
    return {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    """Atomic replace; best-effort — a failed save only costs a re-fetch."""
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=config.STATE_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, _cache_path())
    except OSError:
        pass


def _key(text: str) -> str:
    # Endpoint and model both shape the vector: same text through a different
    # one is a different point in a different space, so they share no keys.
    basis = f"{_endpoint()}\x00{_model()}\x00{text}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _model() -> str:
    return config.EMBEDDING_MODEL or config.LLM_MODEL


# --- Opt-in loopback discovery (0.12.0, design docs/design/local-semantic) ---
# FOLDCRUMBS_EMBEDDING_AUTO=1 lets a machine WITHOUT an explicit endpoint find
# a local embeddings server by itself: ollama, llama-server, MLX/LM Studio —
# all speak /v1/embeddings. Rules, all deliberate:
#   * opt-in: without the switch this code never runs (zero behaviour change);
#   * loopback-only: candidates are 127.0.0.1/localhost, never a remote host;
#   * never overrides the user: an explicit endpoint (env or state file) wins;
#   * once per process, then cached in the state dir (non-synced: machines
#     differ); a dead cached endpoint is simply re-probed on next process;
#   * honest failure: nothing answers → None → the lexical fallback engages.
_DEFAULT_CANDIDATES = (
    "http://127.0.0.1:11434/v1",   # ollama (OpenAI-compatible shim)
    "http://127.0.0.1:8080/v1",    # llama-server
    "http://localhost:8081/v1",    # MLX / LM Studio compat (distill default)
)
_DISCOVERED: str | None = None


def _discovery_reset() -> None:
    """Test hook: forget the in-process discovery result."""
    global _DISCOVERED
    _DISCOVERED = None


def _discovery_cache_path():
    return config.STATE_DIR / "embedding-endpoint-discovered"


def _probe_endpoint(base: str) -> bool:
    """True when ``base`` answers a minimal /v1/embeddings POST in time."""
    url = base.rstrip("/") + "/embeddings"
    payload = json.dumps({"model": _model(), "input": ["ping"]}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(
                req, timeout=config.EMBEDDING_PROBE_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False
    # A real embeddings server returns aligned vectors — accept only that.
    data = body.get("data") if isinstance(body, dict) else None
    return bool(data) and all(isinstance(d, dict) and d.get("embedding")
                              for d in data)


def discover_local_endpoint(probe=None, candidates=None) -> str | None:
    """Return a working local /v1 base, or None. Pure-ish: ``probe`` and
    ``candidates`` are injectable so tests never touch the network.

    Precedence: an endpoint the user configured explicitly always wins (this
    function is then a no-op returning None — nothing to discover). Otherwise:
    in-process result, then the state-dir cache (validated by a probe — a dead
    cache entry costs one short timeout, not a wrong answer), then the default
    loopback candidates in order.
    """
    global _DISCOVERED
    if not config.EMBEDDING_AUTO:
        return None
    if config.EMBEDDING_ENDPOINT_EXPLICIT:
        return None                      # the user chose; never surprise them
    if probe is None:
        probe = _probe_endpoint
    if candidates is None:
        candidates = _DEFAULT_CANDIDATES
    if _DISCOVERED and probe(_DISCOVERED):
        return _DISCOVERED
    try:
        cached = _discovery_cache_path().read_text(encoding="utf-8").strip()
    except OSError:
        cached = ""
    ordered = ([cached] if cached else []) + [c for c in candidates if c != cached]
    for base in ordered:
        if probe(base):
            _DISCOVERED = base
            try:
                config.STATE_DIR.mkdir(parents=True, exist_ok=True)
                _discovery_cache_path().write_text(base, encoding="utf-8")
            except OSError:
                pass                     # cache is best-effort
            return base
    _DISCOVERED = None
    return None


def _endpoint() -> str:
    """The /v1 base _post() should use: explicit config, else discovered.

    Normalises the historical convention: config.EMBEDDING_ENDPOINT is the
    server root ("http://host:8081", the code appends "/v1/embeddings"),
    while discovery candidates carry "/v1" already. Both end up here as a
    base ending in "/v1".
    """
    if config.EMBEDDING_ENDPOINT_EXPLICIT:
        base = config.EMBEDDING_ENDPOINT
    else:
        found = discover_local_endpoint(candidates=_TEST_CANDIDATES)
        base = found or config.EMBEDDING_ENDPOINT
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _post(texts: list[str]) -> list[list[float]] | None:
    """One batched /v1/embeddings call. None on any failure or odd payload."""
    url = _endpoint().rstrip("/") + "/embeddings"
    payload = {"model": _model(), "input": texts}
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.EMBEDDING_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    try:
        rows = sorted(body["data"], key=lambda d: d["index"])
        vectors = [row["embedding"] for row in rows]
    except (KeyError, IndexError, TypeError):
        return None
    if len(vectors) != len(texts):
        return None
    for vec in vectors:
        if not isinstance(vec, list) or not vec or \
                not all(isinstance(x, (int, float)) for x in vec):
            return None
    return [list(map(float, v)) for v in vectors]


def embed(texts: list[str], candidates=None) -> list[list[float]] | None:
    """Vectors for ``texts``, aligned, or None when any of them is unavailable.

    All-or-nothing on purpose: a mix of semantic and missing vectors would rank
    on two different scales at once, which is worse than ranking on one.
    Cache hits never touch the network; on a miss exactly one batched request
    covers everything missing.

    ``candidates`` overrides the discovery candidate list (test hook; the
    production path uses _DEFAULT_CANDIDATES via discover_local_endpoint).
    """
    if not texts:
        return []
    if not config.SEMANTIC:
        return None          # gate 1: the user did not opt in — never call
    global _TEST_CANDIDATES
    _TEST_CANDIDATES = candidates
    try:
        return _embed_inner(texts)
    finally:
        _TEST_CANDIDATES = None


_TEST_CANDIDATES = None


def _embed_inner(texts: list[str]) -> list[list[float]] | None:
    cache = _load_cache()
    out: list[list[float] | None] = [None] * len(texts)
    missing: list[tuple[int, str, str]] = []
    for i, text in enumerate(texts):
        key = _key(text)
        hit = cache.get(key)
        if hit is not None:
            out[i] = hit
        else:
            missing.append((i, key, text))
    if missing:
        got = _post([text for _, _, text in missing])
        if got is None:      # gate 2: the endpoint did not answer — lexical
            return None
        for (i, key, _), vec in zip(missing, got):
            out[i] = vec
            cache[key] = vec
        _save_cache(cache)
    return out               # type: ignore[return-value]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 for degenerate inputs (never raises)."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cache_size() -> int:
    return len(_load_cache())


def clear_cache() -> None:
    try:
        _cache_path().unlink(missing_ok=True)
    except OSError:
        pass
