"""Auth for the single fixed super-admin account that runs the /admin portal.

This is deliberately not a user-accounts system - there is exactly one admin
identity (ADMIN_EMAIL / ADMIN_PASSWORD in .env), no signup, no OTP, no social
login. The pattern mirrors app/connectors/access_gate.py: a shared secret
compared in constant time, then a signed, short-lived JWT (role="admin")
issued via the same jose.jwt mechanics as auth_service.create_guest_token.
"""
import hmac
import logging
import uuid
from datetime import datetime, timezone, timedelta

from jose import JWTError, jwt

from app.config import cfg

logger = logging.getLogger(__name__)

_ROLE = "admin"


def verify_admin_credentials(email: str, password: str) -> bool:
    if not cfg.admin_email or not cfg.admin_password:
        logger.warning("[ADMIN] ADMIN_EMAIL/ADMIN_PASSWORD not configured")
        return False
    email_ok    = hmac.compare_digest((email or "").strip().lower(), cfg.admin_email.strip().lower())
    password_ok = hmac.compare_digest((password or ""), cfg.admin_password)
    return email_ok and password_ok


def issue_admin_token() -> str:
    payload = {
        "sub":   f"admin-{uuid.uuid4()}",
        "email": cfg.admin_email,
        "role":  _ROLE,
        "exp":   datetime.now(timezone.utc) + timedelta(hours=cfg.jwt_expire_hours),
        "iat":   datetime.now(timezone.utc),
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)


def verify_admin_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("role") != _ROLE:
        return None
    return {
        "id":    payload["sub"],
        "email": payload["email"],
        "name":  "Admin",
        "role":  _ROLE,
    }
