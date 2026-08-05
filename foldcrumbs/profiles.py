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


def add(name: str, kind: str = DEDICATED, path: str | os.PathLike[str] | None = None):
    """Register a profile. Returns its root, or None if it could not be made.

    A dedicated profile invents its directory when none is given; a shared one
    must be told which config directory it stands for, because that directory
    is someone else's and guessing at it would be a decision, not a default.
    """
    if kind not in _MODE_FOR:
        raise ValueError(f"not a profile shape: {kind}")
    if not name or "/" in name or os.sep in name or name.startswith("."):
        raise ValueError(f"not usable as a profile name: {name!r}")
    if path is None:
        if kind == SHARED:
            raise ValueError(
                "a shared profile needs the config directory it stands for")
        target = default_path(name)
        target.mkdir(parents=True, exist_ok=True)
    else:
        target = Path(os.path.abspath(os.path.expanduser(str(path))))
    return federation.register(target, mode=_MODE_FOR[kind], label=name)


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
    for ref in federation.iter_roots():
        if ref.label != name:
            continue
        if ref.mode == "explicit":
            return f'export FOLDCRUMBS_DIR="{ref.path}"'
        return f'export CLAUDE_CONFIG_DIR="{ref.path}"'
    return None


def remove(name: str) -> bool:
    """Unregister a profile. Its memories are left exactly where they are.

    Removing a profile takes it out of the shared view; it is not a way to
    delete an agent's memory, and nothing here touches the store.
    """
    for ref in federation.iter_roots():
        if ref.label == name:
            return federation.unregister(ref.id)
    return False
