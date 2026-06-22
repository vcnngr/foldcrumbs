"""Merge-safe installer for engram's Claude Code hooks.

Adds engram hooks to a settings.json WITHOUT disturbing existing hooks
(GSD, graphify, sound, etc.): for each event we append our own matcher group,
and skip if our command is already registered (idempotent). Always backs up
the settings file first.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent / "hooks"

# event -> (script filename, matcher). matcher "" means "all".
_HOOKS = {
    "SessionStart": ("session_start.py", ""),
    "PostCompact": ("post_compact.py", ""),
    "SessionEnd": ("session_end.py", ""),
    "PostToolUse": ("context_monitor.py", "Bash|Edit|Write|MultiEdit|Agent|Task"),
}

_MARKER = "engram/hooks/"  # any command containing this path is ours


def _command_for(script: str) -> str:
    py = sys.executable or "python3"
    return f'"{py}" "{HOOKS_DIR / script}"'


def _already_present(groups: list, script: str) -> bool:
    needle = f"{_MARKER}{script}"
    for group in groups:
        for h in group.get("hooks", []):
            if needle in h.get("command", ""):
                return True
    return False


def default_settings_path(global_scope: bool = True) -> Path:
    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def install(settings_path: Path, timeout: int = 15) -> list[str]:
    """Merge engram hooks into ``settings_path``. Returns list of changes."""
    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
        shutil.copy2(settings_path, settings_path.with_suffix(".json.engram-bak"))

    hooks = settings.setdefault("hooks", {})
    changes: list[str] = []

    for event, (script, matcher) in _HOOKS.items():
        groups = hooks.setdefault(event, [])
        if _already_present(groups, script):
            continue
        entry = {"type": "command", "command": _command_for(script), "timeout": timeout}
        group: dict = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        changes.append(f"{event} -> {script}")

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return changes


def uninstall(settings_path: Path) -> list[str]:
    """Remove engram hook groups. Returns list of removed events."""
    settings_path = Path(settings_path)
    if not settings_path.exists():
        return []
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    hooks = settings.get("hooks", {})
    removed: list[str] = []
    for event in list(hooks.keys()):
        kept = []
        for group in hooks[event]:
            ours = any(_MARKER in h.get("command", "") for h in group.get("hooks", []))
            if ours:
                removed.append(event)
            else:
                kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return removed
