"""Merge-safe installers for foldcrumbs across coding agents.

The hook scripts are agent-agnostic (they read cwd/transcript from the payload
and emit ``hookSpecificOutput.additionalContext``), so Claude Code and Codex
reuse the same scripts under their respective event names. For every agent we
append our own hook groups WITHOUT disturbing existing hooks, and skip if ours
are already registered (idempotent). Settings files are backed up first.

OpenCode has no SessionStart-style hook that can inject context, so there we
install an MCP server entry + a plugin + an AGENTS.md instruction (prompt-driven
recall/remember). Codex also gets an MCP entry merged into its TOML config.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from . import config

PACKAGE_DIR = Path(__file__).resolve().parent
HOOKS_DIR = PACKAGE_DIR / "hooks"
_MARKER = "foldcrumbs/hooks/"  # any command containing this path is ours
_LEGACY_MARKERS = ("engram/hooks/",)  # pre-rename installs to clean up on migrate

# Per-agent hook maps: event -> (script, matcher). The same scripts are reused;
# only event names and matcher conventions differ between agents.
_CLAUDE_HOOKS = {
    "SessionStart": ("session_start.py", ""),
    "PostCompact": ("post_compact.py", ""),
    "SessionEnd": ("session_end.py", ""),
    "PostToolUse": ("context_monitor.py", "Bash|Edit|Write|MultiEdit|Agent|Task"),
}
_CODEX_HOOKS = {
    "SessionStart": ("session_start.py", "*"),
    "PostCompact": ("post_compact.py", "*"),
    "Stop": ("session_end.py", "*"),
    "PostToolUse": ("context_monitor.py", "*"),
}

_AGENT_HOOKS = {"claude": _CLAUDE_HOOKS, "codex": _CODEX_HOOKS}


def _stage_runtime(runtime_root: Path | None = None) -> tuple[Path, Path]:
    """Copy a self-contained runtime outside the source checkout.

    Codex lifecycle hooks run in a different macOS privacy context from the
    terminal that launched Codex.  A hook command pointing into an editable
    checkout under ~/Documents can therefore fail with ``Operation not
    permitted`` even though the same interpreter can read it from a terminal.
    Keep the registered command machine-local and independent of checkout
    location by snapshotting the package under ~/.foldcrumbs/runtime.
    """
    root = Path(runtime_root or (config.STATE_DIR / "runtime")).expanduser()
    package_dir = root / "foldcrumbs"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        PACKAGE_DIR,
        package_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    mcp_launcher = root / "foldcrumbs_mcp.py"
    mcp_launcher.write_text(
        "from foldcrumbs.mcp_server import main\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    return package_dir, mcp_launcher


def _stage_hook_runtime(runtime_root: Path | None = None) -> Path:
    package_dir, _ = _stage_runtime(runtime_root)
    return package_dir / "hooks"


def _command_for(script: str, hooks_dir: Path = HOOKS_DIR) -> str:
    py = sys.executable or "python3"
    return f'"{py}" "{hooks_dir / script}"'


def _mcp_command(runtime_root: Path | None = None) -> list[str]:
    _, launcher = _stage_runtime(runtime_root)
    return [sys.executable or "python3", str(launcher)]


# --------------------------------------------------------------------------- #
# LLM backend selection (machine-local; written to ~/.foldcrumbs)
# --------------------------------------------------------------------------- #

# Ordered for the interactive menu. Each: (key, one-line description).
BACKEND_CHOICES: list[tuple[str, str]] = [
    ("claude-cli", "Claude subscription — shell out to `claude -p` (no API key)"),
    ("codex", "Codex subscription — shell out to `codex exec` (no API key)"),
    ("openai", "OpenAI-compatible HTTP endpoint (local server or remote gateway)"),
    ("none", "No LLM — keyword heuristic only (last resort, lower quality)"),
]
_BACKEND_BIN = {"claude-cli": ("claude-bin", "claude"), "codex": ("codex-bin", "codex")}


def detect_bin(name: str) -> str | None:
    """Absolute path of a CLI on PATH, or None. Hooks run with a minimal PATH, so
    we persist the resolved absolute path rather than the bare name."""
    return shutil.which(name)


def configure_backend(
    choice: str,
    *,
    state_dir: Path | None = None,
    bin_path: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> list[str]:
    """Persist the LLM backend choice to the machine-local state dir.

    Writes ``llm-backend`` plus the backend's companion file: the CLI binary
    path for claude-cli/codex (auto-detected when not given), or endpoint/model
    for openai. ``none`` writes only the backend marker. Returns the relative
    filenames written, for reporting.
    """
    choice = choice.strip().lower()
    if choice not in config.BACKENDS:
        raise ValueError(f"unknown backend {choice!r}; pick one of {', '.join(config.BACKENDS)}")
    d = Path(state_dir or config.STATE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def _write(name: str, value: str) -> None:
        (d / name).write_text(value.strip() + "\n", encoding="utf-8")
        written.append(name)

    _write("llm-backend", choice)
    if choice in _BACKEND_BIN:
        fname, exe = _BACKEND_BIN[choice]
        resolved = bin_path or detect_bin(exe) or exe
        _write(fname, resolved)
    elif choice == "openai":
        if endpoint:
            _write("llm-endpoint", endpoint)
        if model:
            _write("llm-model", model)
    return written


def prompt_backend(in_fn=input, out_fn=print) -> str | None:
    """Interactively ask which LLM backend to use. Returns the chosen key, or
    None if the user aborts (EOF/blank at a non-default). Pure-IO via injected
    callables so it's testable and so callers can skip it when non-interactive."""
    out_fn("\nHow should foldcrumbs distill memories? (recall never uses an LLM)\n")
    for i, (key, desc) in enumerate(BACKEND_CHOICES, 1):
        hint = ""
        if key in _BACKEND_BIN and detect_bin(_BACKEND_BIN[key][1]):
            hint = "  [detected]"
        out_fn(f"  {i}) {key:<11} {desc}{hint}")
    default_idx = 1
    try:
        raw = in_fn(f"\nChoose [1-{len(BACKEND_CHOICES)}] (default {default_idx}): ").strip()
    except EOFError:
        return None
    if not raw:
        return BACKEND_CHOICES[default_idx - 1][0]
    # Accept either the number or the backend name.
    for i, (key, _) in enumerate(BACKEND_CHOICES, 1):
        if raw == str(i) or raw.lower() == key:
            return key
    out_fn(f"Unrecognised choice {raw!r}; leaving backend unchanged.")
    return None


