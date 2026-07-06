"""Scrub obvious secrets before a transcript is sent to the LLM or stored.

Conservative on purpose: it targets well-known credential shapes and explicit
``key = value`` pairs whose key looks sensitive. It does NOT chase generic
high-entropy strings (those produce false positives on hashes, ids, UUIDs).
Defense-in-depth: applied both to the distillation input and to the memory
content/title before it is written to disk.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Known credential shapes — replace the whole token.
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),            # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),       # GitHub PAT/OAuth
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),     # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                 # AWS access key id
    re.compile(r"\bA[SK]IA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),  # Authorization: Bearer …
    re.compile(r"\beyJ[A-Za-z0-9._-]{20,}\b"),           # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
]

# key = value / "key": "value" where the key name looks sensitive — redact value.
_KV_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"client[_-]?secret|auth[_-]?token|private[_-]?key)\b"
    r"(\s*[:=]\s*)"
    r"(['\"]?)([^\s'\"]{4,})\3"
)


def scrub(text: str) -> str:
    """Return ``text`` with recognised secrets replaced by ``[REDACTED]``."""
    if not text:
        return text
    for pat in _TOKEN_PATTERNS:
        text = pat.sub(_REDACTED, text)
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    return text
