"""Merge-safe installers for engram across coding agents.

The hook scripts are agent-agnostic (they read cwd/transcript from the payload
and emit ``hookSpecificOutput.additionalContext``), so Claude Code and Codex
reuse the same scripts under their respective event names. For every agent we
append our own hook groups WITHOUT disturbing existing hooks, and skip if ours
are already registered (idempotent). Settings files are backed up first.

OpenCode has no SessionStart-style hook that can inject context, so there we
install an MCP server entry + a plugin + an AGENTS.md instruction (prompt-driven
recall/remember). Codex also gets an MCP entry (printed as a TOML snippet, since
we don't hand-edit TOML).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import config

HOOKS_DIR = Path(__file__).resolve().parent / "hooks"
_MARKER = "engram/hooks/"  # any command containing this path is ours

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


def _command_for(script: str) -> str:
    py = sys.executable or "python3"
    return f'"{py}" "{HOOKS_DIR / script}"'


def _mcp_command() -> list[str]:
    return [sys.executable or "python3", "-m", "engram.mcp_server"]


# --------------------------------------------------------------------------- #
# LLM backend selection (machine-local; written to ~/.engram)
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
    out_fn("\nHow should engram distill memories? (recall never uses an LLM)\n")
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


def default_settings_path(agent: str = "claude", global_scope: bool = True) -> Path:
    if agent == "codex":
        return Path.home() / ".codex" / "hooks.json"
    if global_scope:
        return config.claude_config_dir() / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def install_hooks(settings_path: Path, agent: str = "claude", timeout: int = 15) -> list[str]:
    """Merge engram hooks into a JSON settings/hooks file. Returns changes."""
    hooks_map = _AGENT_HOOKS[agent]
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
    for event, (script, matcher) in hooks_map.items():
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
                if not any(_MARKER in h.get("command", "") for h in g.get("hooks", []))]
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
    """Merge an engram MCP server into opencode.json. Returns changes."""
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        shutil.copy2(config_path, config_path.with_suffix(".json.engram-bak"))
    mcp = cfg.setdefault("mcp", {})
    if "engram" in mcp:
        return []
    mcp["engram"] = {"type": "local", "command": _mcp_command(), "enabled": True}
    config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return ["mcp.engram"]


OPENCODE_PLUGIN = '''\
// engram memory plugin for OpenCode — distills the session on idle/compaction.
// Recall is prompt-driven via AGENTS.md (the agent calls the engram MCP tools).
import { spawn } from "node:child_process";

function distill(kind) {
  // Fire-and-forget; engram reads the transcript and writes durable memories.
  try { spawn("engram", ["index"], { detached: true, stdio: "ignore" }).unref(); }
  catch (_) {}
}

export default function engramPlugin() {
  return {
    hooks: {
      "session.idle": async () => distill("idle"),
      "session.compacted": async () => distill("compacted"),
    },
  };
}
'''

AGENTS_MD_BLOCK = """\
## Memory (engram)

This project has a persistent memory store. Use the `engram` MCP tools:
- At the start of a task, call `recall` with your task to load prior decisions,
  conventions and preferences — do not re-ask what is already recorded.
- When a durable decision, rule, preference or lesson is established, call
  `remember` to persist it for future sessions.
"""


def write_opencode_plugin(plugins_dir: Path) -> Path:
    d = Path(plugins_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "engram.ts"
    path.write_text(OPENCODE_PLUGIN, encoding="utf-8")
    return path


def append_agents_md(agents_path: Path) -> Path | None:
    """Append the engram instruction block to an AGENTS.md if not already there."""
    path = Path(agents_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if "Memory (engram)" in existing:
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
# Config snippets to print (TOML we don't hand-edit)
# --------------------------------------------------------------------------- #


def codex_mcp_snippet() -> str:
    cmd = _mcp_command()
    args = ", ".join(json.dumps(a) for a in cmd[1:])
    return (
        "[mcp_servers.engram]\n"
        f"command = {json.dumps(cmd[0])}\n"
        f"args = [{args}]\n"
    )


def install_codex_mcp_toml(config_path: Path | None = None) -> str:
    """Append [mcp_servers.engram] to ~/.codex/config.toml if not present.

    Appending a new table at EOF is safe for existing TOML; we never rewrite or
    reorder existing content. Returns a status string.
    """
    config_path = Path(config_path or (Path.home() / ".codex" / "config.toml"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if "[mcp_servers.engram]" in existing:
        return "already present"
    if existing:
        shutil.copy2(config_path, config_path.with_suffix(".toml.engram-bak"))
    sep = "" if not existing or existing.endswith("\n\n") else "\n\n" if existing.endswith("\n") else "\n\n"
    config_path.write_text(existing + sep + codex_mcp_snippet(), encoding="utf-8")
    return f"added to {config_path}"
