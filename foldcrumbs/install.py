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
import subprocess
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

This project has a persistent memory store. Use the foldcrumbs tools if
your runtime exposes them (MCP tools `recall`/`remember`, or native
`foldcrumbs_recall`/`foldcrumbs_remember` tools); otherwise shell out to
the `foldcrumbs` CLI (`foldcrumbs recall "…"` / `foldcrumbs remember "…"`)
— same store, same semantics:
- At the start of a task, recall with your task to load prior decisions,
  conventions and preferences — do not re-ask what is already recorded.
- When a durable decision, rule, preference or lesson is established,
  remember it to persist it for future sessions.
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
# Pi coding agent (pi.dev): TS extension auto-discovered by jiti, no build.
# Verified against @earendil-works/pi-coding-agent dist types + examples:
# extensions live in ~/.pi/agent/extensions/*.ts (global) or .pi/extensions/*.ts
# (project, gated by project trust); registerTool takes a TypeBox schema;
# before_agent_start may return {systemPrompt} to append context; pi reads
# AGENTS.md natively (project dir and ~/.pi/agent/AGENTS.md global).
# No MCP client in pi — the extension shells out to the foldcrumbs CLI and
# reuses the agent-agnostic Python hook for the session-start index inject.
# --------------------------------------------------------------------------- #

PI_EXTENSION = '''\
// foldcrumbs memory extension for the pi coding agent (pi.dev).
// Installed by `foldcrumbs install --agent pi`. Tools shell out to the
// foldcrumbs CLI; the session-start index reuses the same agent-agnostic
// Python hook Claude Code/Codex use. No MCP, no build step (jiti loads TS).
import {{ execFileSync, spawnSync }} from "node:child_process";
import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";
import {{ Type }} from "typebox";

const FOLDCRUMBS_BIN = {bin};
const HOOK = {hook};
const PYTHON = {python};

interface RunResult {{ ok: boolean; text: string }}

function run(args: string[], cwd?: string): RunResult {{
  // RT P1 F3: failures surface as tool errors (isError), not as ordinary
  // text — a dead CLI must not read like an empty answer.
  try {{
    const out = execFileSync(FOLDCRUMBS_BIN, args, {{
      cwd, encoding: "utf-8", timeout: 30_000, stdio: ["ignore", "pipe", "pipe"],
    }});
    return {{ ok: true, text: out }};
  }} catch (e: any) {{
    const detail = e?.stderr?.toString?.().trim() || e?.message || String(e);
    return {{ ok: false, text: `foldcrumbs failed: ${{detail}}` }};
  }}
}}

function toolResult(r: RunResult, emptyText?: string) {{
  if (!r.ok) {{
    return {{
      content: [{{ type: "text" as const, text: r.text }}],
      details: {{}},
      isError: true,
    }};
  }}
  return {{
    content: [{{ type: "text" as const, text: r.text || (emptyText ?? "") }}],
    details: {{}},
  }};
}}

const RECALL_PARAMS = Type.Object({{
  query: Type.String({{ description: "what to recall" }}),
  index: Type.Optional(Type.Boolean({{ description: "hit list only (cheap)" }})),
}});

const REMEMBER_PARAMS = Type.Object({{
  content: Type.String({{ description: "the durable fact/decision/lesson" }}),
  type: Type.Optional(Type.String({{ description: "fact|decision|preference|..." }})),
  expires: Type.Optional(Type.String({{ description: "ISO date; for dated truths" }})),
}});

export default function foldcrumbsPiExtension(pi: ExtensionAPI) {{
  let indexBlock = "";

  // Reuse the agent-agnostic hook: it reads {{cwd, session_id}} on stdin and
  // emits hookSpecificOutput.additionalContext (the MEMORY.md index block).
  pi.on("session_start", async (_event, ctx) => {{
    try {{
      const res = spawnSync(PYTHON, [HOOK], {{
        input: JSON.stringify({{ cwd: ctx.cwd, session_id: "pi", source: "startup" }}),
        encoding: "utf-8", timeout: 30_000,
      }});
      const parsed = JSON.parse(res.stdout || "{{}}");
      indexBlock = parsed?.hookSpecificOutput?.additionalContext ?? "";
    }} catch {{
      indexBlock = "";
    }}
  }});

  pi.on("before_agent_start", async (event) => {{
    if (!indexBlock) return;
    return {{ systemPrompt: event.systemPrompt + "\\n\\n" + indexBlock + "\\n" }};
  }});

  pi.registerTool({{
    name: "foldcrumbs_recall",
    label: "Recall memory",
    description:
      "Search this project's persistent memory (decisions, conventions, " +
      "preferences from previous sessions). Use index=true for a cheap hit " +
      "list, then read a file for detail.",
    promptSnippet: "search persistent project memory",
    parameters: RECALL_PARAMS,
    async execute(_id, params, _signal, _onUpdate, ctx) {{
      // RT P1 F2: options first, then "--", then the positional query —
      // a query starting with "-" is content, never an option.
      const args = ["recall"];
      if (params.index) args.push("--index");
      args.push("--", params.query);
      return toolResult(run(args, ctx.cwd), "(no matching memories)");
    }},
  }});

  pi.registerTool({{
    name: "foldcrumbs_remember",
    label: "Remember",
    description:
      "Persist a durable fact, decision, preference or lesson for future " +
      "sessions. One fact per call. Authorizations are refused here by " +
      "design (human CLI path only).",
    promptSnippet: "persist a durable memory",
    parameters: REMEMBER_PARAMS,
    async execute(_id, params, _signal, _onUpdate, ctx) {{
      // RT P1 F2: --opt=value form + "--" guard — content starting with
      // "-" (even "--grants=...") is stored literally, never parsed as
      // an option. Invalid types fail closed in the CLI (argparse
      // choices), surfacing as a tool error via isError.
      const args = ["remember"];
      if (params.type) args.push(`--type=${{params.type}}`);
      if (params.expires) args.push(`--expires=${{params.expires}}`);
      args.push("--", params.content);
      return toolResult(run(args, ctx.cwd));
    }},
  }});
}}
'''


