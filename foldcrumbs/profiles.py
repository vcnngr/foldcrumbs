"""Named memory profiles: one identity per agent, node or role.

(Not to be confused with ``profile.py``, which renders the context block
injected into a session. This module is about *whose* memory is being read.)

A profile is a registered root with a name and a shape. Both shapes already
existed — this gives them a vocabulary, a place to live, and a way to see them
side by side:

* **dedicated** — one memory directory, the same from every project. What a
  long-running agent wants: a node on a chat bus, a bot with one job, anything
  whose memory is about *itself* rather than about whichever repository it
  happens to be standing in. Backed by ``FOLDCRUMBS_DIR``.
* **shared** — memory kept per project underneath a config directory, which is
  how an interactive assistant works: what it learned about one repository
  should not surface in another. Backed by ``CLAUDE_CONFIG_DIR``.

The choice is per profile, not per installation. A machine can run several
dedicated agents and one shared assistant, and the federated index shows all
of them together — each store still written only by its owner.

**There is no "switch to this profile" command, deliberately.** Which store a
process uses is decided by its environment before it starts, and a CLI cannot
reach back into the shell that launched it. Pretending otherwise would make
``profile use`` appear to work and silently do nothing. ``env`` prints the one
line that does work, to put in a service file, a shell profile, or an eval.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from . import config, federation

DEDICATED, SHARED = "dedicated", "shared"

# A profile's shape *is* the root's mode; this is only the vocabulary. Kept in
# one place so the two never drift into meaning different things.
_MODE_FOR = {DEDICATED: "explicit", SHARED: "config"}
_KIND_FOR = {v: k for k, v in _MODE_FOR.items()}


def default_path(name: str) -> Path:
    """Where a dedicated profile keeps its memory unless told otherwise.

    Under the state directory, which is machine-local: a profile is one
    agent's own memory, and putting it beside the registry keeps it out of
    whatever the config directories are doing.
    """
    return config.STATE_DIR / "profiles" / name


class AmbiguousProfile(LookupError):
    """More than one root answers to that name.

    Names are labels, and nothing has ever made a label unique — two config
    directories called ``.claude`` under different homes are both plausible.
    So a command that acts on a name refuses rather than picking one: the
    wrong guess here points a process at another agent's store, or unregisters
    an agent that was never meant to be touched.
    """


def _matching(name: str) -> list:
    return [r for r in federation.iter_roots() if r.label == name]


def _only(name: str):
    """The single root by that name, or None. Raises if several answer."""
    found = _matching(name)
    if len(found) > 1:
        raise AmbiguousProfile(
            f"{len(found)} profiles are called {name!r}: "
            + ", ".join(f"{r.id} → {r.path}" for r in found))
    return found[0] if found else None


def _bad_name(name: str) -> str | None:
    """Why a name cannot be a profile, or None when it can.

    One source of truth: ``add`` refuses with these same words, and the
    import dry-run pre-checks with them so a broken prefix is visible before
    ``--apply``, not discovered by it.
    """
    if not name or "/" in name or os.sep in name or name.startswith("."):
        return f"not usable as a profile name: {name!r}"
    return None


def add(name: str, kind: str = DEDICATED, path: str | os.PathLike[str] | None = None):
    """Register a profile. Returns its root, or None if it could not be made.

    A dedicated profile invents its directory when none is given; a shared one
    must be told which config directory it stands for, because that directory
    is someone else's and guessing at it would be a decision, not a default.
    """
    if kind not in _MODE_FOR:
        raise ValueError(f"not a profile shape: {kind}")
    bad = _bad_name(name)
    if bad:
        raise ValueError(bad)
    if path is None:
        if kind == SHARED:
            raise ValueError(
                "a shared profile needs the config directory it stands for")
        target = default_path(name)
        target.mkdir(parents=True, exist_ok=True)
    else:
        target = Path(os.path.abspath(os.path.expanduser(str(path))))
    # ``unique_label`` rather than a check here: registration already holds the
    # registry lock, and asking first would be a check-then-act — two processes
    # both find the name free, both take it, and it then identifies nothing.
    return federation.register(target, mode=_MODE_FOR[kind], label=name,
                               unique_label=True)


# Where a multi-agent runtime keeps its own profiles on this machine. Read
# only ever: these directories belong to that runtime, and foldcrumbs takes
# names from them, never writes into them.
AGENT_HOMES = {
    "hermes": Path.home() / ".hermes" / "profiles",
}


def discover(agent: str = "hermes", home: str | os.PathLike[str] | None = None
             ) -> list[str]:
    """The agent profiles configured on this machine, by name.

    A runtime that already runs one agent per profile has done the hard part —
    it knows who its agents are. This reads that list so each can be given a
    memory of its own, and returns empty when the runtime is not installed
    rather than treating its absence as an error.

    Includes the runtime's own ``default`` profile when there is one: a
    hermes installation keeps its named profiles under ``profiles/``, but the
    default profile *is* the home directory itself (its SOUL/config live at
    the root). Skipping it would silently import everyone except the agent
    that answers when no profile is named. Detection is layout-based — a
    ``SOUL.md`` beside the profiles directory — so a bare folder of
    directories (a test fixture, a partial tree) does not invent a profile.
    """
    base = Path(home) if home is not None else AGENT_HOMES.get(agent)
    if base is None:
        raise ValueError(f"no known profile layout for {agent!r}")
    try:
        names = sorted(d.name for d in base.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
    except OSError:
        return []
    if (base.parent / "SOUL.md").is_file():
        # The home itself is a profile — the runtime's default one.
        names = sorted(names + ["default"])
    return names


def import_agent(agent: str = "hermes", home=None, apply: bool = False,
                 prefix: str | None = None) -> dict:
    """Give each of that agent's profiles a dedicated memory of its own.

    Dedicated, not shared: an agent on a chat bus carries its memory with it
    rather than per repository. Each store lives under foldcrumbs' own state
    directory — putting them inside the other runtime's tree would make two
    tools own one directory, and the first to tidy up would take the other's
    data with it.

    Dry-run unless ``apply``. Outcomes are kept apart so a failure is never
    reported as success: ``skipped`` means the profile was already registered
    (nothing to do), ``failed`` means registration did not work and why — an
    invalid name (e.g. from a prefix that breaks the naming rules), a root
    that could not be made, or a conflict with an existing registration under
    the same directory. Callers surface ``failed`` and return a non-zero exit.
    """
    label = (lambda n: f"{prefix}{n}") if prefix else (lambda n: n)
    found = discover(agent, home)
    taken = {p["name"] for p in listing()}
    plan = {n: label(n) for n in found}
    added, skipped = [], sorted(n for n in found if label(n) in taken)
    failed: dict[str, str] = {}
    # Pre-check names on every pass, dry-run included: a broken prefix must be
    # visible *before* --apply, not discovered by it.
    for name in found:
        if label(name) in taken:
            continue
        bad = _bad_name(label(name))
        if bad:
            failed[label(name)] = f"invalid: {bad}"
    if apply:
        for name in found:
            if label(name) in taken or label(name) in failed:
                continue
            try:
                ref = add(label(name))
            except federation.FederationConflict as exc:
                failed[label(name)] = f"conflict: {exc}"
            except ValueError as exc:
                failed[label(name)] = f"invalid: {exc}"
            else:
                if ref is not None:
                    added.append(label(name))
                else:
                    failed[label(name)] = "no root could be registered for it"
    return {"found": found, "plan": plan, "added": added,
            "skipped": sorted(set(skipped)), "failed": failed,
            "applied": apply}


def listing() -> list[dict]:
    """Every registered root, described as a profile.

    Roots registered before profiles existed appear here too — they are the
    same thing, and hiding them would suggest a second registry that does not
    exist.
    """
    here = config.memory_dir()
    out = []
    for ref in federation.iter_roots():
        kind = _KIND_FOR.get(ref.mode, ref.mode)
        out.append({
            "name": ref.label,
            "id": ref.id,
            "kind": kind,
            "path": str(ref.path),
            "in_use": federation._same_path(ref.memory_dir(), here) is True,
            "current": ref.is_current(),
        })
    return sorted(out, key=lambda p: (p["kind"], p["name"]))


def env_line(name: str) -> str | None:
    """The one environment change that makes a process use this profile.

    None when no profile goes by that name. The variable differs by shape
    because the shapes differ: a dedicated profile pins one directory, a
    shared one selects an instance whose memory is still per project.
    """
    ref = _only(name)
    if ref is None:
        return None
    # Quoted for a shell, because it is going into one. A memory directory can
    # hold a space, a quote, a dollar sign — and a line meant to be pasted or
    # eval'd would then set the wrong variable, or run whatever the path spells.
    var = "FOLDCRUMBS_DIR" if ref.mode == "explicit" else "CLAUDE_CONFIG_DIR"
    return f"export {var}={shlex.quote(str(ref.path))}"


def remove(name: str) -> bool:
    """Unregister a profile. Its memories are left exactly where they are.

    Removing a profile takes it out of the shared view; it is not a way to
    delete an agent's memory, and nothing here touches the store.
    """
    ref = _only(name)
    return federation.unregister(ref.id) if ref is not None else False
