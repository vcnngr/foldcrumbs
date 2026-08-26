"""Ingest external documents into the store with provenance.

`foldcrumbs distill` is tuned for SESSION transcripts (its prompt assumes a
session summary). Ingest covers the other half of the capture gap: arbitrary
documents — a markdown ADR, a design note, an article fetched by URL — become
typed memories whose provenance is ``imported`` and whose ``source`` carries
``ingest:<origin>``, so every ingested memory stays traceable to the document
it came from.

Design boundary (deliberate): stdlib only. Local text/markdown files and
http(s) pages (HTML reduced to visible text with ``html.parser``). PDF, OCR
and JavaScript-rendered pages are OUT of scope — adding binary parsers or a
headless browser is exactly the dependency weight foldcrumbs refuses.

Fail-soft contract, same as distill:
* secrets are scrubbed BEFORE the LLM sees the document and again per record;
* a dead LLM degrades to the keyword heuristic — never an error;
* an unreadable file / unreachable URL raises IngestError BEFORE any write,
  so a failed ingest never touches the store.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from . import config, distill, llm, redact
from .schema import MemoryRecord

# Documents are longer than session summaries; still bounded so one giant page
# cannot blow up the extraction prompt. Deterministic head+tail truncation.
_MAX_DOC_CHARS = 24_000
_TRUNC_MARKER = "\n\n[... document truncated ...]\n\n"

_URL_TIMEOUT_S = 15
_URL_MAX_BYTES = 1_000_000  # 1 MB is plenty for a document; a cap, not a fetch-all

_DOC_HEADER = (
    "Extract the durable engineering knowledge from this document. "
    "Prefer decisions, constraints, rules, facts and configuration over "
    "narrative. Each memory must stand alone without the document. Do not "
    "invent anything the document does not state."
)


class IngestError(Exception):
    """The document could not be read (missing file, unreachable URL...)."""


# --- HTML -> visible text ---------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "template", "head"}


class _TextExtractor(HTMLParser):
    """Keeps visible text, drops script/style content entirely."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """Reduce HTML to its visible text. Tolerant: malformed input still parses."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — malformed HTML must never crash ingest
        pass
    return parser.text()


# --- reading the document ---------------------------------------------------

def read_document(source: str) -> tuple[str, str]:
    """Return ``(text, origin)`` for a local path or http(s) URL.

    Raises IngestError on any read failure — callers must write nothing.
    """
    source = source.strip()
    if not source:
        raise IngestError("empty source")
    if source.startswith(("http://", "https://")):
        return _read_url(source), source
    path = Path(source).expanduser()
    if not path.is_file():
        raise IngestError(f"no such file: {source}")
    try:
        return path.read_text(encoding="utf-8", errors="replace"), str(path)
    except OSError as exc:
        raise IngestError(f"cannot read {source}: {exc}") from exc


def _read_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "foldcrumbs-ingest"})
    try:
        with urllib.request.urlopen(req, timeout=_URL_TIMEOUT_S) as resp:  # noqa: S310
            raw = resp.read(_URL_MAX_BYTES + 1)
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise IngestError(f"cannot fetch {url}: {exc}") from exc
    if len(raw) > _URL_MAX_BYTES:
        raise IngestError(f"document too large (> {_URL_MAX_BYTES} bytes): {url}")
    text = raw.decode("utf-8", errors="replace")
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return html_to_text(text)
    return text  # plain text / markdown served over http


# --- truncation -------------------------------------------------------------

def _truncate_doc(text: str) -> str:
    """Bounded, deterministic: keep the head and the tail, mark the cut."""
    if len(text) <= _MAX_DOC_CHARS:
        return text
    half = _MAX_DOC_CHARS // 2
    return text[:half] + _TRUNC_MARKER + text[-half:]


# --- extraction -------------------------------------------------------------

def _extract(text: str) -> list[dict]:
    """LLM extraction with the document prompt; heuristic fallback."""
    answer = llm.chat(
        messages=[
            {"role": "system", "content": _DOC_HEADER},
            {"role": "user", "content": f"=== DOCUMENT ===\n{text}\n=== END DOCUMENT ==="},
        ],
        temperature=0.0,
        json_schema=distill.MEMORY_JSON_SCHEMA if config.LLM_JSON_SCHEMA else None,
    )
    raw = distill.parse_llm_memories(answer) if answer else []
    if not raw:
        raw = distill.heuristic_memories(text)
    return raw


def ingest(source: str, cwd: str | None = None) -> dict[str, int]:
    """Ingest one document into the store. Returns persist-style counts.

    Provenance is ``imported`` and source is ``ingest:<origin>`` — every
    record stays traceable to the document it came from.
    """
    text, origin = read_document(source)  # may raise IngestError; no writes yet
    text = redact.scrub(text.strip())
    if not text:
        return {"created": 0, "validated": 0, "superseded": 0, "total": 0}
    text = _truncate_doc(text)

    records = []
    for item in _extract(text):
        # Same write gate and artifact filter distill applies — one rule set.
        if not distill._passes_gate(item):  # noqa: SLF001 — canonical gate
            continue
        if distill._is_artifact(item.get("title", "")) or \
           distill._is_artifact(item.get("content", "")):
            continue
        records.append(
            MemoryRecord(
                title=redact.scrub(item["title"]),
                content=redact.scrub(item["content"]),
                type=item["type"],
                confidence=item["confidence"],
                provenance="imported",
                source=f"ingest:{origin}",
                tags=item.get("tags", []),
            )
        )
    return distill.persist(records, cwd)
