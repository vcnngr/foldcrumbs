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
    fm = [
        "---",
        f"description: {description}",
    ]
    if argument_hint:
        fm.append(f"argument-hint: {argument_hint}")
    fm += [f"allowed-tools: {_ALLOWED}", "---"]
    return "\n".join(fm) + "\n" + _MARKER_LINE + "\n\n" + body.strip() + "\n"


COMMANDS: dict[str, str] = {
    "remember.md": _cmd(
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
    "recall.md": _cmd(
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
    "forget.md": _cmd(
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
    "memory.md": _cmd(
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
    d.mkdir(parents=True, exist_ok=True)
    actions: dict[str, str] = {}
    for name, content in COMMANDS.items():
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


def uninstall_commands(target_dir: Path | None = None) -> list[str]:
    """Remove our managed command files (user-owned files are left alone)."""
    d = Path(target_dir) if target_dir else commands_dir()
    removed: list[str] = []
    for name in COMMANDS:
        path = d / name
        if path.exists() and is_managed(path):
            try:
                path.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed
