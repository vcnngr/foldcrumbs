"""Tests for `foldcrumbs ingest` — document ingestion with provenance.

The contract under test:
* local files and URLs both produce memories with provenance="imported";
* the source field carries `ingest:<origin>` so every memory is traceable;
* HTML is reduced to visible text via a stdlib HTMLParser (no JS, no CDN);
* secrets are scrubbed before the LLM ever sees the document;
* a dead LLM degrades to the heuristic path (never an error);
* a dead URL produces a clean error and writes NOTHING to the store;
* truncation is deterministic (head + tail + marker) for oversized docs.
"""

import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sandbox BEFORE foldcrumbs: isolates STATE_DIR so no test touches the
# developer's real ~/.foldcrumbs or federation registry.
from _sandbox import SANDBOX, is_inside  # noqa: E402,F401  (import for side effect)

from foldcrumbs import distill, ingest, store  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_SAMPLE_MD = """\
# Architecture Decision Record 001

We decided to use stdlib only for the hooks.

## Rationale

The hooks must never fail to import. A missing dependency at import time
breaks the entire session. Therefore no third-party packages are allowed
in the hook layer.
"""

_SAMPLE_HTML = """\
<!doctype html>
<html><head><title>Test Page</title>
<style>body{color:red}</style>
<script>var x = "secret";</script>
</head><body>
<h1>Visible heading</h1>
<p>First paragraph with real content.</p>
<script>alert('hidden')</script>
<p>Second paragraph.</p>
</body></html>
"""


class _DocHandler(BaseHTTPRequestHandler):
    """Serves _SAMPLE_HTML once per request; silent logs."""
    def do_GET(self):
        body = _SAMPLE_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass


def _start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _DocHandler)
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------------------------------------------------------------------------
# html_to_text
# ---------------------------------------------------------------------------

class TestHtmlToText(unittest.TestCase):

    def test_strips_script_and_style(self):
        text = ingest.html_to_text(_SAMPLE_HTML)
        self.assertNotIn("secret", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)

    def test_keeps_visible_text(self):
        text = ingest.html_to_text(_SAMPLE_HTML)
        self.assertIn("Visible heading", text)
        self.assertIn("First paragraph with real content.", text)
        self.assertIn("Second paragraph.", text)

    def test_empty_html(self):
        self.assertEqual(ingest.html_to_text("").strip(), "")

    def test_plain_text_passthrough(self):
        text = ingest.html_to_text("no tags here")
        self.assertIn("no tags here", text)


# ---------------------------------------------------------------------------
# read_document — local files
# ---------------------------------------------------------------------------

class TestReadDocumentFile(TmpStore):

    def test_reads_markdown_file(self):
        p = Path(self.dir) / "adr001.md"
        p.write_text(_SAMPLE_MD, encoding="utf-8")
        text, origin = ingest.read_document(str(p))
        self.assertIn("stdlib only", text)
        self.assertEqual(origin, str(p))

    def test_missing_file_raises(self):
        with self.assertRaises(ingest.IngestError):
            ingest.read_document("/nonexistent/nope.md")


# ---------------------------------------------------------------------------
# read_document — URLs (local HTTP server)
# ---------------------------------------------------------------------------

class TestReadDocumentUrl(TmpStore):

    def setUp(self):
        super().setUp()
        self._srv = _start_server()
        self._port = self._srv.server_address[1]

    def tearDown(self):
        self._srv.shutdown()
        super().tearDown()

    def test_fetches_html_and_extracts_text(self):
        url = f"http://127.0.0.1:{self._port}/page"
        text, origin = ingest.read_document(url)
        self.assertIn("Visible heading", text)
        self.assertIn("First paragraph", text)
        self.assertEqual(origin, url)

    def test_dead_url_raises_ingest_error(self):
        # Port 1 is never listening.
        with self.assertRaises(ingest.IngestError):
            ingest.read_document("http://127.0.0.1:1/dead")

    def test_malformed_url_raises_ingest_error_not_traceback(self):
        # Spaces in a URL make Request() raise InvalidURL (a ValueError) at
        # construction time. It must surface as IngestError, never a raw
        # traceback (regression test for review F1).
        with self.assertRaises(ingest.IngestError):
            ingest.read_document("http://host/bad path")


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------

