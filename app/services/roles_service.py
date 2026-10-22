"""Department-role assignment and knowledge-base access checks.

Roles are free-text tags (e.g. "hr", "it", "finance", "sales") assigned by
the admin per user (by email) and per knowledge base (as an allow-list).
There is no fixed enum - the admin can introduce a new role just by typing
it when assigning a user or restricting a KB; list_known_roles() surfaces
whatever's already in use so the admin UI can suggest them instead of
retyping from scratch.

Authorization source of truth is the local `user_roles` table (see
app.models.db.UserRole), keyed by email - never the token/client, since a
user's own Supabase account metadata is client-writable and unsafe for
this. Every lookup here is server-side, driven by the email on the already-
verified request.state.user set by app.main's auth middleware.
"""
import logging
import threading
import time
from datetime import datetime, timezone

from app.db.session import get_session_factory
from app.models.db import KnowledgeBase, UserRole

logger = logging.getLogger(__name__)

# get_user_roles() is the hot path: app.routers.chat and
# app.routers.knowledge_base call it on EVERY chat message and every
# knowledge-base listing to enforce access. A network-latency-bound DB
# round trip (observed ~800ms against this project's remote Supabase
# Postgres - genuinely a query round trip, not a code inefficiency) on
# every single one of those would make the whole app feel slow, not just
# the admin dashboard. Role assignments change rarely, so a short TTL
# cache - kept fresh immediately on write by set_user_roles, not just
# invalidated - removes that round trip from the common case entirely
# without ever risking a stale permission for longer than the TTL on a
# cold cache, and never on a role the admin just changed (write-through).
# Same in-memory-per-process caching pattern already used elsewhere in
# this codebase (e.g. rag.py's BM25/answer caches) - not safe across
# multiple server processes/workers without a shared cache, matching this
# app's existing single-process assumption throughout.
_ROLES_CACHE_TTL_S = 30
_roles_cache: dict[str, list[str]] = {}
_roles_cache_ts: dict[str, float] = {}
_roles_cache_lock = threading.Lock()

_bulk_roles_cache: dict[str, list[str]] | None = None
_bulk_roles_cache_ts: float = 0.0


def _normalize_roles(roles: list[str] | None) -> list[str]:
    """Lowercases, strips, drops empties, and dedupes while preserving
    order - so "HR", " hr ", "hr" all collapse to one "hr" entry regardless
    of how the admin typed it."""
    if not roles:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for r in roles:
        r = (r or "").strip().lower()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def get_user_roles(email: str | None) -> list[str]:
    """The department roles assigned to this email, or [] if none assigned
    (or the email is empty, e.g. a guest/admin account - see can_access_kb's
    docstring for how [] interacts with a KB's allowed_roles). Cached - see
    this module's cache comment above."""
    if not email:
        return []
    email = email.strip().lower()

    with _roles_cache_lock:
        ts = _roles_cache_ts.get(email)
        if ts is not None and time.monotonic() - ts < _ROLES_CACHE_TTL_S:
            return list(_roles_cache[email])

    factory = get_session_factory()
    if factory is None:
        logger.warning("[ROLES] DB unavailable - treating user as role-less")
        return []
    db = factory()
    try:
        row = db.query(UserRole).filter(UserRole.email == email).first()
        roles = list(row.roles or []) if row else []
    finally:
        db.close()

    with _roles_cache_lock:
        _roles_cache[email] = roles
        _roles_cache_ts[email] = time.monotonic()
    return roles


def set_user_roles(email: str, roles: list[str]) -> list[str]:
    """Upserts this email's role assignment - called by the admin's user
    management endpoint. Returns the normalized roles actually stored.
    Updates the cache immediately (write-through, not just invalidation) -
    the next get_user_roles()/list_user_roles() call for this email sees
    the change right away, on this process, with no TTL wait and no extra
    DB round trip to repopulate it."""
    email = email.strip().lower()
    normalized = _normalize_roles(roles)
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError("Database unavailable")
    db = factory()
    try:
        row = db.query(UserRole).filter(UserRole.email == email).first()
        if row is None:
            import uuid
            row = UserRole(id=uuid.uuid4(), email=email, roles=normalized)
            db.add(row)
        else:
            row.roles = normalized
            row.updated_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(f"[ROLES] {email} -> {normalized}")
    finally:
        db.close()

    global _bulk_roles_cache
    with _roles_cache_lock:
        _roles_cache[email] = normalized
        _roles_cache_ts[email] = time.monotonic()
        if _bulk_roles_cache is not None:
            if normalized:
                _bulk_roles_cache[email] = normalized
            else:
                _bulk_roles_cache.pop(email, None)
    return normalized


def list_user_roles() -> dict[str, list[str]]:
    """Every email that has an explicit role assignment, as {email: roles}.
    Emails with no row here simply aren't in the result - the caller (the
    admin users list) merges this against the full Supabase user list so
    role-less users still show up with an empty roles list. Cached - see
    this module's cache comment above."""
    global _bulk_roles_cache, _bulk_roles_cache_ts
    with _roles_cache_lock:
        if _bulk_roles_cache is not None and time.monotonic() - _bulk_roles_cache_ts < _ROLES_CACHE_TTL_S:
            return dict(_bulk_roles_cache)

    factory = get_session_factory()
    if factory is None:
        return {}
    db = factory()
    try:
        result = {row.email: list(row.roles or []) for row in db.query(UserRole).all()}
    finally:
        db.close()

    with _roles_cache_lock:
        _bulk_roles_cache = result
        _bulk_roles_cache_ts = time.monotonic()
    return dict(result)


def list_known_roles() -> list[str]:
    """Every distinct role name currently in use, from both user
    assignments and KB restrictions - used only to populate admin-UI
    suggestions (autocomplete), not as a validation allow-list; the admin
    can still type a brand-new role name at any time."""
    factory = get_session_factory()
    if factory is None:
        return []
    db = factory()
    try:
        roles: set[str] = set()
        for (user_roles,) in db.query(UserRole.roles).all():
            roles.update(user_roles or [])
        for (kb_roles,) in db.query(KnowledgeBase.allowed_roles).all():
            roles.update(kb_roles or [])
        return sorted(roles)
    finally:
        db.close()


def _clear_roles_cache() -> None:
    """Test-only: resets both in-memory caches to a clean slate. Production
    code never needs this - set_user_roles() already keeps the cache
    correct on every write."""
    global _bulk_roles_cache, _bulk_roles_cache_ts
    with _roles_cache_lock:
        _roles_cache.clear()
        _roles_cache_ts.clear()
        _bulk_roles_cache = None
        _bulk_roles_cache_ts = 0.0


def can_access_kb(user_roles: list[str], kb_allowed_roles: list[str] | None) -> bool:
    """A knowledge base with no allowed_roles (None or []) is unrestricted -
    every signed-in user can see it, same as every KB behaved before roles
    existed. A knowledge base WITH allowed_roles requires the user to hold
    at least one of them; a user with no roles at all can therefore never
    see a restricted KB, which is the intended default-deny behavior for
    role-gated content."""
    if not kb_allowed_roles:
        return True
    return bool(set(user_roles) & set(kb_allowed_roles))
