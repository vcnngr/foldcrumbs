"""Render recalled memories as an injectable context block.

Adapted from memanto's ``profile.py`` (MIT) to work on MemoryRecord objects.
Used by the CLI ``recall`` command; the SessionStart hook injects the curated
MEMORY.md index directly instead (cheaper, already grouped).
"""

from __future__ import annotations

import html

from .schema import MemoryRecord

_TYPE_ORDER = [
    "instruction",
    "decision",
    "commitment",
    "preference",
    "error",
    "learning",
    "fact",
    "observation",
    "relationship",
    "artifact",
    "event",
    "context",
    "goal",
]

_TYPE_LABEL = {
    "instruction": "Rules (always honour)",
    "decision": "Decisions made",
    "commitment": "Commitments",
    "preference": "Preferences",
    "error": "Known failure modes",
    "learning": "Lessons learned",
    "fact": "Facts",
    "observation": "Observed patterns",
    "relationship": "Relationships",
    "artifact": "Artifacts",
    "event": "Events",
    "context": "Background",
    "goal": "Goals",
}


def format_context_block(
    memories: list[MemoryRecord], heading: str | None = None
) -> str:
    """Render memories as a deterministic Markdown block, grouped by type."""
    if not memories:
        return ""

    grouped: dict[str, list[MemoryRecord]] = {}
    for m in memories:
        grouped.setdefault(m.type, []).append(m)

    safe = html.escape(heading, quote=True) if heading else ""
    lines = [
        "<foldcrumbs-recall>",
        f"Relevant memory{f' for {safe}' if safe else ''} "
        "(carried over from previous sessions — honour it, do not re-ask):",
    ]
    ordered = [t for t in _TYPE_ORDER if t in grouped]
    ordered += [t for t in grouped if t not in _TYPE_ORDER]
    for mtype in ordered:
        label = _TYPE_LABEL.get(mtype, mtype.capitalize())
        lines.append(f"\n{label}:")
        for m in grouped[mtype]:
            text = m.content.strip()
            if m.compute_confidence() < 0.6:
                text += " (tentative)"
            lines.append(f"  - {text}")
    lines.append("</foldcrumbs-recall>")
    return "\n".join(lines)
