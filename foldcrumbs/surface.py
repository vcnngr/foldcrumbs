"""Active surface: slash commands that make memory an in-agent capability.

The hooks are foldcrumbs's passive layer (inject at start, distill in the
background); this module writes the *active* layer — user-invocable commands
(`/remember`, `/recall`, `/forget`, `/memory`) that teach the agent to use the
memory store deliberately. For Claude Code they are markdown files in
``<config-dir>/commands/``; the same bodies are portable to Codex prompt files.

Managed-file contract: every file we write carries a marker line. On install we
overwrite only files that are missing or still carry the marker — a file the
user edited (marker removed) is theirs and is skipped. Uninstall removes only
marked files. This keeps install/uninstall idempotent and merge-safe, like the
hook installer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

MARKER = "managed-by: foldcrumbs"
_MARKER_LINE = (
    f"<!-- {MARKER} — `foldcrumbs install` overwrites this file; "
    "remove this line to take ownership -->"
)

# The CLI invocation commands should use. The console script is on PATH for a
# normal pip install; the module form is the fallback for odd environments.
_CLI_NOTE = (
    "Run foldcrumbs from the project root so it targets this project's store "
    "(use `python3 -m foldcrumbs` if `foldcrumbs` is not on PATH)."
)

_ALLOWED = "Bash(foldcrumbs:*), Bash(python3 -m foldcrumbs:*)"


def _cmd(description: str, argument_hint: str, body: str) -> str:
    # json.dumps produces a double-quoted scalar that is also valid YAML —
    # descriptions containing ": " would otherwise break the frontmatter.
    fm = [
        "---",
        f"description: {json.dumps(description)}",
    ]
    if argument_hint:
        fm.append(f"argument-hint: {json.dumps(argument_hint)}")
    fm += [f"allowed-tools: {_ALLOWED}", "---"]
    return "\n".join(fm) + "\n" + _MARKER_LINE + "\n\n" + body.strip() + "\n"


# Shared bodies: (description, argument-hint, body). The same text serves
# Claude Code commands (with frontmatter) and Codex prompt files (plain
# markdown) — both substitute $ARGUMENTS.
_BODIES: dict[str, tuple[str, str, str]] = {
    "remember": (
        "Store a durable memory (no arguments: distill from this conversation)",
        "[text to remember]",
        f"""
Store durable project memory with foldcrumbs. {_CLI_NOTE}

Input: $ARGUMENTS

If input text was given: store it with
`foldcrumbs remember "<text>" --type <type> --title "<short title>"`,
inferring the best type (decision | instruction | preference | fact | error |
goal | learning) and a concise title from the text. Report the created file.

If NO input was given: review this conversation and extract the durable facts —
decisions taken, rules or conventions stated, preferences expressed, lessons
learned from debugging. Ignore anything about the memory tooling itself. List
each candidate with its proposed type and ask the user to confirm; store each
confirmed item with `foldcrumbs remember`, then report what was saved.
""",
    ),
    "recall": (
        "Search project memory and apply it to the current task",
        "<query> [--type t] [--tag t]",
        f"""
Recall from this project's foldcrumbs memory. {_CLI_NOTE}

Query: $ARGUMENTS

If a query was given: run `foldcrumbs recall "<query>"`, forwarding any
`--type`/`--tag` filters the user included. Read the results and honour them in
the current task — do not re-ask what is already recorded.

If NO query was given: read `MEMORY.md` in the project memory directory (shown
by `foldcrumbs status`) and give the user a short overview of what the store
knows, grouped by type.
""",
    ),
    "forget": (
        "Forget a memory that is wrong or revoked",
        "<memory filename or search words>",
        f"""
Forget a memory in this project's foldcrumbs store. {_CLI_NOTE}

Target: $ARGUMENTS

Run `foldcrumbs forget "<target>"` first — it is a dry-run. If it lists
candidate filenames (the target was not an exact filename), show them to the
user and ask which one to forget. Once the target is confirmed, run
`foldcrumbs forget <filename> --apply`. Use `--hard` only if the user
explicitly asks to delete the file outright; otherwise the soft delete keeps it
on disk for audit (a later `foldcrumbs prune --apply` clears it).
""",
    ),
    "memory": (
        "Project memory dashboard: status, health, resume point",
        "",
        f"""
