"""Distill a session transcript into typed memories.

Leads with the local LLM (config endpoint); degrades to a conservative keyword
heuristic if the LLM yields nothing parseable, so a hook never silently
no-ops. The idea of distilling a finished session into typed memories is
inspired by memanto (MIT); the prompts, the OpenAI-compatible call, the write
gate and the dedup step are engram's own.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config, llm, redact, store
from .schema import VALID_TYPES, MemoryRecord, clean_line

_MAX_SUMMARY_CHARS = 6000

# Types worth keeping as durable engineering memory (the write gate).
_GATE_TYPES = {
    "decision",
    "instruction",
    "preference",
    "fact",
    "error",
    "goal",
    "learning",
}

EXTRACTION_HEADER = (
    "Act as a memory curator for a software developer's coding assistant. "
    "Given notes from a finished coding session, pull out only the facts that "
    "stay true beyond this session and are worth recalling next time: choices "
    "of architecture or tooling, firm rules and conventions, the developer's "
    "stated preferences, durable facts about the codebase, lessons from "
    "diagnosing a problem, and clearly stated objectives. "
    "Skip small talk, pleasantries, and details specific to a single task. "
    "Ignore any discussion ABOUT the assistant, the memory system, these "
    "instructions, or documentation being written in this session — capture "
    "facts about the developer's own project and choices, never the tooling's "
    "own design notes. "
    "Write each item so it makes sense on its own, with no surrounding context."
)

EXTRACTION_FOOTER = (
    "Reply with a JSON array and nothing else — no commentary, no code fences. "
    'Each entry is an object: {"type": one of [decision, instruction, '
    "preference, fact, learning, error, goal, context], "
    '"title": <=80 characters, "content": a single standalone sentence, '
    '"confidence": a number from 0.0 to 1.0}. '
    "If the session established nothing durable, reply with []."
)


# OpenAI structured-output schema (best-effort; tolerant parser is the safety net).
MEMORY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["decision", "instruction", "preference", "fact",
                                 "learning", "error", "goal", "context"],
                    },
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["type", "title", "content", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}


# A candidate that looks like rendered tooling/UI output — a markdown table, a
# link, status glyphs, a reference to a memory/index file, or the local-command
# caveat — is an artifact of the session, never a durable fact about the
# developer's project. Drop it regardless of project (the LLM prompt already
# discourages self-talk; this is the deterministic backstop that also covers the
# keyword-heuristic fallback, which has no such instruction).
_ARTIFACT_RE = re.compile(
    r"```"                       # code fence
    r"|^\s*\|.*\|"               # markdown table row
    r"|\|\s*:?-{2,}"             # markdown table separator
    r"|\]\([^)]+\)"              # markdown link
    r"|[✓✅❌✗]"                  # status glyphs from tool/UI output
    r"|do not respond to these messages"   # local-command caveat boilerplate
    r"|MEMORY\.md|\buntitled\.md",         # references to the memory store itself
    re.IGNORECASE | re.MULTILINE,
)


def _is_artifact(text: str) -> bool:
    return bool(_ARTIFACT_RE.search(text or ""))


def build_extraction_question(summary: str) -> str:
    summary = (summary or "").strip()[-_MAX_SUMMARY_CHARS:]
    return (
        "Extract the durable engineering memories from this session summary.\n\n"
        "=== SESSION SUMMARY ===\n"
        f"{summary}\n"
        "=== END SUMMARY ==="
    )


def distill(summary: str, source: str = "engram-distill") -> list[MemoryRecord]:
    """Return gated MemoryRecords distilled from a transcript summary.

    Secrets are scrubbed up front (before the LLM ever sees the text) and again
    on each memory's title/content before it becomes a record — defense in
    depth, so a credential is never sent out or written to disk.
    """
    summary = redact.scrub((summary or "").strip())
    if not summary:
        return []

    raw = _llm_extract(summary)
    if not raw:
        raw = heuristic_memories(summary)

    records: list[MemoryRecord] = []
    for item in raw:
        if not _passes_gate(item):
            continue
        if _is_artifact(item.get("title", "")) or _is_artifact(item.get("content", "")):
            continue
        records.append(
            MemoryRecord(
                title=redact.scrub(item["title"]),
                content=redact.scrub(item["content"]),
                type=item["type"],
                confidence=item["confidence"],
                provenance="inferred",
                source=source,
                tags=item.get("tags", []),
            )
        )
    return records


def persist(records: list[MemoryRecord], cwd: str | None = None) -> dict[str, int]:
    """Upsert records (dedup-aware) and rebuild the index. Returns counts."""
    created = validated = 0
    for rec in records:
        action, _ = store.upsert(rec, cwd)
        if action == "created":
            created += 1
        else:
            validated += 1
    if records:
        store.rebuild_index(cwd)
    return {"created": created, "validated": validated, "total": len(records)}


def distill_and_store(
    summary: str, cwd: str | None = None, source: str = "engram-distill"
) -> dict[str, int]:
    return persist(distill(summary, source=source), cwd)


_HANDOFF_HEADER = (
    "You write a short handoff note so a coding session can be resumed after a "
    "context reset. From the session notes, capture only the LIVE working state: "
    "the task currently in progress, files being edited, decisions just taken, "
    "and the immediate next steps. Be concise and concrete; address the reader "
    "as 'You'. Use Markdown bullet points. Omit anything already finished."
)


def make_handoff(summary: str) -> str | None:
    """Produce a Markdown working-state handoff from a transcript summary.

    Uses the LLM; on failure falls back to the scrubbed transcript tail so a
    /clear still leaves *something* to resume from. Returns None if empty.
    """
    summary = redact.scrub((summary or "").strip())
    if not summary:
        return None
    text = llm.chat(
        messages=[
            {"role": "system", "content": _HANDOFF_HEADER},
            {"role": "user", "content": f"Session notes:\n{summary[-_MAX_SUMMARY_CHARS:]}"},
        ],
        temperature=0.2,
        max_tokens=512,
    )
    if text and text.strip():
        body = text.strip()
    else:
        # Fallback: last slice of the conversation, lightly framed.
        body = "_(LLM unavailable — raw tail)_\n\n" + summary[-1500:]
    stamp = "<!-- engram handoff -->\n# Resume point\n\n"
    return redact.scrub(stamp + body)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _passes_gate(item: dict[str, Any]) -> bool:
    return (
        item.get("type") in _GATE_TYPES
        and float(item.get("confidence", 0)) >= config.MIN_CONFIDENCE
        and bool(item.get("content"))
    )


def _llm_extract(summary: str) -> list[dict[str, Any]]:
    answer = llm.chat(
        messages=[
            {"role": "system", "content": EXTRACTION_HEADER},
            {"role": "user", "content": build_extraction_question(summary)},
            {"role": "user", "content": EXTRACTION_FOOTER},
        ],
        temperature=0.0,
        json_schema=MEMORY_JSON_SCHEMA if config.LLM_JSON_SCHEMA else None,
    )
    if not answer:
        return []
    return parse_llm_memories(answer)


_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def parse_llm_memories(answer_text: str) -> list[dict[str, Any]]:
    """Parse the LLM answer into validated memory dicts.

    Tolerant of: bare JSON array, an object ``{"memories": [...]}`` (structured
    output), code fences, and leading/trailing prose. Invalid items are dropped.
    """
    if not answer_text:
        return []
    cleaned = _FENCE_RE.sub("", answer_text).strip()

    # Structured-output object form: {"memories": [...]}.
    obj_start, obj_end = cleaned.find("{"), cleaned.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        try:
            obj = json.loads(cleaned[obj_start : obj_end + 1])
            if isinstance(obj, dict) and isinstance(obj.get("memories"), list):
                return [m for m in map(_coerce_memory, obj["memories"]) if m]
        except (json.JSONDecodeError, ValueError):
            pass

    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        raw = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        mem = _coerce_memory(item)
        if mem is not None:
            out.append(mem)
    return out


def _coerce_memory(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    content = str(item.get("content") or "").strip()
    if not content:
        return None
    mtype = str(item.get("type") or "learning").strip().lower()
    if mtype not in VALID_TYPES:
        mtype = "learning"
    title = str(item.get("title") or content).strip()[:80]
    try:
        confidence = float(item.get("confidence", 0.85))
    except (TypeError, ValueError):
        confidence = 0.85
    return {
        "type": mtype,
        "title": title,
        "content": content[:10000],
        "confidence": min(max(confidence, 0.0), 1.0),
    }


# --- heuristic fallback (keyword classifier, engram) ---------------------- #

_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?:user|assistant|human|claude|system)\s*:\s*", re.IGNORECASE
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+")

_HEURISTIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("instruction", ("always", "never", "must ", "should ", "do not", "don't",
                      "enforce", "convention")),
    ("decision", ("decided", "chose", "will use", "going with", "we use",
                  "picked", "selected", "switched to")),
    ("preference", ("prefer", "favour", "favor", "instead of", "rather than",
                    "like to")),
    ("error", ("bug", "root cause", "regression", "failed because", "broke")),
    ("goal", ("goal is", "aim to", "objective", "we want to")),
]


def _classify(sentence: str) -> str | None:
    lower = sentence.lower()
    for mtype, keywords in _HEURISTIC_RULES:
        if any(kw in lower for kw in keywords):
            return mtype
    return None


def heuristic_memories(summary: str) -> list[dict[str, Any]]:
    summary = (summary or "").strip()
    if not summary:
        return []
    normalized = re.sub(r"^\s*[-*•]\s*", "", summary, flags=re.MULTILINE)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(normalized):
        s = _ROLE_PREFIX_RE.sub("", sentence).strip()
        if len(s) < 12:
            continue
        if _is_artifact(s):
            continue
        mtype = _classify(s)
        if mtype is None:
            continue
        key = s.lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        # 0.7 == gate floor: heuristic memories persist but provenance=inferred
        # drops their *effective* confidence (compute_confidence) into tentative.
        out.append({"type": mtype, "title": s[:80], "content": s, "confidence": 0.7})
        if len(out) >= 12:
            break
    return out
