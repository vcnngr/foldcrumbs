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
import json
import os
import re
import tempfile
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


class FederationConflict(ValueError):
    """A registration that would silently reinterpret an existing root."""


def roots_dir() -> Path:
    """Registry directory: one JSON shard per registered root."""
    return config.STATE_DIR / "roots"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOCK_POLL_SECONDS = 0.02
_LOCK_WAIT_SECONDS = 5.0


@contextlib.contextmanager
def _mkdir_lock(lockdir: Path):
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
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
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
    try:
        roots_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return

    # Pick the mechanism from the platform, never from whether this attempt
    # happened to succeed. Falling back after a failed flock would let one
    # process hold .lock while another holds .lock.d — two lock domains that
    # exclude nobody, which is worse than not locking at all.
    if fcntl is not None:
        fh = None
        try:
            fh = (roots_dir() / ".lock").open("a+")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            if fh is not None:
                fh.close()
            config.log_event("federation: could not lock the registry; not mutating")
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()
        return

    with _mkdir_lock(roots_dir() / ".lock.d") as held:
        if not held:
            config.log_event("federation: could not lock the registry; not mutating")
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


def _publish_marker(root_path: Path, payload: dict, replace: bool) -> dict | None:
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
    payload = _marker_payload(Path(root_path), mode)
    published = _publish_marker(root_path, payload, replace=False)
    if published:
        return published
    # Nothing valid on disk: either the write failed, or an unparseable marker
    # is squatting the name. Replace it once, still create-once.
    if (Path(root_path) / ROOT_MARKER).exists():
        config.log_event(f"federation: replacing corrupt marker in {root_path}")
        return _publish_marker(root_path, payload, replace=True)
    return None


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


def _detect_clone(rid: str, root_path: Path) -> bool | None:
    """True when ``root_path`` is a *copy* of an already-registered root.

    Returns None when identity cannot be established — the caller must then
    refuse to register rather than pick a destructive default.

    Copying a config dir (``cp -r ~/.claude ~/.claude-copy``) duplicates the
    marker, so two live roots would claim one id and fight over one shard. A
    move is the same situation minus the original, so the distinguishing test
    is whether the previously registered path still carries that id.
    """
    shard = shard_path(rid)
    prior = _read_json(shard) if shard else None
    if prior is None:
        # A removed root leaves no shard, but its identity is still live on
        # disk: without this the copy of an unregistered root would claim the
        # original's id. The tombstone carries the path for exactly this.
        tomb = read_tombstone(rid)
        if tomb is None:
            return False  # nothing ever registered this id: nothing to clone
        if not tomb.get("path"):
            # Present but unreadable, or written before tombstones carried
            # metadata. Fail closed: we cannot tell a copy from a rejoin, and
            # guessing wrong hands two live roots the same id.
            return None
        prior = tomb
    old = prior.get("path")
    if not old or Path(old) == root_path:
        return False
    if read_marker(Path(old)) != rid:
        return False  # the old path moved away: this is that move, not a copy
    try:
        # A symlink or bind mount reaches one physical root by two names. Paths
        # are stored unresolved on purpose, so identity has to be settled by
        # inode: treating an alias as a copy would re-id the original root.
        return not os.path.samefile(old, root_path)
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
        return _register_locked(root_path, mode, label)


def _register_locked(
    root_path: Path | None,
    mode: str | None,
    label: str | None,
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
        marker = ensure_marker(root_path, want_mode)
    except OSError:
        return None
    if marker is None:
        return None

    rid = marker["id"]
    recorded_mode = marker.get("mode", "config")
    if mode is not None and mode != recorded_mode:
        raise FederationConflict(
            f"{root_path} is already registered as mode {recorded_mode!r}; "
            f"refusing to reinterpret it as {mode!r} "
            f"(`roots remove {rid}` then re-add if that is really the intent)"
        )
    if want_mode != recorded_mode:
        # Not an error: the recorded mode wins, so no memory changes address.
        # Still worth an audit line — the caller asked for something else.
        config.log_event(
            f"federation: {root_path} stays mode {recorded_mode!r} "
            f"(this run would have chosen {want_mode!r})"
        )
    verdict = _detect_clone(rid, root_path)
    if verdict is None:
        config.log_event(
            f"federation: refusing to register {root_path} — its identity is "
            f"ambiguous against the root already holding id {rid}"
        )
        return None
    if verdict:
        fresh = _publish_marker(
            root_path, _marker_payload(root_path, recorded_mode), replace=True
        )
        if fresh is None:
            return None
        rid = fresh["id"]
        config.log_event(
            f"federation: {root_path} is a copy of a live root — new id {rid}"
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
            return _register_locked(None, None, None)
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
    strays = {
        r.state_dir for r in iter_roots() if r.state_dir and r.state_dir != here
    }
    cur = current_root_path()
    marker = read_marker_data(cur) if cur is not None else None
    prior = marker.get("registry") if marker else None
    if prior and prior != here:
        strays.add(prior)
    if not strays:
        return None
    return (
        f"roots registered against a different state dir: {', '.join(sorted(strays))} "
        f"(this instance uses {here}) — set FOLDCRUMBS_STATE_DIR consistently "
        "or those roots stay invisible here"
    )
