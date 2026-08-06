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


def _parse_dt_opt(value: str | None) -> datetime | None:
    """Parse a stored timestamp to aware UTC, or None if it isn't one.

    A hand-written or imported record can carry a naive timestamp. Mixing
    naive and aware datetimes raises ``TypeError`` the moment two of them are
    compared — which is what the index sort does — so a naive value is read as
    UTC rather than left to blow up a rebuild later.

    None is kept distinct from "now" so callers can tell a timestamp that was
    *read* from one that was *invented*: an invented one differs on every
    parse, and anything ordering across machines must not depend on it.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_dt(value: str | None) -> datetime:
    return _parse_dt_opt(value) or _now()


# Claims share one comma-separated frontmatter line, so the filename half
# cannot contain a comma — nor a line break, which POSIX allows in a filename
# and which would split the field across lines. Either way the claim would be
# read back as fragments matching nothing and the supersession would vanish.
# Excluded here so such a claim is never written in the first place.
_CLAIM_RE = re.compile(r"^[0-9a-f]{16}:[^/\\,\r\n]+\.md$")


def _clean_claim(raw: str) -> str | None:
    """Validate one "<root id>:<filename>" supersession claim.

    Both halves are used as lookup keys against the federated view, and the
    filename half is displayed. Anything that is not a real root id plus a
    plain filename is dropped rather than carried around as a claim that can
    never match — or, worse, one containing a path.
    """
    claim = (raw or "").strip()
    return claim if _CLAIM_RE.match(claim) else None


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
    source: str = "foldcrumbs"
    superseded_by: str | None = None
    validation_count: int = 0
    contradiction_detected: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    # The actual file this record was read from, if any. Set by the store on
    # load so the index can link to the real file on disk rather than a name
    # re-derived from the (mutable) title. Never serialized.
    source_path: str | None = field(default=None, compare=False)
    # True when the file carried no created_at and one was invented at parse
    # time. Callers that need a reproducible order must not trust it. Never
    # serialized.
    created_at_missing: bool = field(default=False, compare=False)
    # Set when the record was read from *another instance's* store. Marks it
    # read-only for every write path, and lets recall say whose it is. Never
    # serialized: it describes where a record was found, not what it says.
    origin_root: str | None = field(default=None, compare=False)
    origin_root_id: str | None = field(default=None, compare=False)
    origin_path: str | None = field(default=None, compare=False)
    # Claims of the form "<root id>:<file>", each naming a memory in another
    # instance's store that this one obsoletes. Recorded here because that
    # store is read-only from this side: the assertion is ours to make, the
    # edit is not ours to write. Resolved only in the federated view, where
    # both sides are visible at once.
    #
    # A list, because one memory can obsolete several — a single field would
    # silently drop every claim after the first. Keyed on the root *id*, not
    # its label: labels come from directory names, and two instances can
    # easily share one ("~/a/.claude" and "~/b/.claude" are both "claude"),
    # which would attribute the claim to whichever was rendered.
    supersedes_external: list[str] = field(default_factory=list)

    # Appended, never inserted. Every field above shipped in 0.6.0 in this
    # order, and a caller may pass them positionally; slipping a new one in
    # between would bind their arguments to the wrong fields — silently, since
    # the types happen to be compatible. New fields go at the end, always.
    #
    # Title of the local memory that declares this foreign one obsolete, when
    # one does. Set on load, never serialized: the claim lives on our record,
    # this is only how it reaches the reader of theirs.
    contested_by: str | None = field(default=None, compare=False)

    # Whether this record's id was minted just now instead of read from the
    # file. A memory written before ids were serialized gets a fresh uuid on
    # every load, so anything keyed on the id would key on a different value
    # each time — recorded here so those callers can tell rather than guess.
    id_missing: bool = field(default=False, compare=False)

    # Same for the timestamps: both default to "now" when the file does not
    # carry them, so a caller reasoning about age cannot tell a memory touched
    # a moment ago from one whose date was simply never written down.
    updated_at_missing: bool = field(default=False, compare=False)

    # "True until <date>" — the EXPIRING class. A memory whose statement stops
    # being true at a known point (a deadline, a trial that ends, a deferral
    # with a date on it) carries that point here. Past it, the memory is
    # invisible everywhere an archived one is — index, recall, federation, dedup,
    # contradiction pass — but the file is untouched: expiry is a visibility
    # decision, `decay` is the sweep that archives, and removing the line (or
    # moving it forward) is how the user says it still holds. Only ever set by
    # explicit user intent (CLI `--expires`): a distiller guessing a date
    # would be a silent timer on someone else's memory.
    expires_at: datetime | None = None

    @property
    def is_foreign(self) -> bool:
        return self.origin_root is not None

    @property
    def is_expired(self) -> bool:
        """Past its ``expires_at``, if it has one.

        Evaluated live (not at parse time) so a store crossing midnight while a
        session runs notices on the next read instead of the next restart. A
        missing or unparseable date never expires: unknown is not evidence of
        age, same rule decay applies.
        """
        if self.expires_at is None:
            return False
        return _now() >= self.expires_at

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
        if self.supersedes_external:
            fm.append("supersedes_external: "
                      + ", ".join(self.supersedes_external))
        if self.expires_at is not None:
            fm.append(f"expires_at: {_iso(self.expires_at)}")
        fm.append("---")
        return "\n".join(fm) + "\n\n" + self.content.strip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryRecord":
        meta, body = _split_frontmatter(text)
        tags_raw = meta.get("tags", "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        stored_id = meta.get("id", "").strip()
        rec = cls(
            title=meta.get("name", "").strip() or "Untitled",
            content=body.strip(),
            type=meta.get("type", "fact").strip(),
            description=meta.get("description", "").strip(),
            id=stored_id or str(uuid.uuid4()),
            id_missing=not stored_id,
            confidence=_safe_float(meta.get("confidence"), 0.8),
            provenance=meta.get("provenance", "imported").strip() or "imported",
            status=meta.get("status", "active").strip() or "active",
            tags=tags,
            source=meta.get("source", "imported").strip() or "imported",
            superseded_by=(meta.get("superseded_by") or "").strip() or None,
            supersedes_external=[
                c for c in (
                    _clean_claim(raw)
                    for raw in (meta.get("supersedes_external") or "").split(",")
                ) if c
            ],
            validation_count=int(_safe_float(meta.get("validation_count"), 0)),
            created_at=_parse_dt(meta.get("created_at")),
            updated_at=_parse_dt(meta.get("updated_at")),
            # Optional and tolerant: an unparseable date degrades to "no
            # expiry", never to an error — a hand-edited line must not take a
            # memory out of service.
            expires_at=_parse_dt_opt(meta.get("expires_at")),
        )
        # Remember that the timestamp was invented rather than read — which
        # covers a field that is absent *and* one that is present but
        # unparseable, since both end up as a fresh "now" on every parse.
        # Anything needing a stable order (the federated index, fed by several
        # machines) has to substitute something deterministic instead.
        rec.created_at_missing = _parse_dt_opt(meta.get("created_at")) is None
        rec.updated_at_missing = _parse_dt_opt(meta.get("updated_at")) is None
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
