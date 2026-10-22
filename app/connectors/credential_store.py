"""Per-user override credentials for API-key-style connectors (GitHub,
Azure DevOps), backed by the `connector_credentials` table.

Lets one user "bring their own key" for a connector that otherwise only
reads a single shared app-level env var - their row takes priority for their
own requests only; every other user still gets the env var default (or stays
unconfigured if there isn't one, with no env var touched or server restart
needed). Distinct from token_store.py (OAuth access/refresh tokens for
Gmail/Google Drive); this one stores an opaque JSON blob per
provider since each one's credential shape differs (e.g. GitHub needs just a
token, Azure DevOps needs an org_url + PAT).

Follows the same DB-session calling convention as token_store.py.
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
        logger.warning(f"[CONNECTOR_CREDENTIALS] DB session unavailable: {e}")
        return None


def get_own_credential_providers(user_id: str) -> set:
    """Returns the set of providers this user has their own credentials for -
    one query for all of them, instead of /status calling get_credentials()
    once per byok provider (each a separate DB session open/close)."""
    db = _get_db_session()
    if db is None:
        return set()
    try:
        from app.connectors.models import ConnectorCredential
        rows = db.query(ConnectorCredential.provider).filter(ConnectorCredential.user_id == user_id).all()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning(f"[CONNECTOR_CREDENTIALS] get_own_credential_providers failed for user={user_id}: {e}")
        return set()
    finally:
        db.close()


def get_credentials(user_id: str, provider: str) -> Optional[dict]:
    db = _get_db_session()
    if db is None:
        return None
    try:
        from app.connectors.models import ConnectorCredential
        row = (
            db.query(ConnectorCredential)
            .filter(ConnectorCredential.user_id == user_id, ConnectorCredential.provider == provider)
            .first()
        )
        if row is None:
            return None
        try:
            return json.loads(row.credentials)
        except (TypeError, ValueError):
            return None
    except Exception as e:
        logger.warning(f"[CONNECTOR_CREDENTIALS] get_credentials failed for {provider}/{user_id}: {e}")
        return None
    finally:
        db.close()


def save_credentials(user_id: str, provider: str, credentials: dict) -> bool:
    """Upsert the credentials row for (user_id, provider). Returns False
    (never raises) if the DB is unavailable - callers should surface an
    error to the user rather than silently proceeding as if it saved."""
    db = _get_db_session()
    if db is None:
        logger.warning(f"[CONNECTOR_CREDENTIALS] DB unavailable - could not persist {provider} credentials for user={user_id}")
        return False
    try:
        from app.connectors.models import ConnectorCredential
        row = (
            db.query(ConnectorCredential)
            .filter(ConnectorCredential.user_id == user_id, ConnectorCredential.provider == provider)
            .first()
        )
        if row is None:
            row = ConnectorCredential(
                id=uuid.uuid4(), user_id=user_id, provider=provider,
                credentials=json.dumps(credentials),
            )
            db.add(row)
        else:
            row.credentials = json.dumps(credentials)
            row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"[CONNECTOR_CREDENTIALS] save_credentials failed for {provider}/{user_id}: {e}")
        return False
    finally:
        db.close()


def delete_credentials(user_id: str, provider: str) -> bool:
    db = _get_db_session()
    if db is None:
        return False
    try:
        from app.connectors.models import ConnectorCredential
        row = (
            db.query(ConnectorCredential)
            .filter(ConnectorCredential.user_id == user_id, ConnectorCredential.provider == provider)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"[CONNECTOR_CREDENTIALS] delete_credentials failed for {provider}/{user_id}: {e}")
        return False
    finally:
        db.close()
