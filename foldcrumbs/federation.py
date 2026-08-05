"""Root registry for federated memory.

Each CLI instance (``claude``, ``claude-work``, ``claude-peo``, …) owns its own
store under its own ``CLAUDE_CONFIG_DIR``. Federation keeps those stores
**separate and exclusively owned** — an instance only ever writes its own — and
adds a shared view on top so every instance can *see* what the others learned
about the same project.

Two artefacts, deliberately split so two *different* roots never write the same
file:

* **root marker** — ``<root>/.foldcrumbs-root``, a stable id living inside the
  root it identifies. Path hashes were rejected: they don't survive moving or
  renaming a config dir, and the id has to outlive that. Created with an atomic
  link so two processes racing on the same fresh root agree on one id instead of
  minting two.
* **registry shard** — ``<state-dir>/roots/<root-id>.json``, one file per root.
  A single shared manifest would put every instance on the same write path and
  reintroduce the lost-update race that sharding exists to remove.

The registry lives in ``config.STATE_DIR``, machine-local and shared by all
instances on the box in the normal configuration. ``FOLDCRUMBS_STATE_DIR`` can
move it per-instance, which silently splits federation into disjoint groups;
each marker records the registry it was registered into so the split is
detectable (``state_dir_conflict``) rather than merely invisible.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from foldcrumbs import config

try:  # POSIX only; the package must still import where it is missing.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

# Lives inside the root it names, so the id survives a move/rename.
ROOT_MARKER = ".foldcrumbs-root"

# Root ids become filenames under roots/. Anything outside this alphabet — a
# hand-edited or corrupted marker holding "../../x" — would let a read/write/
# unlink escape the registry directory, so ids are validated at every boundary
# rather than trusted because we minted them.
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

VALID_MODES = ("config", "explicit")

# What ``file_lock`` yields when the filesystem refuses locks outright but the
# caller opted into proceeding. Truthy, so "did I get in" still reads the same;
# distinguishable, so operations that genuinely need exclusion can decline.
DEGRADED = "degraded"


class FederationConflict(ValueError):
    """A registration that would silently reinterpret an existing root."""


def roots_dir() -> Path:
    """Registry directory: one JSON shard per registered root."""
    return config.STATE_DIR / "roots"


_ABSENT = object()      # the path is not there
_UNKNOWN = object()     # the filesystem would not say


def _identity(path: str):
    """``(device, inode)`` for a path, or ``_ABSENT`` / ``_UNKNOWN``.

    Identity comes from the filesystem rather than from a resolved name:
    ``realpath`` follows symlinks but leaves a bind mount's two names distinct,
    so one directory reached under each still compared as different.

    Absent and unknown are kept apart on purpose. A path that is *gone* is
    evidence; a path the filesystem declined to describe — unreadable, on a
    stalled mount — is not, and collapsing the two would make a slow disk look
    like a deletion.
    """
    def probe():
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return _ABSENT
        except OSError:
            return _UNKNOWN
        return (st.st_dev, st.st_ino)

    got = _bounded(probe, _REGISTRY_PROBE_TIMEOUT)
    return _UNKNOWN if got is None else got


def _same_path(a, b) -> bool | None:
    """Whether two paths name one directory. None when it cannot be told.

    Text first, then filesystem identity, so a directory reached through a
    symlink or an alternate spelling is recognised as itself. Two paths that
    are both missing are unknown, not equal: nothing was compared.
    """
    if str(a) == str(b):
        return True
    ia, ib = _identity(str(a)), _identity(str(b))
    if ia is _UNKNOWN or ib is _UNKNOWN:
        return None
    if ia is _ABSENT and ib is _ABSENT:
        return None
    if ia is _ABSENT or ib is _ABSENT:
        return False
    return ia == ib


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOCK_POLL_SECONDS = 0.02
_LOCK_WAIT_SECONDS = 5.0

# Errors that mean "someone else holds it, try again". Anything else is a
# filesystem that will not lock this file, where waiting changes nothing.
_CONTENDED_ERRNOS = frozenset(
    e for e in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EINTR)
    if e is not None
)


@contextlib.contextmanager
def _mkdir_lock(lockdir: Path, wait: float | None = None):
    """Portable mutual exclusion for platforms without ``fcntl``.

    ``mkdir`` is atomic and fails if the directory exists, which is all an
    exclusive lock needs.

    Ownership is carried by the *filename* of the marker inside, so releasing
    means unlinking a name only its own holder can have — no read-then-unlink
    window — and the final ``rmdir`` fails by itself if anyone else's marker is
    present.

    It deliberately does **not** break locks it considers stale. Age cannot
    distinguish a dead holder from a slow one, so stealing admits two live
    holders — and the sequence that follows loses data: the original writes a
    tombstone, the thief revokes it and republishes the shard, the original
    resumes and unlinks that shard, leaving a root that is neither removed nor
    registered. Since callers refuse to mutate without the lock, waiting and
    giving up is merely unavailable, which is recoverable; stealing is not.

    A holder killed mid-mutation therefore leaves the lock behind. That is a
    manual cleanup, and the log says exactly which directory to remove.
    """
    deadline = time.monotonic() + (
        _LOCK_WAIT_SECONDS if wait is None else wait)
    owner_name = f"owner-{uuid.uuid4().hex}"
    owner = lockdir / owner_name
    held = False

    def withdraw() -> None:
        """Undo a partial or abandoned acquisition.

        Both steps matter for *availability*: since stale locks are never
        broken, anything left behind here strands the registry permanently.
        Removing our own marker and then trying the directory means the last
        one out clears it, whether we failed mid-acquisition or backed off.
        """
        try:
            owner.unlink()
        except OSError:
            pass
        try:
            lockdir.rmdir()   # fails harmlessly if another marker is inside
        except OSError:
            pass

    while True:
        created = False
        try:
            # mkdir, not rename: rename *replaces* an empty directory, so a
            # holder whose owner file was deleted by hand could be displaced
            # while still inside its critical section. mkdir refuses whenever
            # the path exists at all, empty or not.
            lockdir.mkdir()
            created = True
            owner.write_text(_now_iso(), encoding="utf-8")
            # mkdir and the owner file are two steps, and a manual removal in
            # between could let a second holder take the directory this one is
            # about to write into. Verify instead of assuming: if anyone else's
            # marker is here, this is not our lock.
            intruders = [p for p in lockdir.glob("owner-*") if p.name != owner_name]
            if not intruders:
                held = True
                break
            config.log_event(f"federation: lost a race for {lockdir}; backing off")
            withdraw()
        except FileExistsError:
            pass
        except OSError:
            # A transient failure between mkdir and the marker would otherwise
            # leave a directory nobody owns and nobody may break — turning one
            # bad write into a registry that can never be mutated again.
            if created:
                withdraw()
            break
        if time.monotonic() > deadline:
            config.log_event(
                f"federation: registry lock {lockdir} is held; not mutating "
                "(if no foldcrumbs process is running, remove that directory)"
            )
            break
        time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield held
    finally:
        if held:
            # Same two safe steps as withdraw(): drop only this holder's own
            # marker, then remove the directory — which fails harmlessly if the
            # lock was cleared by hand and re-taken, because the new holder's
            # marker is still inside. No check-then-act either way.
            withdraw()


@contextlib.contextmanager
def file_lock(lock_path: Path, allow_unsupported: bool = False,
              wait: float | None = None):
    """Exclusive lock on one path, bounded in time. Yields True while held.

    Scoped deliberately: callers pass the narrowest path that covers what
    actually races. A single machine-wide lock would make one slow holder
    block every instance, and this is taken on the SessionStart path.

    The wait is bounded — ``flock`` is taken non-blocking and retried until a
    deadline — because an editor that will not start is a worse outcome than a
    publish that waits for the next session.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return

    if fcntl is not None:
        fh = None
        try:
            fh = lock_path.open("a+")
        except OSError:
            config.log_event(f"federation: cannot open lock {lock_path}")
            yield False
            return
        deadline = time.monotonic() + (
        _LOCK_WAIT_SECONDS if wait is None else wait)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                # Only contention is worth waiting out. ENOLCK, EINVAL and the
                # like mean this file will never be lockable — some network and
                # FUSE mounts simply do not support it — and retrying spends the
                # whole deadline on the session-start path for a result that
                # cannot change.
                if exc.errno not in _CONTENDED_ERRNOS:
                    fh.close()
                    if allow_unsupported:
                        # The filesystem refuses locks outright — some network
                        # and FUSE mounts do — but still honours the atomic
                        # link the marker is published with. Refusing here
                        # would make registration impossible on a root that
                        # worked before the lock existed; proceeding keeps
                        # create-once and loses only the wider atomicity.
                        config.log_event(
                            f"federation: {lock_path} cannot be locked "
                            f"({exc.strerror}); relying on create-once"
                        )
                        yield DEGRADED
                        return
                    config.log_event(
                        f"federation: {lock_path} cannot be locked "
                        f"({exc.strerror}); not mutating"
                    )
                    yield False
                    return
                if time.monotonic() > deadline:
                    fh.close()
                    config.log_event(
                        f"federation: gave up waiting for {lock_path}; not mutating"
                    )
                    yield False
                    return
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield True
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        return

    with _mkdir_lock(Path(str(lock_path) + ".d"), wait) as held:
        if not held:
            config.log_event(f"federation: could not lock {lock_path}; not mutating")
        yield held