class TestTruncation(unittest.TestCase):

    def test_short_doc_unchanged(self):
        short = "hello world"
        self.assertEqual(ingest._truncate_doc(short), short)

    def test_long_doc_deterministic_head_tail(self):
        long = "A" * 30_000
        result = ingest._truncate_doc(long)
        self.assertLessEqual(len(result), ingest._MAX_DOC_CHARS + 100)
        self.assertIn(ingest._TRUNC_MARKER, result)
        # deterministic
        self.assertEqual(ingest._truncate_doc(long), result)

    def test_long_doc_keeps_head_and_tail(self):
        long = "H" * 20_000 + "M" * 10_000 + "T" * 20_000
        result = ingest._truncate_doc(long)
        self.assertIn("H", result[:200])
        self.assertIn("T", result[-200:])


# ---------------------------------------------------------------------------
# ingest() — provenance and source
# ---------------------------------------------------------------------------

_LLM_JSON = (
    '{"memories": [{"title": "stdlib only for hooks", '
    '"content": "The hooks must never fail to import.", '
    '"type": "decision", "confidence": 0.95, "tags": ["hooks"]}]}'
)


class TestIngestProvenance(TmpStore):

    def setUp(self):
        super().setUp()
        # Source documents live OUTSIDE the store: writing the doc into the
        # store dir would make the scanner read it back as a raw memory.
        self.docdir = tempfile.mkdtemp(prefix="ccmem_ingest_src_")

    def test_records_have_imported_provenance(self):
        p = Path(self.docdir) / "doc.md"
        p.write_text(_SAMPLE_MD, encoding="utf-8")
        with patch.object(distill.llm, "chat", return_value=_LLM_JSON):
            counts = ingest.ingest(str(p), cwd=self.dir)
        self.assertGreater(counts["created"], 0)
        mems = store.load_all(self.dir)
        self.assertTrue(all(m.provenance == "imported" for m in mems),
                        [m.provenance for m in mems])

    def test_source_carries_ingest_origin(self):
        p = Path(self.docdir) / "doc.md"
        p.write_text(_SAMPLE_MD, encoding="utf-8")
        with patch.object(distill.llm, "chat", return_value=_LLM_JSON):
            ingest.ingest(str(p), cwd=self.dir)
        mems = store.load_all(self.dir)
        self.assertTrue(all(m.source.startswith("ingest:") for m in mems),
                        [m.source for m in mems])
        self.assertTrue(any(str(p) in m.source for m in mems))

    def test_url_origin_in_source(self):
        srv = _start_server()
        port = srv.server_address[1]
        try:
            url = f"http://127.0.0.1:{port}/page"
            with patch.object(distill.llm, "chat", return_value=_LLM_JSON):
                ingest.ingest(url, cwd=self.dir)
            mems = store.load_all(self.dir)
            self.assertTrue(any(url in m.source for m in mems),
                            [m.source for m in mems])
        finally:
            srv.shutdown()


# ---------------------------------------------------------------------------
# secret scrubbing
# ---------------------------------------------------------------------------

class TestIngestScrubbing(TmpStore):

    def test_api_key_scrubbed_before_llm(self):
        doc = "The API key is ***...z789 for the service."
        docdir = tempfile.mkdtemp(prefix="ccmem_ingest_sec_")
        p = Path(docdir) / "secret.md"
        p.write_text(doc, encoding="utf-8")
        captured = []
        def _capture(messages, **kw):
            captured.append(str(messages))
            return _LLM_JSON
        with patch.object(distill.llm, "chat", side_effect=_capture):
            ingest.ingest(str(p), cwd=self.dir)
        joined = "".join(captured)
        self.assertNotIn("sk-pro...z789", joined)

    def test_secret_in_url_origin_never_reaches_disk(self):
        # Review F1 (GPT): the origin is user-controlled input; a credential
        # in the query must not be persisted in `source`. The fetch itself
        # succeeds — the leak would land on disk.
        srv = _start_server()
        port = srv.server_address[1]
        try:
            url = f"http://127.0.0.1:{port}/page?password=***"
            with patch.object(distill.llm, "chat", return_value=_LLM_JSON):
                ingest.ingest(url, cwd=self.dir)
            for f in Path(self.dir).glob("*.md"):
                content = f.read_text(encoding="utf-8")
                self.assertNotIn("supersecret", content, f"{f.name} leaks the secret")
            mems = store.load_all(self.dir)
            self.assertTrue(mems, "ingest produced no memories")
            self.assertTrue(all(m.source.startswith("ingest:") for m in mems))
            self.assertTrue(any("[REDACTED]" in m.source for m in mems),
                            [m.source for m in mems])
        finally:
            srv.shutdown()

    def test_userinfo_url_fails_clean_and_writes_nothing(self):
        # urlopen cannot connect through user:pass@ userinfo — the failure
        # must surface as IngestError with zero writes (no leaked origin).
        before = list(Path(self.dir).glob("*.md"))
        with self.assertRaises(ingest.IngestError):
            ingest.ingest("http://user:***@127.0.0.1:1/page", cwd=self.dir)
        self.assertEqual(before, list(Path(self.dir).glob("*.md")))

    def test_safe_origin_strips_userinfo(self):
        out = ingest._safe_origin("https://user:***@example.com/doc")
        self.assertNotIn("supersecret", out)
        self.assertNotIn("user:", out)
        self.assertIn("example.com/doc", out)

    def test_safe_origin_scrubs_query(self):
        out = ingest._safe_origin("https://example.com/doc?password=***")
        self.assertNotIn("supersecret", out)
        self.assertIn("[REDACTED]", out)


