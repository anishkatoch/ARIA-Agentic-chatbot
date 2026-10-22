import os
from typing import Any
import httpx
import msal

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


class MicrosoftGraphError(RuntimeError):
    """Raised when Microsoft Graph authentication or a request fails."""


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
    """Shared Microsoft Graph client for SharePoint and Teams."""

    def __init__(self) -> None:
        self.tenant_id = os.getenv("MICROSOFT_TENANT_ID", "").strip()
        self.client_id = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
        self._app: msal.ConfidentialClientApplication | None = None

    def _validate_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("MICROSOFT_TENANT_ID", self.tenant_id),
                ("MICROSOFT_CLIENT_ID", self.client_id),
                ("MICROSOFT_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise MicrosoftGraphError(
                f"Missing required Microsoft Graph environment variables: {', '.join(missing)}"
            )

    def _get_app(self) -> msal.ConfidentialClientApplication:
        if self._app is None:
            self._validate_credentials()
            authority = f"https://login.microsoftonline.com/{self.tenant_id}"
            self._app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                authority=authority,
                client_credential=self.client_secret,
            )
        return self._app

    def get_token(self) -> str:
        app = self._get_app()
        result = app.acquire_token_silent(GRAPH_SCOPE, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)

        if not result or "access_token" not in result:
            err_desc = result.get("error_description") or result.get("error") if result else "Unknown error"
            raise MicrosoftGraphError(f"Failed to acquire Microsoft Graph token: {err_desc}")

        return result["access_token"]

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
                    raise MicrosoftGraphError(f"Resource not found: {endpoint}")
                if response.status_code == 403:
                    detail = _extract_graph_error_message(response)
                    suffix = f": {detail}" if detail else ""
                    raise MicrosoftGraphError(f"Access denied or insufficient Microsoft Graph permissions{suffix}")
                if response.status_code == 401:
                    detail = _extract_graph_error_message(response)
                    suffix = f" ({detail})" if detail else ""
                    raise MicrosoftGraphError(f"Microsoft Graph authentication failed{suffix}")
                if response.status_code == 429:
                    raise MicrosoftGraphError("Microsoft Graph API rate limit exceeded")
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