@contextlib.contextmanager
def _registry_lock():
    """Serialize registry mutations across processes.

    register / unregister / repair each touch two files (shard and tombstone),
    and four instances plus their hooks can run at once. Ordering alone gets
    the crash cases right but still leaves interleavings where a removal is
    revoked by a repair that read stale state.

    Yields True only while exclusion is actually held. Callers **must** refuse
    to mutate on False: proceeding unlocked would reopen exactly the
    check-then-act window this exists to close. ``flock`` is preferred because
    the kernel releases it when a process dies; the mkdir fallback covers
    platforms without ``fcntl``.
    """
    # Pick the mechanism from the platform, never from whether this attempt
    # happened to succeed. Falling back after a failed flock would let one
    # process hold .lock while another holds .lock.d — two lock domains that
    # exclude nobody, which is worse than not locking at all. That choice
    # lives in file_lock; this only names the path.
    with file_lock(roots_dir() / ".lock") as held:
        yield held


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _write_json(target: Path, payload: dict) -> None:
    """Atomically replace ``target`` with ``payload`` (tmp + os.replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _label_for(path: Path) -> str:
    """Human label for a root: the dir's name without its leading dot."""
    return path.name.lstrip(".") or str(path)


def _abs(path: str | os.PathLike[str]) -> Path:
    """Absolute + normalised, without resolving symlinks.

    ``abspath`` (not ``resolve``): a relative path must not be stored, or the
    shard means something different from the next cwd; but symlinked config
    dirs are a deliberate user choice and following them would rewrite the
    path the user asked for.
    """
    return Path(os.path.abspath(os.path.expanduser(str(path))))


# --- root marker -----------------------------------------------------------


def read_marker_data(root_path: Path) -> dict | None:
    """Marker payload for ``root_path``, or None when absent/invalid."""
    data = _read_json(Path(root_path) / ROOT_MARKER)
    if not data or not valid_id(data.get("id")):
        return None
    if data.get("mode") not in VALID_MODES:
        data["mode"] = "config"
    return data


def read_marker(root_path: Path) -> str | None:
    data = read_marker_data(root_path)
    return data["id"] if data else None


def _marker_payload(root_path: Path, mode: str, rid: str | None = None) -> dict:
    return {
        "id": rid or _new_id(),
        "created_at": _now_iso(),
        "label": _label_for(root_path),
        "mode": mode,
        # Which registry this root federated into. A later run with a different
        # STATE_DIR can then say so instead of silently seeing fewer roots.
        "registry": str(config.STATE_DIR),
    }


def marker_lock_path(root_path: Path) -> Path:
    """Lock guarding a root's marker, kept beside the marker itself.

    Not the registry lock. The marker belongs to the *root*, and two processes
    can be pointed at different ``FOLDCRUMBS_STATE_DIR``s — exactly what a
    relocation is — in which case they take different registry locks and
    exclude nobody while replacing the same file. A lock that lives with the
    root is the only one both of them can agree on.
    """
    return Path(root_path) / f"{ROOT_MARKER}.lock"


def _publish_marker(
    root_path: Path, payload: dict, *, replace: bool, exclusive: bool
) -> dict | None:
    """Publish a marker create-once, returning whatever ended up on disk.

    ``os.link`` is the whole point: it fails if the target exists, so of N
    processes racing on one root exactly one wins and the losers adopt the
    winner's id. ``replace`` unlinks first (corrupt marker, detected clone) and
    still goes through link, so even the replacement path stays create-once
    rather than degrading to a last-writer-wins ``os.replace``.

    Returns None when nothing could be published — the caller must not invent
    an id that no marker records, or the next run mints a second one for the
    same root.
    """
    if replace and not exclusive:
        # The single place every rewrite passes through. Both flags are
        # keyword-only and required: a default would have made this gate
        # opt-in, and the value anyone would omit is the permissive one. Three branches learned
        # this rule one at a time and a fourth still missed it; enforcing it
        # here means the next one cannot. Creating is untouched — the atomic
        # link below is the whole guarantee for that.
        config.log_event(
            f"federation: refusing to replace the marker in {root_path} "
            "without exclusion")
        return None
    target = Path(root_path) / ROOT_MARKER
    try:
        fd, tmp = tempfile.mkstemp(dir=str(root_path), suffix=".tmp")
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        if replace:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        try:
            os.link(tmp, target)
        except FileExistsError:
            pass  # someone else won; fall through and adopt what they wrote
        except OSError:
            # Filesystems without hard links (some network/FUSE mounts) can't
            # give create-once semantics. Say so rather than silently racing.
            config.log_event(
                f"federation: cannot create {target} atomically on this filesystem"
            )
            return None
    except OSError:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return read_marker_data(root_path)


