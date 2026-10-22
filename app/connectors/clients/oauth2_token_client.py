"""Shared OAuth2 "authorization code + refresh token" HTTP plumbing, used by
the Gmail and Google Drive connectors - both implement the
same flow (POST to a token endpoint, track expiry, refresh, persist) against
different providers. This class handles the mechanical parts that were
previously duplicated nearly verbatim: the token-endpoint POST, expiry
math, and token_store persistence. Each connector keeps its own thin
exchange/refresh/get_valid_token methods so its provider-specific error
wording and behavior stays unchanged - this is deliberately not a
one-size-fits-all interface over those parts.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app.connectors import token_store

EXPIRY_BUFFER_SECONDS = 60


class OAuth2TokenClient:
    """One instance per (user_id, provider). Not a connector itself - each
    connector class holds one of these and delegates token-endpoint
    mechanics to it."""

    def __init__(self, user_id: str, provider: str, client_id: str, client_secret: str,
                 token_endpoint: str, default_expires_in: int = 3600):
        self.user_id = user_id
        self.provider = provider
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_endpoint = token_endpoint
        self.default_expires_in = default_expires_in

    def post_token_endpoint(self, form: dict) -> dict:
        """Raw POST to the provider's token endpoint. Raises
        urllib.error.HTTPError (callers can read exc.read() for the
        provider's error body) or another Exception on a network failure -
        callers map these to their own HTTPException so each provider keeps
        its own status code and wording."""
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            self.token_endpoint, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def expires_at_from(self, token_data: dict) -> datetime:
        return datetime.now(timezone.utc) + timedelta(
            seconds=float(token_data.get("expires_in", self.default_expires_in))
        )

    def stored_token(self) -> dict | None:
        return token_store.get_token(self.user_id, self.provider)

    def save(self, access_token: str, refresh_token: str | None, expires_at: datetime,
              extra: dict | None = None) -> None:
        token_store.save_token(
            self.user_id, self.provider,
            access_token=access_token, refresh_token=refresh_token,
            expires_at=expires_at, extra=extra,
        )

    def is_expiring(self, expires_at: datetime | None) -> bool:
        return expires_at is None or datetime.now(timezone.utc) > (expires_at - timedelta(seconds=EXPIRY_BUFFER_SECONDS))