# --------------------------------------------------------------------------- #
# Hook installer (JSON settings: Claude Code settings.json, Codex hooks.json)
# --------------------------------------------------------------------------- #


def _already_present(groups: list, script: str) -> bool:
    needle = f"{_MARKER}{script}"
    return any(
        needle in h.get("command", "")
        for group in groups
        for h in group.get("hooks", [])
    )


def _has_legacy(group: dict) -> bool:
    """True if a group is a pre-rename (engram) install of ours.

    A migrating machine still has ``engram/hooks/...`` commands in its
    settings.json; those paths no longer exist after the package is renamed, so
    install/uninstall must recognise and clear them instead of leaving orphans.
    """
    return any(
        m in h.get("command", "")
        for h in group.get("hooks", [])
        for m in _LEGACY_MARKERS
    )


def default_settings_path(agent: str = "claude", global_scope: bool = True) -> Path:
    if agent == "codex":
        return Path.home() / ".codex" / "hooks.json"
    if global_scope:
        return config.claude_config_dir() / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _remove_script_hooks(groups: list, script: str) -> None:
    """Remove one stale foldcrumbs script while preserving foreign hooks."""
    needle = f"{_MARKER}{script}"
    kept_groups = []
    for group in groups:
        kept_hooks = [
            hook for hook in group.get("hooks", [])
            if needle not in hook.get("command", "")
        ]
        if kept_hooks:
            kept_group = dict(group)
            kept_group["hooks"] = kept_hooks
            kept_groups.append(kept_group)
    groups[:] = kept_groups


def install_hooks(
    settings_path: Path,
    agent: str = "claude",
    timeout: int = 15,
    runtime_root: Path | None = None,
) -> list[str]:
    """Merge foldcrumbs hooks into a JSON settings/hooks file. Returns changes."""
    hooks_map = _AGENT_HOOKS[agent]
    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_hooks_dir = _stage_hook_runtime(runtime_root)

    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            settings = {}
        shutil.copy2(settings_path, settings_path.with_suffix(".json.foldcrumbs-bak"))

    hooks = settings.setdefault("hooks", {})
    changes: list[str] = []
    for event, (script, matcher) in hooks_map.items():
        groups = hooks.setdefault(event, [])
        # Clear any pre-rename (engram) group for this event first, so a migrating
        # machine ends up with foldcrumbs hooks only — no orphaned engram commands.
        if any(_has_legacy(g) for g in groups):
            groups[:] = [g for g in groups if not _has_legacy(g)]
            changes.append(f"{event} -> removed legacy engram hook")
        command = _command_for(script, runtime_hooks_dir)
        if any(
            hook.get("command") == command
            for group in groups
            for hook in group.get("hooks", [])
        ):
            continue
        change = f"{event} -> {script}"
        if _already_present(groups, script):
            _remove_script_hooks(groups, script)
            change = f"{event} -> refreshed {script}"
        entry = {"type": "command", "command": command, "timeout": timeout}
        group: dict = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        changes.append(change)

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return changes


def uninstall_hooks(settings_path: Path) -> list[str]:
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
        kept = [g for g in hooks[event]
                if not any(_MARKER in h.get("command", "") for h in g.get("hooks", []))
                and not _has_legacy(g)]
        if len(kept) != len(hooks[event]):
            removed.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return removed


# Back-compat aliases (Claude Code default).
def install(settings_path: Path, timeout: int = 15) -> list[str]:
    return install_hooks(settings_path, agent="claude", timeout=timeout)


def uninstall(settings_path: Path) -> list[str]:
    return uninstall_hooks(settings_path)


# --------------------------------------------------------------------------- #
# OpenCode: MCP entry in opencode.json (mergeable JSON) + plugin + AGENTS.md
# --------------------------------------------------------------------------- #