def ensure_marker(root_path: Path, mode: str = "config") -> dict | None:
    """Read the root's marker, creating one atomically on first use.

    None means no marker could be established — a caller must then refuse to
    register rather than proceed with an unpublished id.
    """
    existing = read_marker_data(root_path)
    if existing:
        return existing
    # Under the same lock replacements use, so creating a marker cannot land
    # inside another process's unlink/link gap. Taken once and held across
    # both steps below: file_lock opens a fresh descriptor each call, so a
    # nested acquisition in this process would deadlock on itself.
    with file_lock(marker_lock_path(root_path), allow_unsupported=True) as locked:
        if not locked:
            config.log_event(f"federation: cannot lock the marker in {root_path}")
            return None
        return _ensure_marker_locked(
            root_path, mode, exclusive=locked is not DEGRADED)


def _ensure_marker_locked(
    root_path: Path, mode: str = "config", *, exclusive: bool
) -> dict | None:
    """``ensure_marker`` body. The caller must hold the root's marker lock.

    ``exclusive`` is False on a filesystem that refuses locks. Creating still
    goes ahead — the atomic link is the whole guarantee there — but replacing
    a corrupt marker does not: that is a rewrite, and a rewrite without
    exclusion is the race the lock was added to close.
    """
    existing = read_marker_data(root_path)
    if existing:
        return existing      # someone published while we waited
    payload = _marker_payload(Path(root_path), mode)
    published = _publish_marker(root_path, payload, replace=False,
                                exclusive=exclusive)
    if published:
        return published
    # Nothing valid on disk: either the write failed, or an unparseable marker
    # is squatting the name. Replace it once, still create-once.
    if (Path(root_path) / ROOT_MARKER).exists():
        if not exclusive:
            config.log_event(
                f"federation: {root_path} has a corrupt marker that cannot be "
                "replaced safely without locking on this filesystem")
            return None
        config.log_event(f"federation: replacing corrupt marker in {root_path}")
        return _publish_marker(root_path, payload, replace=True,
                               exclusive=exclusive)
    return None


def _replace_marker_locked(
    root_path: Path, payload: dict, *, exclusive: bool
) -> dict | None:
    """Replace a marker. The caller must already hold the root's marker lock."""
    return _publish_marker(root_path, payload, replace=True,
                           exclusive=exclusive)


def replace_marker(root_path: Path, payload: dict) -> dict | None:
    """Replace a root's marker under the root's own lock.

    Every caller re-reads the result rather than assuming its own payload
    landed: the lock makes the replacement atomic against other foldcrumbs
    processes, not against a hand edit, and the id that matters is the one on
    disk afterwards.
    """
    # No degraded path: replacing is last-writer-wins without a lock, which
    # is exactly what the root lock exists to prevent. Creating is different —
    # the atomic link carries that on its own — so ensure_marker may proceed
    # where this one declines.
    with file_lock(marker_lock_path(root_path)) as locked:
        if not locked:
            config.log_event(
                f"federation: cannot replace the marker in {root_path} without "
                "locking")
            return None
        return _replace_marker_locked(root_path, payload, exclusive=True)


# --- current instance ------------------------------------------------------


def current_root_path() -> Path | None:
    """The root this process writes to.

    ``FOLDCRUMBS_DIR`` pins a single memory dir; that *is* the root (mode
    ``explicit``). Otherwise the root is this instance's config dir.
    """
    override = config._env("DIR")
    if override:
        return _abs(override)
    return _abs(config.claude_config_dir())


def current_mode() -> str:
    return "explicit" if config._env("DIR") else "config"


# --- registry --------------------------------------------------------------


@dataclass
class RootRef:
    """A registered memory root.

    ``mode`` is ``"config"`` for the normal ``<config-dir>/projects/<cwd>/memory``
    layout, or ``"explicit"`` when the root is one pinned directory that serves
    every cwd (``FOLDCRUMBS_DIR``), so there is nothing to derive.
    """

    id: str
    path: Path
    label: str
    mode: str = "config"
    registered_at: str = ""
    state_dir: str = ""

    def memory_dir(self, cwd: str | os.PathLike[str] | None = None) -> Path:
        if self.mode == "explicit":
            return self.path
        cwd = cwd or os.getcwd()
        return self.path / "projects" / config.encode_cwd(cwd) / "memory"

    def is_current(self) -> bool:
        """Identity, not path equality.

        The same root can be spelled several ways (``a/../b``, a symlink, a
        trailing slash), so compare marker ids and let the answer be
        independent of how the caller wrote CLAUDE_CONFIG_DIR.
        """
        cur = current_root_path()
        return cur is not None and self.id == read_marker(cur)

    def available_within(self, timeout: float) -> bool | None:
        """``available()`` bounded in time. None means the probe didn't answer.

        A root can live on a network mount, and ``stat`` on a hung one blocks
        indefinitely — inside a SessionStart hook that means an editor that
        will not start. There is no portable timeout for a stat, so it runs on
        a throwaway thread we simply stop waiting for.
        """
        import threading

        result: list[bool] = []

        def probe() -> None:
            result.append(self.available())

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(timeout)
        return result[0] if result else None

    def available(self) -> bool:
        """True when the root is reachable.

        Deliberately about the *root*, not the project dir inside it: a root
        with no memory for this project is available-and-empty, which must not
        be confused with one we cannot read at all (unmounted volume, deleted
        directory). The two demand opposite handling downstream. X_OK matters
        as much as R_OK — a directory must be traversable to reach the store.
        """
        try:
            return self.path.is_dir() and os.access(self.path, os.R_OK | os.X_OK)
        except OSError:
            return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": str(self.path),
            "label": self.label,
            "mode": self.mode,
            "registered_at": self.registered_at,
            "state_dir": self.state_dir,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RootRef | None":
        rid, path = data.get("id"), data.get("path")
        if not valid_id(rid) or not path:
            return None
        p = Path(path)
        mode = data.get("mode")
        return cls(
            id=str(rid),
            path=p,
            label=str(data.get("label") or _label_for(p)),
            mode=mode if mode in VALID_MODES else "config",
            registered_at=str(data.get("registered_at") or ""),
            state_dir=str(data.get("state_dir") or ""),
        )


def shard_path(root_id: str) -> Path | None:
    return roots_dir() / f"{root_id}.json" if valid_id(root_id) else None


def tombstone_path(root_id: str) -> Path | None:
    """Marker of a *deliberate* removal.

    Deleting the shard alone is not durable: the removed instance's own next
    command repairs it, and its marker makes ``iter_roots`` resynthesise it.
    A tombstone records that leaving was intentional, so only an explicit
    ``install`` / ``roots add`` brings the root back.
    """
    return roots_dir() / f"{root_id}.removed" if valid_id(root_id) else None


