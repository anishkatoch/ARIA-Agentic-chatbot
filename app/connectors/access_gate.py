"""A second, independent lock in front of every /connectors/* endpoint.

Being logged into the app (the normal Bearer JWT) is not enough to reach any
connector - the user must also know a shared CONNECTORS_ACCESS_KEY, set once
by whoever runs this deployment and stored only in .env. This matters because
a connected Gmail/GitHub/Jira/Salesforce session gives this app real access to
someone's real third-party accounts, which is more sensitive than ordinary
chat data.

Flow:
    1. Client calls POST /connectors/unlock with the raw access key.
    2. If it matches CONNECTORS_ACCESS_KEY (compared in constant time, so a
       mistyped guess can't be timed to learn how many characters were right),
       the server issues a short-lived signed "unlock token" scoped to that
       one user.
    3. Every other /connectors/* route requires that unlock token in the
       X-Connectors-Unlock header (enforced once, as a dependency on the main
       router - see router.py). Without it, or with an invalid/expired one,
       every one of those routes returns 403 - nothing short of the real key
       grants access, by design.

The raw access key itself is never stored anywhere after step 2 completes -
not in a session, not in a cookie, not echoed back in any response. Only the
signed proof-of-unlock token (which cannot be reversed into the original key)
leaves this module.
"""
import hmac
import os
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

_ALGORITHM = "HS256"
_SCOPE = "connectors_unlock"
_TOKEN_TTL_HOURS = 12


def _access_key() -> str:
    # Read live rather than caching at import time, so a key set after the
    # process started (e.g. edited into .env then the server reloaded) is
    # picked up the same way every other connector env var already is.
    return os.getenv("CONNECTORS_ACCESS_KEY", "").strip()


def is_configured() -> bool:
    return bool(_access_key())


def verify_access_key(candidate: str) -> bool:
    key = _access_key()
    if not key:
        return False
    return hmac.compare_digest((candidate or "").strip(), key)


def issue_unlock_token(user_id: str) -> str:
    """Signs a short-lived, user-scoped proof of unlock using the access key
    itself as the signing secret - so verifying a token still requires the
    real key to have been in play, and no separate signing secret needs to be
    configured or kept in sync with it."""
    payload = {
        "sub": user_id,
        "scope": _SCOPE,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=_TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, _access_key(), algorithm=_ALGORITHM)


def verify_unlock_token(token: str, user_id: str) -> bool:
    key = _access_key()
    if not key or not token:
        return False
    try:
        payload = jwt.decode(token, key, algorithms=[_ALGORITHM])
    except JWTError:
        return False
    return payload.get("scope") == _SCOPE and payload.get("sub") == user_id