# ---------------------------------------------------------------------------
# LLM fallback — heuristic path
# ---------------------------------------------------------------------------

class TestIngestLlmFallback(TmpStore):

    def test_dead_llm_uses_heuristic(self):
        docdir = tempfile.mkdtemp(prefix="ccmem_ingest_fb_")
        p = Path(docdir) / "doc.md"
        p.write_text(_SAMPLE_MD, encoding="utf-8")
        with patch.object(distill.llm, "chat", return_value=None):
            counts = ingest.ingest(str(p), cwd=self.dir)
        # heuristic may or may not produce records, but must not raise
        self.assertIn("created", counts)
        self.assertIn("total", counts)


# ---------------------------------------------------------------------------
# zero writes on failure
# ---------------------------------------------------------------------------

class TestIngestFailureWritesNothing(TmpStore):

    def test_dead_url_creates_no_files(self):
        before = list(Path(self.dir).glob("*.md"))
        with self.assertRaises(ingest.IngestError):
            ingest.ingest("http://127.0.0.1:1/dead", cwd=self.dir)
        after = list(Path(self.dir).glob("*.md"))
        self.assertEqual(before, after)

    def test_missing_file_creates_no_files(self):
        before = list(Path(self.dir).glob("*.md"))
        with self.assertRaises(ingest.IngestError):
            ingest.ingest("/nonexistent/nope.md", cwd=self.dir)
        after = list(Path(self.dir).glob("*.md"))
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# CLI: foldcrumbs ingest
# ---------------------------------------------------------------------------

class TestCliIngest(TmpStore):

    def test_cli_file_ingest(self):
        docdir = tempfile.mkdtemp(prefix="ccmem_ingest_cli_")
        p = Path(docdir) / "cli_doc.md"
        p.write_text(_SAMPLE_MD, encoding="utf-8")
        with patch.object(distill.llm, "chat", return_value=_LLM_JSON):
            from foldcrumbs import cli
            import argparse
            ns = argparse.Namespace(source=str(p))
            rc = cli._cmd_ingest(ns)
        self.assertEqual(rc, 0)
        mems = store.load_all(self.dir)
        self.assertGreater(len(mems), 0)

    def test_cli_missing_file_exits_nonzero(self):
        from foldcrumbs import cli
        import argparse
        ns = argparse.Namespace(source="/nonexistent/nope.md")
        with self.assertRaises(SystemExit) as ctx:
            cli._cmd_ingest(ns)
        self.assertNotEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# MCP: ingest tool present
# ---------------------------------------------------------------------------

class TestMcpIngestTool(unittest.TestCase):

    def test_ingest_tool_in_catalog(self):
        from foldcrumbs import mcp_server
        names = [t["name"] for t in mcp_server.TOOLS]
        self.assertIn("ingest", names)

    def test_ingest_tool_has_source_param(self):
        from foldcrumbs import mcp_server
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "ingest")
        props = tool["inputSchema"]["properties"]
        self.assertIn("source", props)
        self.assertIn("source", tool["inputSchema"].get("required", []))


if __name__ == "__main__":
    unittest.main()