def read_tombstone(root_id: str) -> dict | None:
    p = tombstone_path(root_id)
    if p is None or not p.is_file():
        return None
    # Older tombstones held a bare timestamp; treat an unparseable one as a
    # removal with no metadata rather than as no removal at all.
    return _read_json(p) or {}


def is_tombstoned(root_id: str) -> bool:
    return read_tombstone(root_id) is not None


_MISSING = object()


def _home_registry(marker: dict) -> str | None:
    """Which registry owns this root, or None when that cannot be told.

    Absent is not malformed. A marker written before the field existed simply
    belongs to the registry reading it; one holding a list or a number belongs
    to nobody knowable — and collapsing the second case into the first is a
    fail-*open*, because "here" is exactly where a tombstone authorising a
    mode change would be found.
    """
    raw = marker.get("registry", _MISSING)
    if raw is _MISSING:
        return str(config.STATE_DIR)
    # Absolute, or unidentifiable. A configured registry always is — config
    # normalises it — so a relative value can only be hand-written or damaged,
    # and consumers resolve it against the caller's working directory: consent
    # for a mode change would then depend on where the command was run, and a
    # project-local `roots/<id>.removed` could authorise a reinterpretation.
    if isinstance(raw, str) and raw and os.path.isabs(raw):
        return raw
    return None


# A registry named by a marker can live on a mount that has stopped
# answering. Probing it is a courtesy to the caller, not a reason to hang a
# registration, so it is given a deadline like every other foreign read.
_REGISTRY_PROBE_TIMEOUT = 1.0


