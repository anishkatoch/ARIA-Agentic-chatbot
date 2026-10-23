"""Shared Microsoft Graph client for SharePoint and Teams - delegated,
per-user access via the same Microsoft OAuth grant Outlook uses (see
outlook.py's SCOPES, which include Sites.Read.All/Team.ReadBasic.All/
Channel.ReadBasic.All/ChannelMessage.Read.All alongside Mail.Read/Chat.Read).

Deliberately NOT app-only/client-credentials (no tenant-wide access with no
per-user login) - that would mean SharePoint/Teams access isn't tied to any
particular signed-in user and can't be connected/disconnected per user, the
same reasoning outlook_chat.py documents for sharing Outlook's grant instead
of running its own. router.py's outlook_callback mirrors the token it
receives into "sharepoint" and "teams" token_store rows right after saving
"outlook" and "outlook_chat", and /connectors/sharepoint/auth/login and
/connectors/teams/auth/login just return the same authorization URL
Outlook's login does.
"""
import os
from typing import Any

import httpx

from app.connectors import token_store
from app.connectors.clients.oauth2_token_client import OAuth2TokenClient

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

_TENANT = os.getenv("MICROSOFT_TENANT_ID") or "common"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"


class MicrosoftGraphError(RuntimeError):
    """Raised when Microsoft Graph authentication or a request fails."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _extract_graph_error_message(response: httpx.Response, max_len: int = 300) -> str:
    """Best-effort human-readable detail from a Graph error response body
    ({"error": {"code", "message"}}) - e.g. "Failed to get license
    information for the user" instead of just "access denied", which points
    at the real cause (no M365 license) instead of a permissions problem."""
    try:
        body = response.json()
    except Exception:
        return response.text.strip()[:max_len]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:max_len]
    return response.text.strip()[:max_len]


class MicrosoftGraphClient:
    """One instance per (user_id, provider) - provider is "sharepoint" or
    "teams", its own token_store row (mirrored from "outlook" at connect
    time, refreshed independently thereafter, same pattern as
    outlook_chat.py's OutlookChatConnector)."""

    def __init__(self, user_id: str, provider: str) -> None:
        if not user_id:
            raise MicrosoftGraphError("Authentication required", status_code=401)
        self.user_id = user_id
        self.provider = provider
        self.client_id = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
        if not self.client_id or not self.client_secret:
            raise MicrosoftGraphError(
                "MICROSOFT_CLIENT_ID or MICROSOFT_CLIENT_SECRET not configured in .env", status_code=500,
            )
        self._oauth = OAuth2TokenClient(user_id, provider, self.client_id, self.client_secret, TOKEN_ENDPOINT)

    def get_token(self) -> str:
        row = self._oauth.stored_token()
        if row is None:
            raise MicrosoftGraphError(
                f"{self.provider.capitalize()} is not connected. Connect Outlook (they share one "
                f"Microsoft sign-in), or visit /connectors/{self.provider}/auth/login directly.",
                status_code=401,
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise MicrosoftGraphError(
                    f"{self.provider.capitalize()} token expired and no refresh token available. "
                    "Reconnect Outlook.",
                    status_code=401,
                )
            access_token = self._refresh_access_token(refresh_token)

        return access_token

    def _refresh_access_token(self, refresh_token: str) -> str:
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            new_data = self._oauth.post_token_endpoint(form)
        except Exception as exc:
            raise MicrosoftGraphError(
                f"Failed to refresh {self.provider} token: {exc}. Please reconnect.", status_code=401,
            )

        new_access = new_data["access_token"]
        new_refresh = new_data.get("refresh_token", refresh_token)
        self._oauth.save(new_access, new_refresh, self._oauth.expires_at_from(new_data))
        return new_access

    def request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.get_token()
        url = f"{GRAPH_BASE_URL}{endpoint}" if endpoint.startswith("/") else f"{GRAPH_BASE_URL}/{endpoint}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            with httpx.Client(timeout=25.0) as client:
                response = client.get(url, headers=headers, params=params)
                if response.status_code == 404:
                    raise MicrosoftGraphError(f"Resource not found: {endpoint}", status_code=404)
                if response.status_code == 403:
                    detail = _extract_graph_error_message(response)
                    suffix = f": {detail}" if detail else ""
                    raise MicrosoftGraphError(
                        f"Access denied or insufficient Microsoft Graph permissions{suffix}", status_code=403,
                    )
                if response.status_code == 401:
                    detail = _extract_graph_error_message(response)
                    suffix = f" ({detail})" if detail else ""
                    raise MicrosoftGraphError(f"Microsoft Graph authentication failed{suffix}", status_code=401)
                if response.status_code == 429:
                    raise MicrosoftGraphError("Microsoft Graph API rate limit exceeded", status_code=429)
                if response.is_error:
                    raise MicrosoftGraphError(
                        f"Microsoft Graph API returned HTTP {response.status_code}: {response.text}"
                    )
                return response.json()
        except httpx.RequestError as exc:
            raise MicrosoftGraphError(f"Unable to reach Microsoft Graph: {str(exc)}")

    def get_json(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(endpoint, params=params)

    def get_all(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        result = self.get_json(endpoint, params=params)
        items = result.get("value", [])
        if not isinstance(items, list):
            return [result] if isinstance(result, dict) else []

        next_link = result.get("@odata.nextLink")
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        while next_link and len(items) < 500:
            try:
                with httpx.Client(timeout=25.0) as client:
                    resp = client.get(next_link, headers=headers)
                    if resp.is_error:
                        break
                    data = resp.json()
                    new_items = data.get("value", [])
                    if not new_items:
                        break
                    items.extend(new_items)
                    next_link = data.get("@odata.nextLink")
            except Exception:
                break

        return items

    def download_file_content(self, endpoint: str) -> bytes:
        token = self.get_token()
        url = f"{GRAPH_BASE_URL}{endpoint}" if endpoint.startswith("/") else f"{GRAPH_BASE_URL}/{endpoint}"

        headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                if response.is_error:
                    detail = _extract_graph_error_message(response)
                    suffix = f": {detail}" if detail else ""
                    raise MicrosoftGraphError(
                        f"Failed to download file from Microsoft Graph: HTTP {response.status_code}{suffix}"
                    )
                return response.content
        except httpx.RequestError as exc:
            raise MicrosoftGraphError(f"Network error during Microsoft Graph download: {str(exc)}")
