import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

from jose import JWTError, jwt

from app.config import cfg

logger = logging.getLogger(__name__)


# ── Supabase client (lazy) ────────────────────────────────────────────────────

_supabase_client = None

def _supabase():
    global _supabase_client
    if _supabase_client is None:
        if not cfg.supabase_url or not cfg.supabase_anon_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
        from supabase import create_client
        _supabase_client = create_client(cfg.supabase_url, cfg.supabase_anon_key)
    return _supabase_client


# ── Supabase Admin API (service-role key - never exposed to the browser) ──────
# Used only by the /admin user-management endpoints (app/routers/admin.py),
# never by anything a regular user's request can reach - the service role
# key bypasses RLS and can act as any user, so a separate client instance
# (never the anon-key one above) keeps that capability from leaking into
# the normal request path by accident.

_supabase_admin_client = None

def _supabase_admin():
    global _supabase_admin_client
    if _supabase_admin_client is None:
        if not cfg.supabase_url or not cfg.supabase_service_key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in .env "
                "to manage users from the admin dashboard"
            )
        from supabase import create_client
        _supabase_admin_client = create_client(cfg.supabase_url, cfg.supabase_service_key)
    return _supabase_admin_client


# The admin dashboard's Users tab previously made a live Supabase network
# round-trip on every single tab switch - this is the actual slowness, not
# just "things feeling slow": Supabase's admin API is a real HTTP call
# (tens to hundreds of ms), and the account list rarely changes between
# switches. A short in-process cache makes repeat loads near-instant
# without ever showing data more than ADMIN_USERS_CACHE_TTL_S stale, and is
# invalidated immediately on admin_create_user so a freshly created account
# always shows up right away rather than waiting out the TTL.
ADMIN_USERS_CACHE_TTL_S = 30
_admin_users_cache: dict = {"data": None, "expires_at": 0.0}
_admin_users_cache_lock = threading.Lock()


def _invalidate_admin_users_cache() -> None:
    with _admin_users_cache_lock:
        _admin_users_cache["data"] = None
        _admin_users_cache["expires_at"] = 0.0


def admin_list_users(per_page: int = 200, force_refresh: bool = False) -> list[dict]:
    """Every Supabase-registered user (email + password accounts, and
    Google/Microsoft OAuth accounts - both land in the same Supabase user
    table). Capped at `per_page` in one page - this app's user count is
    small (an internal tool, not a public product), so one page comfortably
    covers it; raise the cap before this needs real pagination.

    This performs a real network call to Supabase - callers on the async
    request path (app/routers/admin.py) must run it via
    `await asyncio.to_thread(...)`, never call it directly, or it blocks
    the whole event loop (every other in-flight request - chat, uploads,
    everyone else) for the duration of that HTTP round-trip."""
    if not force_refresh:
        with _admin_users_cache_lock:
            cached = _admin_users_cache["data"]
            if cached is not None and time.monotonic() < _admin_users_cache["expires_at"]:
                return cached

    admin = _supabase_admin().auth.admin
    users = admin.list_users(page=1, per_page=per_page)
    result = [
        {
            "id":         u.id,
            "email":      u.email,
            "name":       (u.user_metadata or {}).get("name", (u.email or "").split("@")[0]),
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]
    with _admin_users_cache_lock:
        _admin_users_cache["data"] = result
        _admin_users_cache["expires_at"] = time.monotonic() + ADMIN_USERS_CACHE_TTL_S
    return result


def admin_create_user(email: str, password: str, name: str | None = None) -> dict:
    """Creates a pre-confirmed Supabase account (email_confirm=True) - no
    OTP email round-trip, unlike the public /auth/signup flow. Used by the
    admin's "create user" action, where the admin is vouching for the
    account directly instead of the user self-registering."""
    admin = _supabase_admin().auth.admin
    try:
        res = admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"name": name} if name else {},
        })
    except Exception as e:
        msg = str(e).lower()
        if "already registered" in msg or "already exists" in msg or "already been registered" in msg:
            raise ValueError("An account with this email already exists.")
        raise ValueError(f"Failed to create user: {e}")
    if res.user is None:
        raise ValueError("Failed to create user.")
    _invalidate_admin_users_cache()  # otherwise this new account wouldn't show up for up to ADMIN_USERS_CACHE_TTL_S
    logger.info(f"[AUTH][ADMIN] Created user: {email}")
    return {
        "id":    res.user.id,
        "email": res.user.email,
        "name":  name or email.split("@")[0],
    }