def _bounded(probe, timeout: float):
    """Run a filesystem probe with a deadline. None when it does not answer.

    Callers must test that None *explicitly*. It is a third state, not a falsy
    result: every probe here answers a question whose permissive branch is the
    dangerous one — "the original is gone", "no removal was recorded" — and a
    plain truth test hands an unanswered probe straight to it.


    ``except OSError`` does not bound a stalled call: a hung NFS or FUSE mount
    simply never returns. The probe runs on a throwaway thread we stop waiting
    for — the same trade the recall path makes, for the same reason.
    """
    import threading

    out: list = []

    def run() -> None:
        try:
            out.append(probe())
        except OSError:
            out.append(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return out[0] if out else None


def _removal_recorded(root_id: str, registry: str) -> bool | None:
    """Was this root's removal recorded in the registry that owns it?

    Consent has to be looked for where it would have been written, not where
    this process happens to be looking. The marker is inside the root, so it
    is shared by every registry: treating "not registered *here*" as licence
    to change the mode would re-address the memories of an instance elsewhere
    that still has the root live.

    None when that registry cannot be read — unverifiable, which is not the
    same as absent, and is treated as a refusal by the caller.
    """
    if not valid_id(root_id) or not isinstance(registry, str) or not registry:
        return None      # unverifiable, which the caller treats as a refusal
    try:
        d = Path(registry) / "roots"
    except (TypeError, ValueError):
        return None

    def probe():
        if not d.is_dir():
            return None
        return (d / f"{root_id}.removed").is_file()

    try:
        return _bounded(probe, _REGISTRY_PROBE_TIMEOUT)
    except (OSError, ValueError):
        return None


# One probe per root at a time, and its answer reused briefly. iter_roots()
# runs on every recall, so probing each root's marker afresh cost a full
# timeout *and* an abandoned thread per call on a hung mount — the cost grew
# with traffic, which is exactly backwards.
_MARKER_PROBE_TTL = 30.0
_marker_probes: dict[str, dict] = {}
_marker_probe_lock = threading.Lock()


def _cached_home_registry(root_path: Path) -> str | None:
    """Which registry this root claims, reusing a recent or in-flight answer.

    None means "could not tell", and callers must read it as such rather than
    as a move: a slow mount is not a relocation.
    """
    key = str(root_path)

    def probe(mine: threading.Thread) -> None:
        # Publishes its own answer, however late it arrives. A read that misses
        # the timeout still finishes eventually, and dropping what it learned
        # left the cache serving "could not tell" for the rest of the TTL.
        value = _home_registry(read_marker_data(root_path) or {})
        with _marker_probe_lock:
            entry = _marker_probes.get(key)
            if entry is not None and entry["thread"] is mine:
                entry.update(value=value, at=time.monotonic(), thread=None)

    with _marker_probe_lock:
        entry = _marker_probes.get(key)
        if entry is not None:
            if entry["thread"] is not None:
                # Still blocked from an earlier attempt. Reuse what it last
                # said rather than stacking another thread behind it.
                return entry["value"]
            if time.monotonic() - entry["at"] < _MARKER_PROBE_TTL:
                return entry["value"]
        # Reserved before the worker starts, and under the same lock that
        # decided to start one: released any earlier, two concurrent recalls
        # would each find no entry and each spawn a probe of their own.
        worker = threading.Thread(target=lambda: probe(worker), daemon=True)
        _marker_probes[key] = {
            "value": entry["value"] if entry else None,
            "at": time.monotonic(),
            "thread": worker,
        }
    worker.start()
    worker.join(_REGISTRY_PROBE_TIMEOUT)
    with _marker_probe_lock:
        entry = _marker_probes.get(key)
        return entry["value"] if entry else None


def _registers(shard: object, root_id: str, root_path: Path) -> bool:
    """Whether a registry shard is the registration for this root, here.

    The one question a departure may act on: not "is there a file", but "does
    this registry say it holds *this* root at *this* path". Anything else —
    absent, malformed, another root, another path — is not ours to withdraw.
    """
    if not isinstance(shard, dict) or shard.get("id") != root_id:
        return False
    return _same_path(shard.get("path", ""), root_path) is True


# Registry-alias verdicts, reused briefly. iter_roots() runs on every recall,
# and an identity probe per root per recall is the very cost the marker cache
# exists to avoid. The common case never reaches this: _same_path answers on
# string equality alone, so only a root that genuinely names another spelling
# is ever probed, and few do.
_registry_aliases: dict[tuple[str, str], dict] = {}


def _registry_is_ours(home: str) -> bool | None:
    """Whether a marker's registry is the one we are reading from.

    None means "could not tell", and callers must read it as such: a slow
    mount is not a relocation.
    """
    key = (home, str(config.STATE_DIR))
    if key[0] == key[1]:
        return True
    now = time.monotonic()
    with _marker_probe_lock:
        entry = _registry_aliases.get(key)
        if entry is not None and now - entry["at"] < _MARKER_PROBE_TTL:
            return entry["value"]
    value = _same_path(home, config.STATE_DIR)
    with _marker_probe_lock:
        _registry_aliases[key] = {"value": value, "at": time.monotonic()}
    return value


def _leave_registry(root_id: str, registry: str, root_path: Path) -> None:
    """Best-effort: tell the registry we are leaving that we have left.

    Readers there settle it on their own by reading the marker, so this is not
    the guarantee — it is what makes the change take effect *now* rather than
    at their next look. Failure is expected and harmless: the old registry may
    be unreachable, read-only, or busy.

    Bounded, because "best effort" describes the *outcome*, not the time. Every
    step here touches the registry being left, which can be a mount that has
    stopped answering — and an unbounded courtesy would hang the relocation it
    was meant to tidy up after.
    """
    def attempt() -> bool:
        # Only a registry that is provably *another* directory. The two paths
        # can be aliases of one directory — a symlinked FOLDCRUMBS_STATE_DIR,
        # a different spelling — and then this courtesy would tombstone and
        # delete the shard registration had just written for this very root.
        if _same_path(registry, config.STATE_DIR) is not False:
            config.log_event(
                f"federation: {registry} is not a different registry from "
                f"{config.STATE_DIR}; not recording a departure from it")
            return False
        roots = Path(registry) / "roots"
        # Checked *before* the lock, because taking one is itself a write:
        # file_lock creates the directory and the lock file, so a crafted
        # marker naming any path had this function build a tree there before
        # a single validation had run. Reading proves nothing on its own —
        # the shard can change before we hold the lock — so the same check
        # runs again below, under it. This one exists to keep an unrelated
        # directory untouched.
        if not _registers(_read_json(roots / f"{root_id}.json"),
                          root_id, root_path):
            return False
        with file_lock(roots / ".lock") as locked:
            if not locked:
                return False
            # Re-read under the lock, and check *identity* before position.
            # This runs on a thread the relocation stopped waiting for, so it
            # can acquire the lock long afterwards — by then the root may have
            # been registered back here, may have moved on again, or another
            # root entirely may occupy the path this call captured. Only a
            # marker that is still this root, and still says it lives
            # elsewhere, licenses the write.
            current = read_marker_data(root_path) or {}
            if current.get("id") != root_id:
                config.log_event(
                    f"federation: {root_path} no longer holds {root_id}; not "
                    "recording the earlier departure")
                return False
            belongs = _home_registry(current)
            if belongs is not None and _same_path(belongs, registry) is True:
                config.log_event(
                    f"federation: {root_id} belongs to {registry} again; "
                    "not recording the earlier departure")
                return False
            shard, tomb = roots / f"{root_id}.json", roots / f"{root_id}.removed"
            # The registry path comes from the marker, which is a file in the
            # root — hand-editable, and not ours. Before writing or deleting
            # anything there, require the registration this call claims to be
            # withdrawing: a shard that names this root at this path. Without
            # it, a crafted marker pointed this function at any directory and
            # had it create a lock and a tombstone there and unlink a JSON
            # file, all outside the configured state directory.
            if not _registers(_read_json(shard), root_id, root_path):
                config.log_event(
                    f"federation: {registry} no longer registers {root_id} at "
                    f"{root_path}; leaving it untouched")
                return False
            _write_json(tomb, {"removed_at": _now_iso(), "id": root_id,
                               "path": str(root_path),
                               "reason": f"moved to {config.STATE_DIR}"})
            shard.unlink(missing_ok=True)
        # Outside the roots lock, each under its own. A root that leaves and
        # later returns clears its tombstone here, which would make every
        # project shard it left behind valid again by id and memory path —
        # advertising memories that have since been changed or deleted, until
        # each project happened to republish.
        from foldcrumbs import index_shard
        left = index_shard.drop_root_shards_in(Path(registry), root_id)
        if left:
            config.log_event(
                f"federation: dropped {left} project shard(s) of {root_id} "
                f"left behind in {registry}")
        return True

    if _bounded(attempt, _REGISTRY_PROBE_TIMEOUT):
        config.log_event(
            f"federation: recorded {root_path}'s departure in {registry}")
    else:
        config.log_event(
            f"federation: could not record {root_path}'s departure in "
            f"{registry}; its readers will settle it themselves")


def _adopt_checked(
    marker: dict, requested: str | None, root_path: Path
) -> tuple[str, str]:
    """Adopt the marker that landed, and hold it to what the caller asked for.

    Every replacement can return a winner that is not the payload we sent, so
    every one of them has to be re-validated. Three separate call sites each
    learned that the hard way and one at a time; validating inside the
    adoption is what stops the next branch from missing it again.
    """
    rid, recorded = _adopt(marker)
    if requested is not None and recorded != requested:
        raise FederationConflict(
            f"{root_path} ended up registered as mode {recorded!r} while this "
            f"call asked for {requested!r}; another process won the marker. "
            "Try again."
        )
    return rid, recorded


def _adopt(marker: dict) -> tuple[str, str]:
    """Read identity *and* mode from the marker that actually landed.

    ``replace_marker`` returns whatever is on disk afterwards, which need not
    be the payload we sent — another process can publish in between, and
    during a state-dir relocation the two are not even holding the same lock.
    Taking only the id from it and keeping our own mode was the same mistake
    one field over: the shard would then tell readers to look for this root's
    memory in a directory it does not use.
    """
    return marker["id"], marker.get("mode", "config")


def _detect_clone(
    rid: str, root_path: Path, registry: str | None = None
) -> bool | None:
    """True when ``root_path`` is a *copy* of an already-registered root.

    Returns None when identity cannot be established — the caller must then
    refuse to register rather than pick a destructive default.

    Copying a config dir (``cp -r ~/.claude ~/.claude-copy``) duplicates the
    marker, so two live roots would claim one id and fight over one shard. A
    move is the same situation minus the original, so the distinguishing test
    is whether the previously registered path still carries that id.

    ``registry`` is where to look for that prior record: the root's *home*,
    not necessarily the one this process is using. A copy registered from an
    instance pointed at a different state dir found no shard and no tombstone
    here, concluded there was nothing to clone, and kept the original's id —
    leaving two live roots claiming one identity.
    """
    if registry is None:
        registry = str(config.STATE_DIR)
    if not valid_id(rid):
        return None

    def _at(suffix: str):
        """_MISSING, {} for present-but-unreadable, the payload, or None on
        a probe that did not answer. The three cases are not the same: absent
        means nothing to clone, unreadable means we cannot tell."""
        path = Path(registry) / "roots" / f"{rid}{suffix}"

        def probe():
            if not path.is_file():
                return _MISSING
            return _read_json(path) or {}

        return _bounded(probe, _REGISTRY_PROBE_TIMEOUT)

    prior = _at(".json")
    if prior is None or prior == {}:
        return None          # unreadable or unreachable: cannot tell
    if prior is _MISSING:
        # A removed root leaves no shard, but its identity is still live on
        # disk: without this the copy of an unregistered root would claim the
        # original's id. The tombstone carries the path for exactly this.
        tomb = _at(".removed")
        if tomb is _MISSING:
            return False  # nothing ever registered this id: nothing to clone
        if tomb is None or not tomb.get("path"):
            # Unreachable, unreadable, or written before tombstones carried
            # metadata. Fail closed: we cannot tell a copy from a rejoin, and
            # guessing wrong hands two live roots the same id.
            return None
        prior = tomb
    old = prior.get("path")
    if not old or Path(old) == root_path:
        return False
    # The registry answered, but the root it points at can live on a different
    # mount — and one that has stopped answering. Both probes below are
    # bounded: unbounded, they hung a registration with the marker lock held.
    old_id = _bounded(lambda: read_marker(Path(old)) or _MISSING,
                      _REGISTRY_PROBE_TIMEOUT)
    if old_id is None:
        config.log_event(
            f"federation: {old} did not answer while checking whether "
            f"{root_path} is a copy of it")
        return None                     # cannot tell: fail closed
    if old_id is _MISSING:
        # No marker — but the old root may simply be mid-replacement: the
        # publication below unlinks before it links, and this probe holds the
        # *copy's* lock, not the original's. A vanished directory is a genuine
        # move; a directory still sitting there without a marker is a moment we
        # cannot interpret, so it is refused rather than read as consent to
        # keep the id.
        still_there = _bounded(lambda: Path(old).is_dir(),
                               _REGISTRY_PROBE_TIMEOUT)
        if still_there is None:
            # Unanswered, not absent. A plain truth test read this as "gone" —
            # the permissive answer — so a hung mount handed the copy the
            # original's identity.
            config.log_event(
                f"federation: {old} did not answer; cannot tell whether "
                f"{root_path} is a copy of it")
            return None
        if still_there:
            config.log_event(
                f"federation: {old} exists but has no readable marker; cannot "
                f"tell whether {root_path} is a copy of it")
            return None
        return False  # the old path is gone: this is that move, not a copy
    if old_id != rid:
        return False  # the old path holds a different root now
    try:
        # A symlink or bind mount reaches one physical root by two names. Paths
        # are stored unresolved on purpose, so identity has to be settled by
        # inode: treating an alias as a copy would re-id the original root.
        same = _bounded(lambda: os.path.samefile(old, root_path),
                        _REGISTRY_PROBE_TIMEOUT)
        if same is None:
            raise OSError("identity probe did not answer")
        return not same
    except OSError:
        # Unknown, and both answers are destructive: "clone" rewrites a live
        # root's marker, "not a clone" lets this registration overwrite the
        # original's shard with the wrong path. Refuse instead.
        config.log_event(
            f"federation: cannot tell whether {root_path} is a copy of {old}"
        )
        return None


def register(
    root_path: Path | None = None,
    mode: str | None = None,
    label: str | None = None,
    *,
    relocate: bool = True,
) -> RootRef | None:
    """Register a root so other instances can see it. Idempotent.

    Returns None when the root can't be represented or written. Re-registering
    refreshes ``path``/``label``/``state_dir`` but never the id, so a root that
    moved keeps its identity and its shard.

    Raises ``FederationConflict`` when the requested mode contradicts the one
    already recorded: the same directory read as ``config`` and as ``explicit``
    means two different stores, and silently switching would move every
    memory's address.
    """
    with _registry_lock() as locked:
        if not locked:
            return None
        return _register_locked(root_path, mode, label, relocate=relocate)


def _register_locked(
    root_path: Path | None,
    mode: str | None,
    label: str | None,
    *,
    relocate: bool,
) -> RootRef | None:
    explicit_path = root_path is not None
    root_path = root_path if explicit_path else current_root_path()
    if root_path is None:
        return None
    root_path = _abs(root_path)

    # A manually named path is a config root unless the caller says otherwise;
    # only the *current* root inherits this process's FOLDCRUMBS_DIR mode.
    want_mode = mode or ("config" if explicit_path else current_mode())
    if want_mode not in VALID_MODES:
        raise FederationConflict(f"unknown root mode {want_mode!r}")

    try:
        root_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # Held across the whole registration, not around each marker write. The
    # marker and the shard were two steps under two different locks, so another
    # process could replace the marker in between and the shard would then be
    # published under an identity that had already been superseded.
    with file_lock(marker_lock_path(root_path),
                   allow_unsupported=True) as marker_held:
        if not marker_held:
            config.log_event(f"federation: cannot lock the marker in {root_path}")
            return None
        return _register_with_marker(
            root_path, want_mode, mode, label,
            exclusive=marker_held is not DEGRADED, relocate=relocate)


def _register_with_marker(
    root_path: Path, want_mode: str, mode: str | None, label: str | None,
    *, exclusive: bool, relocate: bool,
) -> RootRef | None:
    """Body of a registration. The caller holds the root's marker lock.

    ``exclusive`` is False when the filesystem refuses locks. Creating a marker
    is still safe there — the atomic link gives create-once on its own — but
    *replacing* one is last-writer-wins, so the branches that rewrite it
    decline instead of racing. A first registration therefore still works on
    such a root; a relocation, mode change or clone re-id waits for a
    filesystem that can lock.
    """
    def _needs_exclusion(what: str) -> None:
        config.log_event(
            f"federation: {root_path} needs {what}, which cannot be done "
            "safely without locking on this filesystem"
        )
    try:
        marker = _ensure_marker_locked(root_path, want_mode, exclusive=exclusive)
    except OSError:
        return None
    if marker is None:
        return None

    rid = marker["id"]
    recorded_mode = marker.get("mode", "config")
    # Refuse before touching anything. The relocation below rewrites the
    # marker, so raising afterwards left a rejected registration having moved
    # the root into the new registry with no shard in it — the old registry
    # reporting a split for a change the caller was told did not happen.
    # Consent is looked for in the registry the marker names — the root's home
    # — read before anything below can refresh it. Two reasons: the marker is
    # shared by every registry, so local ignorance is no licence to re-address
    # someone else's live root; and refusing before any mutation is what makes
    # a retry behave the same as the first attempt.
    home_registry = _home_registry(marker)
    if (mode is not None and mode != recorded_mode
            and (home_registry is None
                 or _removal_recorded(rid, home_registry) is not True)):
        raise FederationConflict(
            f"{root_path} is registered as mode {recorded_mode!r}; refusing "
            f"to reinterpret it as {mode!r} without a recorded removal in "
            f"{home_registry or 'an identifiable registry'} (`roots remove "
            f"{rid}` there, then re-add with --mode {mode})"
        )
    # Before the relocation, and against the root's *home* registry. Running
    # after meant the copy's marker had already been rewritten into this
    # registry, where no prior shard or tombstone exists — so the check
    # concluded there was nothing to clone and both roots kept one identity.
    # Everything the marker needs to say is decided here, then written once.
    #
    # There is deliberately no rollback after it lands. Undoing a marker is
    # itself a replacement, and one that can fail exactly as the shard write
    # just did — leaving a root whose recovery depends on a step that already
    # proved unreliable. The design recovers *forwards* instead: the marker
    # now describes the state the caller asked for, so the next registration
    # sees nothing left to change and only has to publish the shard.
    # ``test_a_failed_shard_write_is_completed_by_the_next_attempt`` holds that
    # convergence, which is the property rollback would otherwise provide.
    # Re-identifying a copy, relocating, and changing the mode used to be three
    # replacements in a row: whichever ran first mutated the marker, and a
    # failure in a later one left the root half-changed — a new id carrying an
    # old mode, or a moved registry the caller was told had not moved.
    verdict = _detect_clone(rid, root_path, home_registry)
    if verdict is None:
        config.log_event(
            f"federation: refusing to register {root_path} — its identity is "
            f"ambiguous against the root already holding id {rid}"
        )
        return None

    is_copy = bool(verdict)
    wants_mode_change = mode is not None and mode != recorded_mode
    # By identity, not by spelling: two aliases of one state directory — a
    # symlinked FOLDCRUMBS_STATE_DIR, a different spelling of the same path —
    # read as a move away from ourselves, and the departure that followed
    # deleted the shard this very registration had just written.
    needs_relocation = (home_registry is None
                        or _same_path(home_registry, config.STATE_DIR) is not True)
    if needs_relocation and not relocate:
        # Repair is not migration. ensure_registered() runs on every CLI
        # command, so allowing it here meant `foldcrumbs status` silently moved
        # a root into whatever registry happened to be configured — hiding the
        # federation it came from and switching off the very warning that would
        # have explained the change. Moving is an explicit act: install, or
        # `roots add`.
        config.log_event(
            f"federation: not repairing {root_path} here — its marker names "
            f"{home_registry}, and moving it is an explicit act"
        )
        return None
    if is_copy or wants_mode_change or needs_relocation:
        what = ", ".join(w for w, needed in (
            ("re-identifying a copy", is_copy),
            ("a relocation", needs_relocation),
            ("a mode change", wants_mode_change),
        ) if needed)
        if not exclusive:
            _needs_exclusion(what)
            return None
        final_mode = mode if wants_mode_change else recorded_mode
        # A copy must not keep the original's identity, so its payload carries
        # no id and _marker_payload mints one.
        payload = _marker_payload(root_path, final_mode,
                                  None if is_copy else rid)
        fresh = _replace_marker_locked(root_path, payload, exclusive=exclusive)
        if fresh is None:
            config.log_event(
                f"federation: {root_path} not registered — {what} could not "
                "be written"
            )
            return None
        marker = fresh
        # Identity *and* mode from what landed, held to what was asked for:
        # another process can win the replacement, and during a relocation the
        # two registries do not even share a lock.
        rid, recorded_mode = _adopt_checked(marker, mode, root_path)
        # By identity: a state directory reached through a symlink is still
        # the one we are registering into, and reading the spelling alone
        # refused every registration made through the other name.
        if _registry_is_ours(str(marker.get("registry") or "")) is not True:
            config.log_event(
                f"federation: {root_path} not registered — its marker now "
                f"names {marker.get('registry')}"
            )
            return None
        config.log_event(
            f"federation: {root_path} completed {what} as {rid} "
            f"({recorded_mode}) in {config.STATE_DIR}"
        )
        if needs_relocation and home_registry:
            _leave_registry(rid, home_registry, root_path)
        if wants_mode_change:
            # The move invalidates every shard this root has published, in
            # every project. Readers refuse them anyway, but refusal is
            # permanent for a project that never opens again.
            try:
                from foldcrumbs import index_shard
                index_shard.drop_stale_shards(
                    RootRef(id=rid, path=root_path,
                            label=label or _label_for(root_path),
                            mode=recorded_mode))
            except Exception:  # noqa: BLE001 - housekeeping, never fatal
                config.log_event(
                    f"federation: could not tidy {root_path}'s old shards")
    if want_mode != recorded_mode:
        # Not an error: the recorded mode wins, so no memory changes address.
        # Still worth an audit line — the caller asked for something else.
        config.log_event(
            f"federation: {root_path} stays mode {recorded_mode!r} "
            f"(this run would have chosen {want_mode!r})"
        )

    shard = shard_path(rid)
    if shard is None:
        return None
    existing = _read_json(shard) or {}
    ref = RootRef(
        id=rid,
        path=root_path,
        label=label or _label_for(root_path),
        mode=recorded_mode,
        # Keep the original registration time: it is the root's own age, not
        # the age of this refresh.
        registered_at=str(existing.get("registered_at") or _now_iso()),
        state_dir=str(config.STATE_DIR),
    )
    # Revoke the tombstone *before* publishing the shard. A crash in between
    # then leaves neither, which the repair path corrects back to registered —
    # the state the caller was asking for. The reverse order would leave a
    # shard shadowed by a live tombstone, i.e. silently still removed.
    tomb = tombstone_path(rid)
    if tomb is not None and tomb.exists():
        try:
            tomb.unlink()
        except OSError:
            config.log_event(
                f"federation: cannot revoke the removal of {rid}; not registering"
            )
            return None
    try:
        _write_json(shard, ref.to_dict())
    except OSError:
        return None
    return ref


def register_current(label: str | None = None) -> RootRef | None:
    """Self-registration for install and for first CLI use after an upgrade."""
    try:
        return register(label=label)
    except (FederationConflict, OSError):
        return None


def ensure_registered() -> RootRef | None:
    """Repair a missing registry shard for an already-opted-in root.

    Deliberately never *creates* a marker: joining the federation is an
    explicit act (``install`` or ``roots add``), so a root the user removed
    stays removed instead of resurrecting on the next command. What this does
    cover is a shard lost to a wiped state dir, and an install whose shard
    predates a rename.
    """
    cur = current_root_path()
    if cur is None:
        return None
    with _registry_lock() as locked:
        if not locked:
            return None
        marker = read_marker_data(cur)
        if not marker:
            return None
        if is_tombstoned(marker["id"]):
            return None  # removal was deliberate; only an explicit add undoes it
        shard = shard_path(marker["id"])
        if shard is not None and shard.is_file():
            return None
        # Same lock as the check above, so a removal committed a moment ago
        # cannot be revoked by a repair that read stale state.
        try:
            # Never relocates: this path exists to put back a missing shard,
            # not to move a root between registries.
            return _register_locked(None, None, None, relocate=False)
        except FederationConflict:
            return None


def iter_roots() -> list[RootRef]:
    """Every registered root, current instance first, then stable by label+id.

    A shard that is unreadable, or whose filename disagrees with the id inside
    it, is skipped and logged: a corrupt registry entry must degrade recall,
    not take it down.
    """
    d = roots_dir()
    refs: list[RootRef] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            data = _read_json(p)
            ref = RootRef.from_dict(data) if data else None
            if ref is None or ref.id != p.stem:
                config.log_event(f"federation: ignoring bad registry shard {p}")
                continue
            # A shard left behind by a failed removal is still removed.
            if is_tombstoned(ref.id):
                continue
            # A root that has moved to another registry is not ours to serve.
            # Relocation cannot always reach the registry it leaves — it may be
            # unreachable, or locked — so the readers of that registry settle
            # it themselves. Only a marker positively naming somewhere else
            # drops the root: unreadable is not proof of a move, and hiding a
            # root because its mount was briefly slow would be worse.
            # By identity, not by spelling. Registration already treats two
            # names for one state directory as one registry and leaves the
            # marker's original spelling alone — so comparing text here read
            # every root as relocated and emptied federation outright.
            home = _cached_home_registry(ref.path)
            if home is not None and _registry_is_ours(home) is False:
                config.log_event(
                    f"federation: {ref.label} now belongs to {home}; not "
                    "serving it from here")
                continue
            refs.append(ref)

    # An instance that hasn't run install yet still belongs in its own view.
    cur = current_root_path()
    if cur is not None:
        marker = read_marker_data(cur)
        if marker and is_tombstoned(marker["id"]):
            marker = None
        if marker and not any(r.id == marker["id"] for r in refs):
            refs.append(
                RootRef(
                    id=marker["id"],
                    path=cur,
                    label=_label_for(cur),
                    mode=marker.get("mode", "config"),
                )
            )

    refs.sort(key=lambda r: (r.label, r.id))
    refs.sort(key=lambda r: not r.is_current())
    return refs


def get_root(root_id: str) -> RootRef | None:
    """Look up one registered root. A tombstoned root is not registered."""
    shard = shard_path(root_id)
    data = _read_json(shard) if shard else None
    if not data or is_tombstoned(root_id):
        return None
    return RootRef.from_dict(data)


def unregister(root_id: str) -> bool:
    """Drop a root from the shared view. The store itself is left untouched.

    Writes a tombstone alongside removing the shard. Deleting the shard alone
    is not durable in either direction: the removed instance's next command
    would repair it, and for the current root ``iter_roots`` would resynthesise
    it from its marker. The tombstone records intent, and nobody's memory
    directory is written — not even our own marker, so a root that rejoins
    later keeps the identity it always had.
    """
    shard = shard_path(root_id)
    tomb = tombstone_path(root_id)
    if shard is None or tomb is None:
        return False
    with _registry_lock() as locked:
        if not locked:
            return False
        ref = get_root(root_id)
        cur = current_root_path()
        known = shard.is_file() or (cur is not None and read_marker(cur) == root_id)
        if not known:
            return False
        # Tombstone first, shard second. Readers ignore a tombstoned shard, so
        # a crash between the two still hides the root; the reverse order would
        # report a removal that the next repair quietly undoes.
        path = str(ref.path) if ref is not None else (
            str(cur) if cur is not None else "")
        try:
            _write_json(
                tomb,
                {"removed_at": _now_iso(), "id": root_id, "path": path},
            )
        except OSError:
            config.log_event(f"federation: could not record the removal of {root_id}")
            return False
        try:
            shard.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Harmless: the tombstone already hides it, and the stale shard is
            # cleared by the next successful removal or registration.
            config.log_event(f"federation: tombstoned {root_id}, shard left behind")
    label = ref.label if ref is not None else root_id
    config.log_event(f"federation: unregistered {label} ({root_id})")
    return True


def mode_conflict() -> str | None:
    """Describe a root whose recorded mode disagrees with this process.

    ``config.memory_dir()`` follows the live ``FOLDCRUMBS_DIR`` while the
    federated view uses the mode recorded in the marker. When the two diverge
    this instance reads and writes one store while the shared view advertises
    another. The registry deliberately keeps the recorded mode — re-addressing
    every memory behind the user's back is worse — so the divergence has to be
    said out loud instead.
    """
    cur = current_root_path()
    marker = read_marker_data(cur) if cur is not None else None
    if not marker:
        return None
    recorded = marker.get("mode", "config")
    live = current_mode()
    if live == recorded:
        return None
    return (
        f"{cur} is federated as mode {recorded!r} but this process runs as "
        f"{live!r} (FOLDCRUMBS_DIR): other instances will look for this "
        f"project's memory in the {recorded!r} location, not the one being "
        f"written now — unset FOLDCRUMBS_DIR, or `roots remove` and re-add "
        f"with --mode {live}"
    )


def state_dir_conflict() -> str | None:
    """Describe a split federation, or None when everything agrees.

    Two ways it shows up, and both are checked because neither alone is
    enough. A registered root can carry a different ``state_dir`` (visible
    here). And *this* instance's marker can name a registry other than the one
    in use — the case where roots registered elsewhere are not merely
    different but wholly invisible, which no scan of this registry could find.
    """
    here = str(config.STATE_DIR)
    # Three states, reported as three. Only a provably *different* directory
    # is a split: registration treats two spellings of one state directory as
    # one registry and keeps each marker's original wording, so comparing text
    # reported a split — and roots "invisible" — in a setup that works. But a
    # registry we could not reach is not thereby ours, and folding that into
    # "fine" hid real splits behind an unreachable mount. This is a status
    # report: saying "cannot tell" costs a line, saying nothing costs the
    # user the one signal that would have explained missing memories.
    strays, unreachable = set(), set()

    def classify(where: str) -> None:
        verdict = _registry_is_ours(where)
        if verdict is False:
            strays.add(where)
        elif verdict is None:
            unreachable.add(where)

    for r in iter_roots():
        if r.state_dir:
            classify(r.state_dir)
    cur = current_root_path()
    marker = read_marker_data(cur) if cur is not None else None
    # Through the same reader the mode guard uses: a hand-edited marker can
    # hold a list or a number here, and this used to put it straight into a
    # set and a join — unhashable or untypeable, so a status report crashed
    # instead of reporting. None means unidentifiable, which is worth saying.
    prior = _home_registry(marker) if marker else None
    if marker and prior is None:
        return (
            f"{cur} has an unreadable registry field in its "
            f"{ROOT_MARKER} — federation cannot tell which registry owns it; "
            "re-run `foldcrumbs install` from this instance to rewrite it"
        )
    if prior:
        classify(prior)
    # Both are said when both are true. Reporting only the confirmed split
    # buried the unreachable ones behind it — and those are the ones the user
    # cannot see for themselves.
    # Each finding carries its own consequence, because they are not the same
    # finding. A confirmed split earns the instruction to fix the config; an
    # unreachable registry has not been shown to be a different one at all,
    # and telling the user to go change their setup over it would be advice
    # the check never established.
    parts = []
    if strays:
        parts.append(
            f"roots registered against a different state dir: "
            f"{', '.join(sorted(strays))} — set FOLDCRUMBS_STATE_DIR "
            "consistently or those roots stay invisible here"
        )
    if unreachable:
        parts.append(
            f"could not reach {', '.join(sorted(unreachable))} to tell whether "
            "it is this registry — if it turns out to be a different one, "
            "roots registered there stay invisible here"
        )
    if not parts:
        return None
    return f"{'; '.join(parts)} (this instance uses {here})"