def pi_paths(global_scope: bool = True) -> dict[str, Path]:
    """Resolve pi extension/AGENTS paths for global or project scope."""
    if global_scope:
        base = Path.home() / ".pi" / "agent"
        return {"extensions": base / "extensions", "agents": base / "AGENTS.md"}
    base = Path.cwd() / ".pi"
    return {"extensions": base / "extensions",
            "agents": Path.cwd() / "AGENTS.md"}


def write_pi_extension(extensions_dir: Path,
                       runtime_root: Path | None = None) -> Path:
    """Write the foldcrumbs pi extension into an auto-discovered dir.

    Idempotent by filename (pi loads *.ts from the dir); rewrites on every
    install so the extension tracks the staged runtime location.
    """
    hooks_dir = _stage_hook_runtime(runtime_root)
    py = sys.executable or "python3"
    d = Path(extensions_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "foldcrumbs.ts"
    # JSON-string literals: valid TS, correct escaping on every platform
    body = PI_EXTENSION.format(
        bin=json.dumps(shutil.which("foldcrumbs") or "foldcrumbs"),
        hook=json.dumps(str(hooks_dir / "session_start.py")),
        python=json.dumps(py),
    )
    path.write_text(body, encoding="utf-8")
    return path


def remove_pi_extension(extensions_dir: Path) -> bool:
    """Delete our extension file (never touches other extensions)."""
    path = Path(extensions_dir) / "foldcrumbs.ts"
    if path.exists():
        path.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# Claude Code MCP registration
# --------------------------------------------------------------------------- #


def claude_mcp_snippet(runtime_root: Path | None = None) -> str:
    """A .mcp.json fragment for manual registration (fallback path)."""
    cmd = _mcp_command(runtime_root)
    return json.dumps(
        {"mcpServers": {"foldcrumbs": {"command": cmd[0], "args": cmd[1:]}}},
        indent=2,
    )


def install_claude_mcp(
    runtime_root: Path | None = None,
    claude_bin: str | None = None,
    scope: str = "user",
) -> str:
    """Register the foldcrumbs MCP server with Claude Code (user scope).

    Prefers the `claude mcp add` CLI — it owns the config file format, so we
    never hand-edit ~/.claude.json. Idempotent via `claude mcp get`. When the
    CLI is missing or the add fails, returns the .mcp.json snippet for manual
    registration instead of guessing at file surgery. The staged-runtime
    launcher keeps the registered command independent of the source checkout,
    same as the hooks.
    """
    cmd = _mcp_command(runtime_root)
    exe = shutil.which(claude_bin or config.claude_bin())
    if exe is None:
        return ("claude CLI not found — register manually by adding this to "
                ".mcp.json (project) or via `claude mcp add`:\n"
                + claude_mcp_snippet(runtime_root))
    try:
        # `mcp get` succeeds if the server exists in ANY visible scope, so the
        # probe must also match the requested scope (its output names it, e.g.
        # "Scope: User config") — otherwise `install --local` with an existing
        # user-scoped entry would silently skip the project registration. And
        # a scope match alone is not enough: a registration pointing at an old
        # interpreter or runtime path must be replaced, or re-running install
        # could never repair a moved Python environment.
        probe = subprocess.run([exe, "mcp", "get", "foldcrumbs"],
                               capture_output=True, text=True, timeout=30)
        stale = False
        if probe.returncode == 0 and f"{scope} config" in probe.stdout.lower():
            if all(part in probe.stdout for part in cmd):
                return "already registered"
            stale = True
            subprocess.run([exe, "mcp", "remove", "--scope", scope, "foldcrumbs"],
                           capture_output=True, text=True, timeout=30)
        add_cmd = [exe, "mcp", "add", "--scope", scope, "foldcrumbs", "--", *cmd]
        add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=30)
        if add.returncode != 0 and not stale:
            # `mcp get` reports only the EFFECTIVE registration; the requested
            # scope may hold a shadowed entry the probe never showed (e.g. a
            # project entry hidden by a user one). Replace it in place so a
            # reinstall can still repair the shadowed scope.
            subprocess.run([exe, "mcp", "remove", "--scope", scope, "foldcrumbs"],
                           capture_output=True, text=True, timeout=30)
            add = subprocess.run(add_cmd, capture_output=True, text=True,
                                 timeout=30)
            stale = add.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (f"claude CLI failed ({exc}) — register manually:\n"
                + claude_mcp_snippet(runtime_root))
    if add.returncode == 0:
        return f"{'refreshed' if stale else 'registered'} ({scope} scope)"
    err = (add.stderr or add.stdout).strip().splitlines()
    detail = err[-1] if err else "unknown error"
    return (f"claude mcp add failed: {detail}\nRegister manually:\n"
            + claude_mcp_snippet(runtime_root))


def uninstall_claude_mcp(
    claude_bin: str | None = None, scope: str = "user"
) -> str:
    """Best-effort `claude mcp remove foldcrumbs` from the scope we installed
    into (mirror install: user by default, project for --local)."""
    exe = shutil.which(claude_bin or config.claude_bin())
    if exe is None:
        return "claude CLI not found — remove manually with `claude mcp remove foldcrumbs`"
    try:
        res = subprocess.run([exe, "mcp", "remove", "--scope", scope, "foldcrumbs"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"claude CLI failed ({exc})"
    return "removed" if res.returncode == 0 else "not registered"


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
