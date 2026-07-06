"""MemoryRecord — typed memory with trust/decay logic.

Adapted from memanto's ``app/core.py`` (MIT). Moorcheh/namespace coupling
removed; serialization is to a Markdown file with YAML-ish frontmatter that
matches the host's existing memory format (name / description / type + body).
Trust scoring (compute_confidence / validate / mark_superseded / trust_score)
is kept faithful to the original.

Pure stdlib (dataclasses) — no pydantic — so hooks never fail to import.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# The 13 memory types (memanto/app/constants.py).
VALID_TYPES = {
    "fact",
    "preference",
    "goal",
    "decision",
    "artifact",
    "learning",
    "event",
    "instruction",
    "relationship",
    "context",
    "observation",
    "commitment",
    "error",
}

VALID_PROVENANCE = {
    "explicit_statement",
    "inferred",
    "corrected",
    "validated",
    "observed",
    "imported",
}

VALID_STATUS = {"active", "superseded", "deleted", "provisional"}

# Host's existing frontmatter also uses these non-memanto type labels; we accept
# them so legacy files round-trip without being rewritten.
LEGACY_TYPES = {"project", "feedback", "reference", "session", "user", "incident"}

_PROVENANCE_WEIGHTS = {
    "explicit_statement": 1.0,
    "validated": 0.95,
    "observed": 0.85,
    "corrected": 0.9,
    "inferred": 0.7,
    "imported": 0.8,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return _now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _now()


def slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (s or "memory")[:max_len]


# Markdown noise that must never leak into a title/description (would break the
# YAML frontmatter and produce ugly slugs/index lines).
_MD_NOISE_RE = re.compile(r"[*_`#>]+|^\s*[-*•]\s+|^\s*\d+\.\s+", re.MULTILINE)


def clean_line(text: str, max_len: int = 100) -> str:
    """Collapse to a single clean line: strip markdown, fold whitespace, trim.

    Titles and the index hook are written verbatim into YAML frontmatter and
    MEMORY.md, so an embedded newline or list marker corrupts both. This makes
    any string safe to embed.
    """
    s = _MD_NOISE_RE.sub(" ", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


@dataclass
class MemoryRecord:
    """A single durable memory."""

    title: str
    content: str
    type: str = "fact"
    description: str = ""  # one-line hook for the MEMORY.md index
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    confidence: float = 0.8
    provenance: str = "explicit_statement"
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    source: str = "engram"
    superseded_by: str | None = None
    validation_count: int = 0
    contradiction_detected: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    # The actual file this record was read from, if any. Set by the store on
    # load so the index can link to the real file on disk rather than a name
    # re-derived from the (mutable) title. Never serialized.
    source_path: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        t = (self.type or "fact").lower()
        if t not in VALID_TYPES and t not in LEGACY_TYPES:
            t = "fact"
        self.type = t
        if self.provenance not in VALID_PROVENANCE:
            self.provenance = "inferred"
        self.confidence = min(max(float(self.confidence), 0.0), 1.0)
        self.title = clean_line(self.title, 100) or "Untitled"
        if not self.description:
            # First sentence / line of content makes a decent index hook.
            first = re.split(r"(?<=[.!?])\s|\n", self.content.strip(), maxsplit=1)[0]
            self.description = clean_line(first, 160)
        else:
            self.description = clean_line(self.description, 160)

    # --- trust / decay (faithful to memanto core.py) -----------------------

    def compute_confidence(self) -> float:
        if self.contradiction_detected:
            return max(0.1, self.confidence * 0.3)
        if self.status == "superseded":
            return 0.0
        base = self.confidence * _PROVENANCE_WEIGHTS.get(self.provenance, 0.8)
        validation_boost = min(0.15, self.validation_count * 0.03)
        if self.type in ("preference", "observation"):
            age_days = (_now() - self.created_at).days
            age_penalty = 0.2 if age_days > 90 else 0.1 if age_days > 30 else 0.0
        else:
            age_penalty = 0.0
        return round(min(1.0, base + validation_boost - age_penalty), 2)

    def validate(self) -> None:
        self.validation_count += 1
        self.updated_at = _now()
        if self.provenance == "inferred":
            self.provenance = "validated"

    def mark_superseded(self, superseded_by_id: str) -> None:
        self.superseded_by = superseded_by_id
        self.status = "superseded"
        self.updated_at = _now()

    def trust_level(self) -> str:
        c = self.compute_confidence()
        if c >= 0.8 and not self.contradiction_detected:
            return "high"
        return "medium" if c >= 0.5 else "low"

    # --- serialization -----------------------------------------------------

    def filename(self) -> str:
        slug = slugify(self.title)
        # Degenerate titles (empty or "Untitled") all collapse to the same slug
        # and would clobber one another on disk; disambiguate with a short id so
        # two title-less memories never share a filename.
        if slug == "memory" or self.title == "Untitled":
            slug = f"{slug}_{self.id[:8]}"
        return f"{self.type}_{slug}.md"

    def to_markdown(self) -> str:
        tags = ", ".join(self.tags)
        fm = [
            "---",
            f"name: {self.title}",
            f"description: {self.description}",
            f"type: {self.type}",
            f"id: {self.id}",
            f"confidence: {self.confidence}",
            f"provenance: {self.provenance}",
            f"status: {self.status}",
            f"source: {self.source}",
            f"tags: {tags}",
            f"validation_count: {self.validation_count}",
            f"created_at: {_iso(self.created_at)}",
            f"updated_at: {_iso(self.updated_at)}",
        ]
        if self.superseded_by:
            fm.append(f"superseded_by: {self.superseded_by}")
        fm.append("---")
        return "\n".join(fm) + "\n\n" + self.content.strip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryRecord":
        meta, body = _split_frontmatter(text)
        tags_raw = meta.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        rec = cls(
            title=meta.get("name", "").strip() or "Untitled",
            content=body.strip(),
            type=meta.get("type", "fact").strip(),
            description=meta.get("description", "").strip(),
            id=meta.get("id", "").strip() or str(uuid.uuid4()),
            confidence=_safe_float(meta.get("confidence"), 0.8),
            provenance=meta.get("provenance", "imported").strip() or "imported",
            status=meta.get("status", "active").strip() or "active",
            tags=tags,
            source=meta.get("source", "imported").strip() or "imported",
            superseded_by=(meta.get("superseded_by") or "").strip() or None,
            validation_count=int(_safe_float(meta.get("validation_count"), 0)),
            created_at=_parse_dt(meta.get("created_at")),
            updated_at=_parse_dt(meta.get("updated_at")),
        )
        return rec


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse leading ``---`` frontmatter. Tolerant: no frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n")
    if parts[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    body_start = len(parts)
    for i in range(1, len(parts)):
        if parts[i].strip() == "---":
            body_start = i + 1
            break
        if ":" in parts[i]:
            k, _, v = parts[i].partition(":")
            meta[k.strip()] = v.strip()
    body = "\n".join(parts[body_start:])
    return meta, body
