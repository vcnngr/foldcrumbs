"""AUTH — authorization integrity: memory as an honest authorization ledger.

Design: docs/design/authorization-integrity.md rev 2 (double-RT GREEN).
Source problem: arXiv 2609.01836 — endogenous authorization laundering.

The contract this module enforces:

MINTING (creating a grant) is fail-closed and locked:
* a grant exists only with non-empty ``granted_to`` and ``grants``, a
  ``backed_by`` pointer to a LIVE (active, non-expired) local ``event``
  or ``decision``, an aware FUTURE ``expires_at``, and provenance
  explicit_statement/verified — never inferred, never imported;
* creation is create-only on id AND destination: nothing ever replaces
  an existing grant file (retirement history is never overwritten);
* fuzzy dedup is excluded: two grants that read alike but differ in
  granted_to/grants/backed_by are different grants;
* minting runs under a per-store lock (federation.file_lock pattern) so
  concurrent mints serialize. The contract is OBSERVATIONAL, not
  transactional: the backing is verified under the lock at creation; if
  it dies concurrently outside it, every served read derives UNBACKED
  from then on — a clean ACTIVE grant with a dead backing is never
  served.

MAINTENANCE (rewrites of an existing grant) is NOT gated by the minting
rules — retiring or annotating a grant whose backing died is the
ledger's job. Blocking it would be exactly backwards.

SERVED READS derive state on every call (nothing trusts the write-time
verdict forever): RETIRED > EXPIRED > UNBACKED > ACTIVE, rendered with
the backing's live status, in a section of their own that is exempt
from the "honour it, do not re-ask" instruction.

What this module is NOT (design §1, declared limits): not a policy
enforcement point; no semantic classifier (a decision whose body says
"X is allowed forever" is out of reach); the CLI attestation is
procedural, not identity; raw files stay greppable — the contract
binds the surfaces the PRODUCT serves.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import config, federation
from .schema import MemoryRecord

AUTH_TYPE = "authorization"

# Provenances that may mint a grant: a human said it, or it was verified.
# `verified` is included for forward-compat with an attestation workflow;
# nothing in the product sets it automatically.
_MINT_PROVENANCES = {"explicit_statement", "verified"}

# Backing records must be one of these types: an event happened, or a
# decision was made. A fact or a preference cannot grant authority.
_BACKING_TYPES = {"event", "decision"}

_LOCK_WAIT_SECONDS = 5.0

_TRACE_MAX_DEPTH = 8


class AuthorizationError(ValueError):
    """Refusal to mint (or mutate) an authorization. Always visible."""


# --- derived state ---------------------------------------------------------- #

def derived_state(rec: MemoryRecord, cwd=None) -> str:
    """The honest state of a grant, re-derived on every served read.

    Precedence (design D4): RETIRED > EXPIRED > UNBACKED > ACTIVE.
    Fail-closed on corruption: a stored grant with a missing/naive/past
    expiry reads EXPIRED, never ACTIVE (T19).
    """
    if rec.type != AUTH_TYPE:
        raise AuthorizationError(f"not an authorization: {rec.type!r}")
    if rec.status in ("superseded", "deleted"):
        return "RETIRED"
    if rec.status not in ("active", "provisional", "archived"):
        # unknown status on a corrupted file: fail closed
        return "RETIRED"
    exp = rec.expires_at
    if exp is None or exp.tzinfo is None:
        return "EXPIRED"          # corrupted/missing expiry: fail closed
    if datetime.now(timezone.utc) >= exp:
        return "EXPIRED"
    if not _backing_is_live(rec.backed_by, cwd):
        return "UNBACKED"
    return "ACTIVE"


def _backing_is_live(backed_by: str | None, cwd=None) -> bool:
    """A backing is live when it resolves to an active, non-expired local
    event/decision. Missing id, dead record, wrong type: all not-live."""
    if not backed_by:
        return False
    from . import store
    for rec in store.iter_memories(cwd):
        if rec.id == backed_by:
            if rec.is_foreign:
                return False
            return (rec.status == "active" and not rec.is_expired
                    and rec.type in _BACKING_TYPES)
    return False


def backing_status(backed_by: str | None, cwd=None) -> str:
    """Human-readable backing status for served reads."""
    if not backed_by:
        return "missing"
    from . import store
    for rec in store.iter_memories(cwd):
        if rec.id == backed_by:
            if rec.is_foreign:
                return "foreign"
            if rec.status != "active":
                return rec.status
            if rec.is_expired:
                return "expired"
            return f"live {rec.type}"
    return "target removed"


# --- minting gate ------------------------------------------------------------ #

def _auth_lock_dir() -> Path:
    d = Path(config.STATE_DIR) / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / "authorization"


def validate_mint(rec: MemoryRecord, cwd=None) -> None:
    """Fail-closed creation gate (design D2). Raises AuthorizationError
    naming what is missing — refusals are always visible."""
    if rec.type != AUTH_TYPE:
        return
    if not (rec.granted_to or "").strip():
        raise AuthorizationError("granted_to is required for an authorization")
    if not (rec.grants or "").strip():
        raise AuthorizationError("grants is required for an authorization")
    if not (rec.backed_by or "").strip():
        raise AuthorizationError(
            "backed_by is required: a grant must point at the event or "
            "decision that is its source")
    exp = rec.expires_at
    if exp is None:
        raise AuthorizationError(
            "expires_at is required: no immortal permissions")
    if exp.tzinfo is None:
        raise AuthorizationError(
            "expires_at must be timezone-aware")
    if datetime.now(timezone.utc) >= exp:
        raise AuthorizationError("expires_at must be in the future")
    if rec.provenance not in _MINT_PROVENANCES:
        raise AuthorizationError(
            f"provenance {rec.provenance!r} cannot mint authority — an "
            "authorization requires explicit_statement or verified "
            "(a model must not infer permissions, a document must not "
            "smuggle them)")
    if not _backing_is_live(rec.backed_by, cwd):
        raise AuthorizationError(
            f"backing {rec.backed_by} is not a live local event/decision "
            f"(status: {backing_status(rec.backed_by, cwd)}) — a grant "
            "must be backed by a valid source event")


def mint(rec: MemoryRecord, cwd=None) -> Path:
    """Create a grant under the per-store authorization lock.

    Create-only on id AND destination (design D2.5): an existing grant
    file is a collision — retirement history is never overwritten. An
    exact live duplicate (same granted_to+grants+backed_by) is refused
    as a duplicate. Fuzzy dedup does not apply to grants.
    """
    from . import store
    validate_mint(rec, cwd)
    memdir = config.memory_dir(cwd)
    dest = memdir / rec.filename()
    with federation.file_lock(_auth_lock_dir(), wait=_LOCK_WAIT_SECONDS) as held:
        if not held:
            raise AuthorizationError(
                "another authorization write holds the lock; refusing "
                "rather than racing it (retry in a moment)")
        # re-check identity under the lock (TOCTOU): existing grants
        for existing in store.iter_memories_including_retired(cwd):
            if existing.type != AUTH_TYPE:
                continue
            # RT r2 F5: identity is BOTH the UUID and the semantic tuple —
            # a forged/duplicate id with a different tuple is a collision.
            if existing.id == rec.id:
                raise AuthorizationError(
                    f"collision: an authorization with id {rec.id} already "
                    f"exists ({existing.source_path or existing.filename()}) "
                    "— ids are never reused")
            if (existing.granted_to == rec.granted_to
                    and existing.grants == rec.grants
                    and existing.backed_by == rec.backed_by):
                if existing.status == "active" and not existing.is_expired:
                    raise AuthorizationError(
                        f"duplicate: a live grant with the same identity "
                        f"already exists ({existing.source_path or existing.filename()})")
                raise AuthorizationError(
                    f"collision: {existing.source_path or existing.filename()} "
                    f"holds a retired grant with the same identity — "
                    "retirement history is never overwritten")
        # validate again under the lock: the backing may have died between
        # the caller's check and here (observational contract, T11)
        validate_mint(rec, cwd)
        if dest.exists():
            raise AuthorizationError(
                f"destination collision: {dest.name} already exists — "
                "a grant never replaces a file (supersede or rename first)")
        return store.write_memory(rec, cwd, _skip_auth_gate=True)


# --- served reads ------------------------------------------------------------- #

def render_authorization_section(cwd=None) -> str:
    """The ledger section for recall/profile (design D4).

    Fed by a SEPARATE selection — store.search pre-excludes expired and
    superseded, and the ledger must show them (a retired grant that
    disappears from view is how laundering restarts). Empty store of
    grants -> empty string (no section, zero noise).
    """
    from . import store
    # RT r2 F4: the ledger shows the WHOLE history — retired/expired grants
    # included (a retired grant that disappears from view is how laundering
    # restarts), each with its derived state and, when retired, its trace.
    grants = [m for m in store.iter_memories_including_retired(cwd)
              if m.type == AUTH_TYPE]
    if not grants:
        return ""
    grants.sort(key=lambda m: (m.created_at, m.source_path or m.filename()))
    lines = [
        "Authorizations (verify before acting — memory is not the source "
        "of truth; this section is exempt from 'honour it, do not re-ask'):",
    ]
    for g in grants:
        state = derived_state(g, cwd)
        until = (g.expires_at.strftime("%Y-%m-%d")
                 if g.expires_at else "MISSING (treated as expired)")
        lines.append(
            f"  - {g.grants or '(no grants field)'} | to: {g.granted_to or '?'}"
            f" | until: {until} | {g.source_path or g.filename()}")
        lines.append(
            f"    backed by: {g.backed_by or 'MISSING'}"
            f" ({backing_status(g.backed_by, cwd)}) | {state}"
            + _invalidation_label(g, cwd))
        if state == "RETIRED":
            trace = render_trace(g, cwd)
            for tl in trace.splitlines()[1:]:
                lines.append(f"    {tl.strip()}")
    return "\n".join(lines)


def _invalidation_label(g: MemoryRecord, cwd=None) -> str:
    """INV design rev2 T14: a grant carrying an invalidation contract
    renders BOTH states — its own (UNBACKED/…) and the contract's. Neither
    suppressed by the other: the ledger is a repair surface, not a ranking.
    """
    from . import invalidation as _inv
    if not _inv.carries_contract(g):
        return ""
    ctx = _inv.ReadContext.for_store(cwd)
    outcome, detail = _inv.derive(g, ctx)
    if outcome == _inv.VALID:
        return ""
    label = {
        _inv.INVALIDATED: "INVALIDATED",
        _inv.DANGLING: "CONTRACT-DANGLING",
        _inv.UNRESOLVED: "CONTRACT-UNVERIFIED",
    }[outcome]
    return f" | {label}" + (f" ({detail})" if detail else "")


def render_trace(rec: MemoryRecord, cwd=None) -> str:
    """Bounded retirement trace (design D5): follow superseded_by links,
    max depth 8, dangling and cycle links rendered VISIBLY broken — the
    trace is never declared complete when it is broken. graph_path is
    deliberately NOT involved: superseded_by is not a relations arc and
    retired endpoints stay excluded there (that is correct behavior).
    """
    from . import store
    lines = [f"trace for {rec.title} ({rec.source_path or rec.filename()}):"]
    seen: set[str] = set()
    current = rec
    depth = 0
    while current.superseded_by and depth < _TRACE_MAX_DEPTH:
        depth += 1
        nxt_id = current.superseded_by
        if nxt_id in seen:
            lines.append(f"  -> cycle at {nxt_id} — trace broken — partial")
            return "\n".join(lines)
        seen.add(nxt_id)
        nxt = None
        for m in store.iter_memories_including_retired(cwd):
            if m.id == nxt_id:
                nxt = m
                break
        if nxt is None:
            lines.append(f"  -> target removed ({nxt_id}) — trace broken, "
                         "partial")
            return "\n".join(lines)
        lines.append(f"  -> {nxt.title} ({nxt.source_path or nxt.filename()})"
                     f" [{nxt.type}, {nxt.status}]")
        current = nxt
    if current.superseded_by and depth >= _TRACE_MAX_DEPTH:
        lines.append("  -> depth cap reached — trace truncated — partial")
    return "\n".join(lines)


def doctor_checks(cwd=None) -> list[str]:
    """Gap classes for `foldcrumbs doctor` (design D4)."""
    from . import store
    out: list[str] = []
    for m in store.iter_memories_including_retired(cwd):
        if m.type != AUTH_TYPE:
            continue
        name = m.source_path or m.filename()
        state = derived_state(m, cwd)
        if state == "UNBACKED":
            out.append(f"unbacked authorization: {name} (backing "
                       f"{backing_status(m.backed_by, cwd)})")
        if (m.expires_at is not None and m.is_expired
                and m.status == "active"):
            out.append(f"expired-but-active authorization: {name} — decay "
                       "has not archived it yet")
        # RT r2 F6: missing/naive expiry is the fail-closed EXPIRED case —
        # doctor must surface it, not just derive it silently.
        if m.expires_at is None:
            out.append(f"authorization with missing expiry: {name} "
                       "(reads EXPIRED, fail-closed) — fix the file or "
                       "retire the grant")
        elif m.expires_at.tzinfo is None:
            out.append(f"invalid (naive) expiry on authorization: {name}")
        if m.backed_by:
            for b in store.iter_memories_including_retired(cwd):
                if b.id == m.backed_by and b.status == "superseded":
                    out.append(f"authorization with superseded backing: "
                               f"{name} — retirement candidate")
                    break
    return out


def fetch_envelope(rec: MemoryRecord, cwd=None) -> str:
    """Deterministic state envelope for fetch (design D4): the product's
    claim BEFORE the raw historical document."""
    state = derived_state(rec, cwd)
    until = rec.expires_at.isoformat() if rec.expires_at else "MISSING"
    return (f"[authorization: {state}, expires {until}, "
            f"backed_by {rec.backed_by or 'MISSING'} "
            f"({backing_status(rec.backed_by, cwd)})]")
