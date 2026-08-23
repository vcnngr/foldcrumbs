"""Distill a session transcript into typed memories.

Leads with the local LLM (config endpoint); degrades to a conservative keyword
heuristic if the LLM yields nothing parseable, so a hook never silently
no-ops. The idea of distilling a finished session into typed memories is
inspired by memanto (MIT); the prompts, the OpenAI-compatible call, the write
gate and the dedup step are foldcrumbs's own.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import config, llm, redact, store
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


# Stricter subset for DELETION (auto-prune / prune): only structural artifacts
# that are never legitimate durable prose. Excludes the MEMORY.md/untitled.md and
# markdown-link clauses, which can appear in genuine memories (notably foldcrumbs's
# own architecture notes) — those are fine to skip at capture time but must not
# trigger deletion of an existing memory.
_HARD_ARTIFACT_RE = re.compile(
    r"```"                       # code fence
    r"|^\s*\|.*\|"               # markdown table row
    r"|\|\s*:?-{2,}"             # markdown table separator
    r"|[✓✅❌✗]"                  # status glyphs from tool/UI output
    r"|do not respond to these messages",  # local-command caveat boilerplate
    re.IGNORECASE | re.MULTILINE,
)


def _is_hard_artifact(text: str) -> bool:
    return bool(_HARD_ARTIFACT_RE.search(text or ""))


def build_extraction_question(summary: str) -> str:
    summary = (summary or "").strip()[-_MAX_SUMMARY_CHARS:]
    return (
        "Extract the durable engineering memories from this session summary.\n\n"
        "=== SESSION SUMMARY ===\n"
        f"{summary}\n"
        "=== END SUMMARY ==="
    )


def distill(summary: str, source: str = "foldcrumbs-distill") -> list[MemoryRecord]:
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
    fresh: list[MemoryRecord] = []
    for rec in records:
        action, _ = store.upsert(rec, cwd)
        if action == "created":
            created += 1
            fresh.append(rec)
        else:
            validated += 1
    # Contradiction pass: a *new* memory can make an older one obsolete (a
    # reversed decision, a completed goal). Dedup can't see this — it only
    # merges near-identical text — so ask the LLM about same-subject pairs.
    superseded = 0
    if fresh and config.auto_supersede_enabled():
        superseded = _auto_supersede(fresh, cwd)
    if records:
        store.rebuild_index(cwd)
    # Light auto-prune: clear any unambiguous artifact pollution that slipped in
    # (e.g. older memories created before the guard existed). Rebuilds if needed.
    if config.auto_prune_enabled():
        from . import audit  # lazy: audit imports distill
        pruned = audit.prune_artifacts(cwd)
        if pruned:
            config.log_event(f"auto-prune removed {len(pruned)} artifact(s): "
                             + ", ".join(pruned))
    return {"created": created, "validated": validated,
            "superseded": superseded, "total": len(records)}


_SUPERSEDE_PROMPT = (
    "You maintain a store of durable engineering memories. Given an OLD memory "
    "and a NEW one about the same subject, classify their relationship with ONE "
    "of three words:\n"
    '  "supersede" — the NEW memory makes the OLD one obsolete: it reverses the '
    "decision, states the deferred thing happened, or replaces the rule;\n"
    '  "coexist"   — both remain true (different aspects, or the new one adds '
    "detail);\n"
    '  "flag"      — they conflict but you cannot tell which one holds.\n'
    "Different aspects of the same subject do NOT supersede each other. When you "
    'are unsure between supersede and coexist, answer "flag" — never guess. '
    'Reply with JSON and nothing else: {"verdict": "supersede"|"coexist"|"flag"}'
)

_SUPERSEDE_TRUE_RE = re.compile(r'"supersedes"\s*:\s*true', re.IGNORECASE)
_SUPERSEDE_FALSE_RE = re.compile(r'"supersedes"\s*:\s*false', re.IGNORECASE)
_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(supersede|coexist|flag)"', re.IGNORECASE)


def parse_supersede_verdict(answer: str | None) -> str | None:
    """Classify a contradiction-pass answer: supersede / coexist / flag / None.

    ``None`` means the LLM did not answer at all — fail-soft, nothing changes
    (and nothing is flagged: an offline machine must not flood the queue).
    ``flag`` means an answer arrived that is neither a clear supersede nor a
    clear coexist — genuine uncertainty or confusion. That is exactly the case
    the old true/false question had no room for, and it must surface, not be
    guessed away. The old ``{"supersedes": ...}`` spelling is still honoured so
    a stale prompt or model keeps working.
    """
    if answer is None:
        return None
    if not answer.strip():
        return None               # an empty answer is still "no answer"
    if _SUPERSEDE_TRUE_RE.search(answer):
        return "supersede"
    if _SUPERSEDE_FALSE_RE.search(answer):
        return "coexist"
    m = _VERDICT_RE.search(answer)
    if m:
        return m.group(1).lower()
    return "flag"


def _auto_supersede(fresh: list[MemoryRecord], cwd: str | None = None) -> int:
    """Mark old memories obsoleted by freshly created ones. Returns the count.

    Conservative by construction: candidates come from a cheap same-subject
    pre-filter (store.find_conflict_candidates), only an explicit LLM
    ``supersede`` verdict flips anything, and with no LLM available nothing
    happens. Superseded files stay on disk — recoverable, cleared by prune.

    A third verdict exists now: ``flag``. When the LLM cannot tell which
    statement holds, the pair goes to the reconciliation queue (machine-local,
    visible via ``foldcrumbs conflicts``) instead of being guessed away — a
    system that silently picks a side when genuinely unsure is a system that
    will confidently act on the wrong fact eventually."""
    from . import conflicts as conflicts_mod

    count = 0
    for rec in fresh:
        for old in store.find_conflict_candidates(rec, cwd, federated=True):
            answer = llm.chat(
                messages=[
                    {"role": "system", "content": _SUPERSEDE_PROMPT},
                    {"role": "user", "content": (
                        f"OLD memory ({old.type}): {old.title}\n{old.content}\n\n"
                        f"NEW memory ({rec.type}): {rec.title}\n{rec.content}")},
                ],
                temperature=0.0,
                max_tokens=32,
            )
            verdict = parse_supersede_verdict(answer)
            if verdict is None:
                continue          # no LLM answer: fail-soft, nothing changes
            name = old.source_path or old.filename()
            if verdict == "flag":
                # Genuinely unsure — surface it, never guess. The queue entry
                # names both sides and drops out again once either one is
                # retired by other means (supersede, forget, expiry sweep).
                conflicts_mod.flag_pair(
                    name, rec.source_path or rec.filename(), cwd,
                    old_root=old.origin_root if old.is_foreign else None)
                config.log_event(
                    f"conflict flagged: {name} <-> {rec.filename()} "
                    f"(verdict unclear; see `foldcrumbs conflicts`)")
                continue
            if verdict == "supersede":
                if old.is_foreign:
                    # Someone else's store is read-only from here, so the
                    # contradiction is *recorded*, not applied: the assertion
                    # lives on our own new memory and is resolved in the
                    # federated view, where both sides are visible. Their
                    # instance stays the only one that can retire their file.
                    from foldcrumbs.schema import _clean_claim
                    claim = _clean_claim(f"{old.origin_root_id}:{name}")
                    if claim is None:
                        # Unserializable (a comma in the filename): recording it
                        # would produce a claim that vanishes on the next read.
                        config.log_event(
                            f"federation: cannot record a claim against {name}")
                        continue
                    if claim not in rec.supersedes_external:
                        # Append: one new memory can obsolete several foreign
                        # ones, and assignment would keep only the last.
                        rec.supersedes_external.append(claim)
                    store.write_memory(rec, cwd)
                    config.log_event(
                        f"auto-supersede (external): {old.origin_root}:{name} "
                        f"asserted obsolete by {rec.filename()}")
                else:
                    store.mark_superseded_on_disk(old, rec.id, cwd)
                    config.log_event(
                        f"auto-supersede: {name} obsoleted by {rec.filename()}")
                count += 1
            # verdict == "coexist": both hold, nothing to do.
    return count


# --- G2: model-proposed relations (opt-in; queue, never store) ------------- #

_G2_MAX_MEMORIES = 50   # D4: the model sees id + title for at most 50 memories

_G2_PROMPT = (
    "You are proposing TYPED relations between the memories of a coding "
    "assistant's store. You are given a list of memories (id + title) and a "
    "session summary. Propose only relations the summary genuinely supports — "
    "each must carry an exact evidence quote from the summary. Use ONLY these "
    "predicates: caused_by (A because of B), depends_on (A needs B), "
    "supersedes (A replaces B), contradicts (A and B cannot both hold), "
    "supports (A is evidence for B), refines (A sharpens B), blocks (A "
    "prevents B), precedes (A before B, temporal only — NOT causation). "
    "Only connect memory ids from the list. Do not invent ids. Prefer fewer, "
    "higher-confidence relations. If nothing is supported, reply [].\n"
    "Reply with a JSON array and nothing else — no commentary, no code "
    "fences. Each entry: {\"subject_id\": id, \"predicate\": one of the "
    "predicates, \"target_id\": id, \"evidence\": exact quote from the "
    "summary, \"confidence\": 0.0-1.0}."
)


def build_g2_question(memories: list, summary: str) -> str:
    from . import redact
    lines = "\n".join(
        f"- id={m.id} :: {m.title}" for m in memories[:_G2_MAX_MEMORIES])
    return (
        "Memories in the store:\n"
        f"{lines}\n\n"
        "=== SESSION SUMMARY ===\n"
        f"{redact.scrub((summary or '').strip())[-_MAX_SUMMARY_CHARS:]}\n"
        "=== END SUMMARY ==="
    )


def parse_g2_relations(answer: str | None) -> list[dict]:
    """Tolerant parse of the model's G2 answer. Invalid entries are dropped
    (D4): unknown predicates, non-string ids, missing fields. This is the
    safety net — the queue re-validates against the live store on submit."""
    if not answer:
        return []
    cleaned = _FENCE_RE.sub("", answer).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        # structured-output object form: {"relations": [...]}
        obj_start, obj_end = cleaned.find("{"), cleaned.rfind("}")
        if obj_start == -1 or obj_end <= obj_start:
            return []
        try:
            obj = json.loads(cleaned[obj_start:obj_end + 1])
        except (json.JSONDecodeError, ValueError):
            return []
        raw = obj.get("relations") if isinstance(obj, dict) else None
        if not isinstance(raw, list):
            return []
    else:
        try:
            raw = json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    from . import relations as _rel
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        predicate = str(item.get("predicate") or "").strip()
        subject = str(item.get("subject_id") or "").strip()
        target = str(item.get("target_id") or "").strip()
        if predicate not in _rel.PREDICATES:
            continue
        if not subject or not target or subject == target:
            continue
        evidence = str(item.get("evidence") or "").strip()
        if not evidence:
            continue          # no evidence quote = not a supported claim
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        out.append({
            "subject_id": subject, "predicate": predicate,
            "target_id": target, "evidence": evidence,
            "confidence": confidence,
        })
    return out


def _extract_relations(summary: str, cwd: str | None = None) -> int:
    """Ask the model for relation proposals and enqueue them. Returns the
    number WRITTEN (not proposed) — dedup/caps are applied by the queue.
    No LLM available → 0, silently (fail-soft, like the supersede pass)."""
    from . import proposals as proposals_mod
    mems = [m for m in store.load_all(cwd)
            if m.status == "active" and not m.is_expired]
    if not mems:
        return 0
    answer = llm.chat(
        messages=[
            {"role": "system", "content": _G2_PROMPT},
            {"role": "user", "content": build_g2_question(mems, summary)},
        ],
        temperature=0.0,
        max_tokens=1024,
    )
    if not answer:
        return 0
    candidates = parse_g2_relations(answer)
    if not candidates:
        return 0
    stats = proposals_mod.submit(candidates, prov="inferred", cwd=cwd)
    written = stats["written"]
    if written or stats["capped"]:
        config.log_event(
            f"G2: {written} relation proposal(s) enqueued "
            f"(invalid={stats['invalid']}, dup_store={stats['dup_store']}, "
            f"dup_queue={stats['dup_queue']}, capped={stats['capped']})")
    return written


def distill_and_store(
    summary: str, cwd: str | None = None, source: str = "foldcrumbs-distill"
) -> dict[str, int]:
    counts = persist(distill(summary, source=source), cwd)
    # G2 (design g2-extraction.md): with the memories now present, ask the
    # model to PROPOSE relations between them. Opt-in (FOLDCRUMBS_G2=1);
    # proposals land in the queue as pending — never in the store directly.
    # Failures are silent and logged: a dead LLM must not break distill.
    if config.g2_enabled():
        try:
            counts["relations_proposed"] = _extract_relations(summary, cwd)
        except Exception as exc:  # noqa: BLE001 — distill must never die here
            counts["relations_proposed"] = 0
            config.log_event(f"G2 extraction failed (distill unaffected): {exc}")
    return counts


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
    stamp = "<!-- foldcrumbs handoff -->\n# Resume point\n\n"
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


# --- heuristic fallback (keyword classifier, foldcrumbs) ---------------------- #

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
