"""Distill a session transcript into typed memories.

Leads with the local LLM (config endpoint); degrades to a conservative keyword
heuristic if the LLM yields nothing parseable, so a hook never silently
no-ops. Extraction prompts, parser and heuristic are lifted from memanto's
extractor.py (MIT); the LLM call is swapped to our OpenAI-compatible client and
a write gate + dedup are added.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config, llm, store
from .schema import VALID_TYPES, MemoryRecord

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
    "You are an engineering-memory distiller for a developer's coding agent. "
    "You read a summary of a finished coding session and extract only the "
    "DURABLE engineering signals worth remembering across future sessions: "
    "architectural decisions, hard rules/conventions, coding preferences, "
    "stable codebase facts, root-cause learnings, and explicit goals. "
    "Ignore ephemeral chatter, greetings, and one-off task details. "
    "Each item must stand alone without the surrounding conversation."
)

EXTRACTION_FOOTER = (
    "Respond with ONLY a JSON array (no prose, no code fences). Each element: "
    '{"type": <one of: decision, instruction, preference, fact, learning, '
    'error, goal, context>, "title": <<=80 chars>, "content": <one atomic '
    'self-contained statement>, "confidence": <0.0-1.0>}. '
    "Return [] if nothing durable was established."
)


def build_extraction_question(summary: str) -> str:
    summary = (summary or "").strip()[-_MAX_SUMMARY_CHARS:]
    return (
        "Extract the durable engineering memories from this session summary.\n\n"
        "=== SESSION SUMMARY ===\n"
        f"{summary}\n"
        "=== END SUMMARY ==="
    )


def distill(summary: str, source: str = "engram-distill") -> list[MemoryRecord]:
    """Return gated MemoryRecords distilled from a transcript summary."""
    summary = (summary or "").strip()
    if not summary:
        return []

    raw = _llm_extract(summary)
    if not raw:
        raw = heuristic_memories(summary)

    records: list[MemoryRecord] = []
    for item in raw:
        if not _passes_gate(item):
            continue
        records.append(
            MemoryRecord(
                title=item["title"],
                content=item["content"],
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
    )
    if not answer:
        return []
    return parse_llm_memories(answer)


_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def parse_llm_memories(answer_text: str) -> list[dict[str, Any]]:
    """Parse the LLM's JSON-array answer into validated memory dicts."""
    if not answer_text:
        return []
    cleaned = _FENCE_RE.sub("", answer_text).strip()
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


# --- heuristic fallback (lifted) ------------------------------------------- #

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
