"""Redirect every store-locating variable to a throwaway directory.

Imported first by each test module, **before** foldcrumbs, because config
resolves ``STATE_DIR`` at import time.

Two traps this exists to close, both of which made an earlier per-class fix
look sufficient:

* *Clearing the variables is not enough.* With nothing set, the state dir
  falls back to the real ``~/.foldcrumbs`` — so the suite would read the
  developer's federation registry and, through it, their actual stores.
* *The legacy spelling is not enough.* ``FOLDCRUMBS_*`` outranks ``ENGRAM_*``,
  so a class isolating only the latter is silently overridden by whatever is
  exported in the developer's shell. That is how the backend tests came to
  overwrite a real ``llm-backend`` choice.

Per-module isolation cannot fix this on its own: it depends on every module —
including ones written later — getting the precedence right. Doing it here
means a module only has to import this before foldcrumbs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

SANDBOX = Path(tempfile.mkdtemp(prefix="foldcrumbs_suite_"))

# Unset the memory-dir overrides so each test picks its own; pin the two that
# would otherwise resolve to something real.
for _var in ("FOLDCRUMBS_DIR", "ENGRAM_DIR", "ENGRAM_STATE_DIR"):
    os.environ.pop(_var, None)
os.environ["FOLDCRUMBS_STATE_DIR"] = str(SANDBOX / "state")
os.environ["CLAUDE_CONFIG_DIR"] = str(SANDBOX / "config")


def is_inside(path: os.PathLike[str] | str) -> bool:
    """True when a resolved path belongs to this sandbox."""
    return str(path).startswith(str(SANDBOX))