def admin_set_user_password(email: str, password: str) -> None:
    """Resets an existing Supabase user's password directly (no "forgot
    password" email round-trip) - used for one-off admin-driven resets.
    Looks the account up through the cached admin_list_users() instead of
    its own separate Supabase call, same rationale as there - also a real
    network call, same async/event-loop caveat applies to callers."""
    users = admin_list_users()
    match = next((u for u in users if (u["email"] or "").lower() == email.lower()), None)
    if match is None:
        raise ValueError(f"No account found for {email}")
    admin = _supabase_admin().auth.admin
    admin.update_user_by_id(match["id"], {"password": password})
    logger.info(f"[AUTH][ADMIN] Password reset for: {email}")


# ── Guest JWT (no Supabase, no email verification) ────────────────────────────

def create_guest_token(name: str, email: str) -> str:
    payload = {
        "sub":   f"guest-{uuid.uuid4()}",
        "name":  name,
        "email": email,
        "role":  "guest",
        "exp":   datetime.now(timezone.utc) + timedelta(hours=cfg.jwt_expire_hours),
        "iat":   datetime.now(timezone.utc),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def verify_guest_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
        if payload.get("role") != "guest":
            return None
        return payload
    except JWTError:
        return None


# ── Supabase Auth wrappers ────────────────────────────────────────────────────

def signup(name: str, email: str, password: str) -> dict:
    sb = _supabase()
    # Step 1 - create the account (no confirmation email if "Confirm email" is OFF in Supabase)
    try:
        res = sb.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"name": name}},
        })
    except Exception as e:
        msg = str(e).lower()
        if "already registered" in msg or "already exists" in msg or "user already" in msg:
            raise ValueError("An account with this email already exists. Try signing in.")
        if "rate limit" in msg or "too many" in msg:
            raise ValueError("Too many attempts on this email. Wait a few minutes or use a different email address.")
        raise ValueError(f"Sign up failed: {e}")
    if res.user is None:
        raise ValueError("Sign up failed - please try again.")

    # Supabase sign_up() already sends the OTP - no second call needed
    logger.info(f"[AUTH] Signup done, OTP sent by Supabase: {email}")
    return {
        "token": None,
        "needs_otp": True,
        "user": {
            "id":    res.user.id,
            "email": res.user.email,
            "name":  name,
            "role":  "user",
        },
    }


def verify_otp(email: str, token: str) -> dict:
    sb = _supabase()
    try:
        # type="signup" matches the OTP sent by sign_up()
        res = sb.auth.verify_otp({"email": email, "token": token, "type": "signup"})
    except Exception as e:
        msg = str(e).lower()
        if "invalid" in msg or "expired" in msg or "token" in msg or "otp" in msg:
            raise ValueError("Invalid or expired code. Check your email and try again.")
        raise ValueError(f"Verification failed: {e}")
    if res.user is None or res.session is None:
        raise ValueError("Invalid or expired code.")
    logger.info(f"[AUTH] OTP verified: {email}")
    return {
        "token": res.session.access_token,
        "user": {
            "id":    res.user.id,
            "email": res.user.email,
            "name":  (res.user.user_metadata or {}).get("name", email.split("@")[0]),
            "role":  "user",
        },
    }


def signin(email: str, password: str) -> dict:
    sb = _supabase()
    try:
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        msg = str(e).lower()
        if "email not confirmed" in msg:
            raise ValueError("Please confirm your email first - check your inbox for the confirmation link.")
        if "invalid login" in msg or "invalid credentials" in msg or "wrong password" in msg:
            raise ValueError("Invalid email or password.")
        raise ValueError(f"Sign in failed: {e}")
    if res.user is None:
        raise ValueError("Invalid email or password.")
    logger.info(f"[AUTH] Signin success: {email}")
    return {
        "token": res.session.access_token,
        "user": {
            "id":    res.user.id,
            "email": res.user.email,
            "name":  (res.user.user_metadata or {}).get("name", email.split("@")[0]),
            "role":  "user",
        },
    }


def forgot_password(email: str) -> None:
    sb = _supabase()
    sb.auth.reset_password_email(email)
    logger.info(f"[AUTH] Password reset email sent: {email}")


def verify_supabase_token(token: str) -> dict | None:
    try:
        sb = _supabase()
        res = sb.auth.get_user(token)
        if res.user is None:
            return None
        return {
            "id":    res.user.id,
            "email": res.user.email,
            "name":  (res.user.user_metadata or {}).get("name", res.user.email.split("@")[0]),
            "role":  "user",
        }
    except Exception as e:
        logger.debug(f"[AUTH] Supabase token verify failed: {e}")
        return None


# ── Universal token verifier ──────────────────────────────────────────────────

def verify_token(token: str) -> dict | None:
    """Try admin JWT, then guest JWT, then Supabase. Returns user dict or None."""
    from app.services import admin_auth
    admin = admin_auth.verify_admin_token(token)
    if admin:
        return admin

    guest = verify_guest_token(token)
    if guest:
        return {
            "id":    guest["sub"],
            "email": guest["email"],
            "name":  guest["name"],
            "role":  "guest",
        }
    return verify_supabase_token(token)
