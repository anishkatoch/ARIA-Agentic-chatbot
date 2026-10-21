"""Per-user OAuth token storage, backed by the `connector_tokens` table.

Used by the Gmail, Google Drive, and Salesforce connectors so each user's
OAuth tokens are scoped to that user (NOT a single flat JSON file shared by
whoever hits the server, like CONNECTOR_POC did).

Follows the same DB-session calling convention as app/services/dedup.py's
_get_db_session(): lazily resolve a session factory, use it, always close.

The only host-app dependency in this file is `app.db.session.get_session_factory`
(a plain SQLAlchemy sessionmaker, resolved lazily inside a try/except so this
module degrades gracefully if that import path doesn't exist in a different
host app). The model itself (ConnectorToken) comes from this package's own
app.connectors.models, not from the host app's model module.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _get_db_session():
    try:
        from app.db.session import get_session_factory
        factory = get_session_factory()
        if factory is None:
            return None
        return factory()
    except Exception as e:
        logger.warning(f"[CONNECTOR_TOKENS] DB session unavailable: {e}")
        return None


def get_connected_providers(user_id: str) -> set:
    """Returns the set of providers this user has a token for - one query for
    all of them, instead of /status calling get_token() once per oauth
    provider (each a separate DB session open/close)."""
    db = _get_db_session()
    if db is None:
        return set()
    try:
        from app.connectors.models import ConnectorToken
        rows = db.query(ConnectorToken.provider).filter(ConnectorToken.user_id == user_id).all()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning(f"[CONNECTOR_TOKENS] get_connected_providers failed for user={user_id}: {e}")
        return set()
    finally:
        db.close()


def get_token(user_id: str, provider: str) -> Optional[dict]:
    """Returns {"access_token", "refresh_token", "expires_at" (datetime|None), "extra" (dict)} or None."""
    db = _get_db_session()
    if db is None:
        return None
    try:
        from app.connectors.models import ConnectorToken
        row = (
            db.query(ConnectorToken)
            .filter(ConnectorToken.user_id == user_id, ConnectorToken.provider == provider)
            .first()
        )
        if row is None:
            return None
        extra = {}
        if row.extra:
            try:
                extra = json.loads(row.extra)
            except (TypeError, ValueError):
                extra = {}
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            # The DB column is timezone-naive, but save_token() always writes
            # datetime.now(timezone.utc) - so a naive value read back is UTC.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return {
            "access_token": row.access_token,
            "refresh_token": row.refresh_token,
            "expires_at": expires_at,
            "extra": extra,
        }
    except Exception as e:
        logger.warning(f"[CONNECTOR_TOKENS] get_token failed for {provider}/{user_id}: {e}")
        return None
    finally:
        db.close()


def save_token(
    user_id: str,
    provider: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_at: Optional[datetime] = None,
    extra: Optional[dict] = None,
) -> bool:
    """Upsert the token row for (user_id, provider). Returns False (never raises)
    if the DB is unavailable - callers should treat that as "could not persist"
    and surface an auth error to the user rather than silently proceeding."""
    db = _get_db_session()
    if db is None:
        logger.warning(f"[CONNECTOR_TOKENS] DB unavailable - could not persist {provider} token for user={user_id}")
        return False
    try:
        from app.connectors.models import ConnectorToken
        row = (
            db.query(ConnectorToken)
            .filter(ConnectorToken.user_id == user_id, ConnectorToken.provider == provider)
            .first()
        )
        if row is None:
            row = ConnectorToken(
                id=uuid.uuid4(),
                user_id=user_id,
                provider=provider,
                access_token=access_token,
            )
            db.add(row)

        row.access_token = access_token
        if refresh_token is not None:
            row.refresh_token = refresh_token
        row.expires_at = expires_at
        if extra is not None:
            row.extra = json.dumps(extra)
        row.updated_at = datetime.now(timezone.utc)

        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"[CONNECTOR_TOKENS] save_token failed for {provider}/{user_id}: {e}")
        return False
    finally:
        db.close()


def delete_token(user_id: str, provider: str) -> bool:
    db = _get_db_session()
    if db is None:
        return False
    try:
        from app.connectors.models import ConnectorToken
        row = (
            db.query(ConnectorToken)
            .filter(ConnectorToken.user_id == user_id, ConnectorToken.provider == provider)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"[CONNECTOR_TOKENS] delete_token failed for {provider}/{user_id}: {e}")
        return False
    finally:
        db.close()