def install_opencode_mcp(config_path: Path) -> list[str]:
    """Merge a foldcrumbs MCP server into opencode.json. Returns changes."""
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        shutil.copy2(config_path, config_path.with_suffix(".json.foldcrumbs-bak"))
    mcp = cfg.setdefault("mcp", {})
    if "foldcrumbs" in mcp:
        return []
    mcp["foldcrumbs"] = {"type": "local", "command": _mcp_command(), "enabled": True}
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return ["mcp.foldcrumbs"]


OPENCODE_PLUGIN = '''\
// foldcrumbs memory plugin for OpenCode — distills the session on idle/compaction.
// Recall is prompt-driven via AGENTS.md (the agent calls the foldcrumbs MCP tools).
import { spawn } from "node:child_process";

function distill(kind) {
  // Fire-and-forget; foldcrumbs reads the transcript and writes durable memories.
  try { spawn("foldcrumbs", ["index"], { detached: true, stdio: "ignore" }).unref(); }
  catch (_) {}
}

export default function foldcrumbsPlugin() {
  return {
    hooks: {
      "session.idle": async () => distill("idle"),
      "session.compacted": async () => distill("compacted"),
    },
  };
}
'''

AGENTS_MD_BLOCK = """\
## Memory (foldcrumbs)

This project has a persistent memory store. Use the `foldcrumbs` MCP tools:
- At the start of a task, call `recall` with your task to load prior decisions,
  conventions and preferences — do not re-ask what is already recorded.
- When a durable decision, rule, preference or lesson is established, call
  `remember` to persist it for future sessions.
"""


def write_opencode_plugin(plugins_dir: Path) -> Path:
    d = Path(plugins_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "foldcrumbs.ts"
    path.write_text(OPENCODE_PLUGIN, encoding="utf-8")
    return path


def append_agents_md(agents_path: Path) -> Path | None:
    """Append the foldcrumbs instruction block to an AGENTS.md if not already there."""
    path = Path(agents_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if "Memory (foldcrumbs)" in existing:
        return None
    sep = "" if not existing or existing.endswith("\n\n") else "\n\n"
    path.write_text(existing + sep + AGENTS_MD_BLOCK, encoding="utf-8")
    return path


def opencode_paths(global_scope: bool = True) -> dict[str, Path]:
    """Resolve opencode config/plugin/AGENTS paths for global or project scope."""
    if global_scope:
        base = Path.home() / ".config" / "opencode"
        return {"config": base / "opencode.json", "plugins": base / "plugins",
                "agents": base / "AGENTS.md"}
    base = Path.cwd()
    return {"config": base / "opencode.json", "plugins": base / ".opencode" / "plugins",
            "agents": base / "AGENTS.md"}


# --------------------------------------------------------------------------- #
# Codex MCP config
# --------------------------------------------------------------------------- #


def codex_mcp_snippet(runtime_root: Path | None = None) -> str:
    cmd = _mcp_command(runtime_root)
    args = ", ".join(json.dumps(a) for a in cmd[1:])
    return (
        "[mcp_servers.foldcrumbs]\n"
        f"command = {json.dumps(cmd[0])}\n"
        f"args = [{args}]\n"
    )


def _refresh_codex_mcp_table(existing: str, snippet: str) -> str:
    """Refresh generated command/args while preserving other table settings."""
    header = "[mcp_servers.foldcrumbs]"
    match = re.search(r"(?m)^\[mcp_servers\.foldcrumbs\]\s*$", existing)
    if not match:
        sep = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
        return existing + sep + snippet
    start = match.start()

    next_table = re.search(r"(?m)^\[", existing[start + len(header):])
    end = (
        start + len(header) + next_table.start()
        if next_table
        else len(existing)
    )
    section = existing[start:end]
    desired = dict(
        line.split(" = ", 1)
        for line in snippet.splitlines()[1:]
        if " = " in line
    )
    for key in ("command", "args"):
        line = f"{key} = {desired[key]}"
        pattern = rf"(?m)^{key}\s*=.*$"
        if re.search(pattern, section):
            section = re.sub(pattern, line, section, count=1)
        else:
            section = section.replace(header, f"{header}\n{line}", 1)
    return existing[:start] + section + existing[end:]


def install_codex_mcp_toml(
    config_path: Path | None = None,
    runtime_root: Path | None = None,
) -> str:
    """Add or refresh [mcp_servers.foldcrumbs] in ~/.codex/config.toml.

    Existing optional table settings are preserved. Returns a status string.
    """
    config_path = Path(config_path or (Path.home() / ".codex" / "config.toml"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    snippet = codex_mcp_snippet(runtime_root)
    updated = _refresh_codex_mcp_table(existing, snippet)
    if updated == existing:
        return "already present"
    if existing:
        shutil.copy2(config_path, config_path.with_suffix(".toml.foldcrumbs-bak"))
    config_path.write_text(updated, encoding="utf-8")
    action = "updated" if "[mcp_servers.foldcrumbs]" in existing else "added"
    return f"{action} {config_path}"