Show the state of this project's foldcrumbs memory. {_CLI_NOTE}

Run `foldcrumbs status`, `foldcrumbs doctor` and `foldcrumbs handoff`, then
summarize for the user:

- store size and LLM backend (from status)
- health issues, if any: dead links, orphans, pollution, low-trust memories
  (from doctor) — with the suggested fix for each (`foldcrumbs index`,
  `foldcrumbs prune`, review of tentative memories)
- the current resume point (from handoff), if one exists

Keep it short; propose concrete next actions only when doctor found something.
""",
    ),
}

# Claude Code slash commands: frontmatter + marker + body.
COMMANDS: dict[str, str] = {
    f"{name}.md": _cmd(desc, hint, body)
    for name, (desc, hint, body) in _BODIES.items()
}

# Codex custom prompts: plain markdown, no Claude-specific frontmatter. Codex
# substitutes $ARGUMENTS the same way, but namespaces prompt files — a file in
# ~/.codex/prompts/<name>.md is invoked as `/prompts:<name>`, not `/<name>`.
CODEX_PROMPTS: dict[str, str] = {
    f"{name}.md": f"# /prompts:{name} — {desc}\n{_MARKER_LINE}\n\n{body.strip()}\n"
    for name, (desc, _hint, body) in _BODIES.items()
}


SKILL = (
    """---
