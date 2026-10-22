import logging
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