name: foldcrumbs
description: >-
  Persistent project memory. Use when the user asks to remember something
  ("remember that...", "ricorda che...", "don't forget..."), asks what was
  decided or why ("what did we decide about...", "cosa avevamo deciso...",
  "why did we choose..."), corrects a remembered fact ("that's no longer
  true", "non è più così"), or at the start of a substantive task in a
  project with a foldcrumbs store.
---
"""
    + _MARKER_LINE
    + f"""

# foldcrumbs — project memory

This project keeps durable memory in a foldcrumbs store. {_CLI_NOTE}

## When to recall

At the start of a substantive task, and whenever the user asks what was
decided or why: run `foldcrumbs recall "<topic>"` (filters: `--type`,
`--tag`). Honour what comes back — do not re-ask or contradict recorded
decisions without flagging it. `MEMORY.md` in the memory dir (path shown by
`foldcrumbs status`) is the index; grep the folder for detail.

## When to remember

When the user states a durable decision, rule, preference, or lesson —
explicitly ("remember that we deploy on Fridays") or in passing ("we're
switching to Postgres, by the way") — store it:

    foldcrumbs remember "<one self-contained sentence>" --type <t> --title "<short>"

Types: decision | instruction | preference | fact | error | goal | learning.
For in-passing statements, confirm with the user before storing. Never store
session-specific details, secrets, or notes about this memory tooling itself.

## When to forget or supersede

If the user corrects a stored fact ("that's outdated", "we reverted that"):

- wrong/revoked → `foldcrumbs forget <filename> --apply` (dry-run first;
  a query lists candidate filenames)
- replaced by a new memory → store the new one, then
  `foldcrumbs supersede <old-filename> --by <new-filename>`

Soft-deleted and superseded files stay on disk until `foldcrumbs prune --apply`.
"""
)


def commands_dir(global_scope: bool = True) -> Path:
    """Claude Code commands dir: <config-dir>/commands (CLAUDE_CONFIG_DIR-aware)
    or ./.claude/commands for project scope."""
    if global_scope:
        return config.claude_config_dir() / "commands"
    return Path.cwd() / ".claude" / "commands"


def is_managed(path: Path) -> bool:
    """True if the file was written by us and never taken over by the user."""
    try:
        return MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def install_commands(target_dir: Path | None = None) -> dict[str, str]:
    """Write the slash-command files. Returns {filename: action}.

    action ∈ {"created", "refreshed", "unchanged", "skipped (user file)"}.
    A file that exists without our marker belongs to the user — never touched.
    """
    d = Path(target_dir) if target_dir else commands_dir()
    return _write_managed(d, COMMANDS)


def skill_dir(global_scope: bool = True) -> Path:
    """Claude Code skill dir for foldcrumbs: <config-dir>/skills/foldcrumbs
    (CLAUDE_CONFIG_DIR-aware) or ./.claude/skills/foldcrumbs for project scope."""
    if global_scope:
        return config.claude_config_dir() / "skills" / "foldcrumbs"
    return Path.cwd() / ".claude" / "skills" / "foldcrumbs"


def install_skill(target_dir: Path | None = None) -> str:
    """Write SKILL.md under the foldcrumbs skill dir. Returns the action taken
    (same contract as install_commands)."""
    d = Path(target_dir) if target_dir else skill_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "SKILL.md"
    if not path.exists():
        path.write_text(SKILL, encoding="utf-8")
        return "created"
    if not is_managed(path):
        return "skipped (user file)"
    if path.read_text(encoding="utf-8") == SKILL:
        return "unchanged"
    path.write_text(SKILL, encoding="utf-8")
    return "refreshed"


def uninstall_skill(target_dir: Path | None = None) -> bool:
    """Remove our managed SKILL.md (and the dir if it ends up empty)."""
    d = Path(target_dir) if target_dir else skill_dir()
    path = d / "SKILL.md"
    if not (path.exists() and is_managed(path)):
        return False
    try:
        path.unlink()
        if not any(d.iterdir()):
            d.rmdir()
    except OSError:
        return False
    return True


def uninstall_commands(target_dir: Path | None = None) -> list[str]:
    """Remove our managed command files (user-owned files are left alone)."""
    d = Path(target_dir) if target_dir else commands_dir()
    return _remove_managed(d, COMMANDS)


def _write_managed(d: Path, files: dict[str, str]) -> dict[str, str]:
    """Write a set of managed files with the standard contract."""
    d.mkdir(parents=True, exist_ok=True)
    actions: dict[str, str] = {}
    for name, content in files.items():
        path = d / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            actions[name] = "created"
        elif not is_managed(path):
            actions[name] = "skipped (user file)"
        elif path.read_text(encoding="utf-8") == content:
            actions[name] = "unchanged"
        else:
            path.write_text(content, encoding="utf-8")
            actions[name] = "refreshed"
    return actions


def _remove_managed(d: Path, files: dict[str, str]) -> list[str]:
    removed: list[str] = []
    for name in files:
        path = d / name
        if path.exists() and is_managed(path):
            try:
                path.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


# --------------------------------------------------------------------------- #
# Codex prompts + OpenCode commands (same bodies, other agents)
# --------------------------------------------------------------------------- #


def codex_prompts_dir() -> Path:
    """Codex prompts dir, honouring a custom CODEX_HOME (default ~/.codex)."""
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "prompts"


def install_codex_prompts(target_dir: Path | None = None) -> dict[str, str]:
    """Write /remember, /recall, /forget, /memory as Codex custom prompts."""
    d = Path(target_dir) if target_dir else codex_prompts_dir()
    return _write_managed(d, CODEX_PROMPTS)


def uninstall_codex_prompts(target_dir: Path | None = None) -> list[str]:
    d = Path(target_dir) if target_dir else codex_prompts_dir()
    return _remove_managed(d, CODEX_PROMPTS)


def install_opencode_commands(config_path: Path) -> list[str]:
    """Merge /remember etc. into opencode.json's ``command`` table.

    Same merge policy as the MCP entry: existing keys (user's own commands)
    are never overwritten; ours are recognisable by the foldcrumbs mention in
    the template. Returns the commands added.
    """
    import json

    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    commands = cfg.setdefault("command", {})
    added: list[str] = []
    for name, (desc, _hint, body) in _BODIES.items():
        if name in commands:
            continue
        commands[name] = {"description": desc, "template": body.strip()}
        added.append(name)
    if added:
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return added


def uninstall_opencode_commands(config_path: Path) -> list[str]:
    """Remove our command entries from opencode.json.

    Only entries whose template is recognisably ours (mentions foldcrumbs) are
    removed — a user's own command under the same name is left alone."""
    path = Path(config_path)
    if not path.exists():
        return []
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    commands = cfg.get("command", {})
    removed = [
        name for name in _BODIES
        if isinstance(commands.get(name), dict)
        and "foldcrumbs" in commands[name].get("template", "")
    ]
    for name in removed:
        del commands[name]
    if removed:
        if not commands:
            cfg.pop("command", None)
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return removed
