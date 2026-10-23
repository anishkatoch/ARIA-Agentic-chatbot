"""Read-only Outlook Mail connector using delegated user OAuth 2.0 against
Microsoft Graph - the Microsoft counterpart to gmail.py.

Runs its own authorization-code + refresh-token flow, same shape as
gmail.py/google_drive.py, against the MICROSOFT_CLIENT_ID/CLIENT_SECRET
Azure AD app registration (which must also have a "Web" platform redirect
URI added for OUTLOOK_REDIRECT_URI, and this module's SCOPES granted as
delegated permissions - admin consent is typically required).

Outlook Chat, SharePoint and Teams (app/connectors/clients/outlook_chat.py,
sharepoint.py, teams.py, via microsoft_graph.py) all share this exact OAuth
grant, the same way Google Chat shares Gmail's - see this module's SCOPES
and router.py's outlook_callback, which mirrors the resulting token into
"outlook_chat", "sharepoint" and "teams" rows too. From the user's
perspective all four are one Microsoft sign-in: connecting or disconnecting
any one of them connects/disconnects all four (see router.py's
_LINKED_OAUTH_PROVIDERS).
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from app.connectors import token_store
from app.connectors.clients._errors import extract_http_error_detail
from app.connectors.clients.oauth2_token_client import OAuth2TokenClient

# "common" supports both personal Microsoft accounts and any work/school
# tenant without the app having to be tied to one specific tenant - set
# MICROSOFT_TENANT_ID to restrict sign-in to a single organization instead.
_TENANT = os.getenv("MICROSOFT_TENANT_ID") or "common"
AUTH_ENDPOINT = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/authorize"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

SCOPES = [
    "offline_access",
    "User.Read",
    "Mail.Read",
    # Outlook Chat shares this grant - see outlook_chat.py.
    "Chat.Read",
    # SharePoint and Teams also share this grant (delegated, per-user access
    # instead of a separate app-only client-credentials flow) - see
    # microsoft_graph.py, sharepoint.py and teams.py. Requires admin consent
    # in most tenants, same as the scopes above.
    "Sites.Read.All",
    "Team.ReadBasic.All",
    "Channel.ReadBasic.All",
    "ChannelMessage.Read.All",
]

PROVIDER = "outlook"

# Graph lets you address several mail folders by these well-known names
# directly as folder ids - no extra /me/mailFolders lookup call needed,
# same trick Gmail's label tabs use with its system labelIds.
FOLDER_IDS = {
    "inbox": "inbox",
    "junk": "junkemail",
    "sent": "sentitems",
    "drafts": "drafts",
    "deleted": "deleteditems",
    "all": None,
}

_MESSAGE_SELECT = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,hasAttachments"
_MESSAGE_SELECT_FULL = "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments"


def _format_recipients(recipients: list[dict] | None) -> str:
    if not recipients:
        return ""
    return ", ".join(
        f"{r.get('emailAddress', {}).get('name', '')} <{r.get('emailAddress', {}).get('address', '')}>".strip()
        for r in recipients
    )


class OutlookConnector:
    def __init__(self, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        self.user_id = user_id
        self.client_id = os.getenv("MICROSOFT_CLIENT_ID")
        self.client_secret = os.getenv("MICROSOFT_CLIENT_SECRET")
        self.redirect_uri = os.getenv("OUTLOOK_REDIRECT_URI", "http://localhost:8000/outlook/callback")

        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=500,
                detail="MICROSOFT_CLIENT_ID or MICROSOFT_CLIENT_SECRET not configured in .env",
            )

        self._oauth = OAuth2TokenClient(user_id, PROVIDER, self.client_id, self.client_secret, TOKEN_ENDPOINT)

    def get_authorization_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "response_mode": "query",
            "prompt": "select_account",
            "state": self.user_id,
        }
        return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    def exchange_code_for_token(self, code: str) -> dict:
        form = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "scope": " ".join(SCOPES),
        }
        try:
            token_data = self._oauth.post_token_endpoint(form)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {err_body}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to reach Microsoft OAuth: {str(e)}")

        expires_at = self._oauth.expires_at_from(token_data)
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            existing = self._oauth.stored_token()
            if existing:
                refresh_token = existing.get("refresh_token")

        self._oauth.save(token_data["access_token"], refresh_token, expires_at)
        return token_data

    def _get_valid_token(self) -> str:
        row = self._oauth.stored_token()
        if row is None:
            raise HTTPException(
                status_code=401,
                detail="Outlook is not connected. Visit /connectors/outlook/auth/login",
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail="Outlook token expired and no refresh token available. Reconnect Outlook.",
                )
            access_token = self._refresh_access_token(refresh_token)

        return access_token

    def _refresh_access_token(self, refresh_token: str) -> str:
        form = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(SCOPES),
        }
        try:
            new_data = self._oauth.post_token_endpoint(form)
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail=f"Failed to refresh Outlook token: {str(exc)}. Please reconnect.",
            )

        new_access = new_data["access_token"]
        new_refresh = new_data.get("refresh_token", refresh_token)
        self._oauth.save(new_access, new_refresh, self._oauth.expires_at_from(new_data))
        return new_access

    def _api_request(self, path_or_url: str, params: dict | None = None, token: str | None = None) -> dict:
        token = token or self._get_valid_token()
        url = path_or_url if path_or_url.startswith("http") else f"{GRAPH_BASE}{path_or_url}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = extract_http_error_detail(exc)
            if exc.code == 401:
                # Microsoft Graph returns this same 401 both for a genuinely
                # expired/invalid token AND for a well-formed, correctly
                # scoped token belonging to an account with no Exchange Online
                # mailbox (e.g. a personal Microsoft account with no Microsoft
                # 365 license) - often with an empty body either way, so we
                # can't tell them apart from the status code alone. Cover
                # both so the message doesn't send someone down the wrong
                # path (reconnecting won't help the no-mailbox case).
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(
                    401,
                    f"Microsoft Graph rejected the Outlook access token{suffix}. "
                    "Reconnect Outlook - if reconnecting doesn't help, the signed-in "
                    "account likely has no Microsoft 365 mailbox/license (a personal "
                    "Hotmail/Outlook.com account alone isn't enough).",
                )
            if exc.code == 403:
                suffix = f": {detail}" if detail else ""
                raise HTTPException(403, f"Outlook/Graph API access denied or insufficient permissions{suffix}")
            if exc.code == 404:
                raise HTTPException(404, "Outlook resource not found")
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Microsoft Graph error: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to reach Microsoft Graph: {str(exc)}")

    def get_status(self) -> dict:
        row = token_store.get_token(self.user_id, PROVIDER)
        if row is None:
            return {"authenticated": False, "message": "Not authenticated. Visit /connectors/outlook/auth/login"}
        try:
            profile = self._api_request("/me", {"$select": "mail,userPrincipalName"})
            return {"authenticated": True, "email_address": profile.get("mail") or profile.get("userPrincipalName")}
        except HTTPException as e:
            return {"authenticated": False, "detail": e.detail}

    def list_messages(self, folder: str = "inbox", max_results: int = 25, next_link: str = "") -> dict:
        if next_link:
            raw = self._api_request(next_link)
        else:
            folder_id = FOLDER_IDS.get(folder, "inbox")
            path = f"/me/mailFolders/{folder_id}/messages" if folder_id else "/me/messages"
            params = {
                "$top": min(max(max_results, 1), 50),
                "$select": _MESSAGE_SELECT,
                "$orderby": "receivedDateTime desc",
            }
            raw = self._api_request(path, params)

        token = self._get_valid_token()

        def _to_item(m: dict) -> dict:
            return {
                "id": m["id"],
                "subject": m.get("subject") or "(No Subject)",
                "from": _format_recipients([{"emailAddress": (m.get("from") or {}).get("emailAddress", {})}]) if m.get("from") else "",
                "to": _format_recipients(m.get("toRecipients")),
                "cc": _format_recipients(m.get("ccRecipients")),
                "date": m.get("receivedDateTime", ""),
                "snippet": m.get("bodyPreview", ""),
                "has_attachments": bool(m.get("hasAttachments")),
            }

        items = [_to_item(m) for m in raw.get("value", [])]
        return {
            "messages": items,
            "next_link": raw.get("@odata.nextLink"),
        }

    def list_message_ids(self, folder: str = "inbox", limit: int | None = None) -> list[str]:
        """Paginates (following Graph's @odata.nextLink) collecting message
        ids only - used for bulk ingestion ("select all" / "latest N")."""
        ids: list[str] = []
        folder_id = FOLDER_IDS.get(folder, "inbox")
        path = f"/me/mailFolders/{folder_id}/messages" if folder_id else "/me/messages"
        params = {"$top": 50, "$select": "id", "$orderby": "receivedDateTime desc"}
        raw = self._api_request(path, params)

        while True:
            ids.extend(m["id"] for m in raw.get("value", []))
            if limit and len(ids) >= limit:
                return ids[:limit]
            next_link = raw.get("@odata.nextLink")
            if not next_link:
                break
            raw = self._api_request(next_link)
        return ids

    def get_message(self, message_id: str) -> dict:
        raw = self._api_request(f"/me/messages/{message_id}", {"$select": _MESSAGE_SELECT_FULL})

        body = raw.get("body") or {}
        attachments = []
        if raw.get("hasAttachments"):
            att_raw = self._api_request(
                f"/me/messages/{message_id}/attachments",
                {"$select": "id,name,contentType,size,isInline"},
            )
            attachments = [
                {
                    "attachment_id": a["id"],
                    "filename": a.get("name") or "attachment",
                    "mime_type": a.get("contentType", ""),
                    "size": a.get("size", 0),
                }
                for a in att_raw.get("value", [])
                if not a.get("isInline")
            ]

        return {
            "id": raw["id"],
            "subject": raw.get("subject") or "(No Subject)",
            "from": _format_recipients([{"emailAddress": (raw.get("from") or {}).get("emailAddress", {})}]) if raw.get("from") else "",
            "to": _format_recipients(raw.get("toRecipients")),
            "cc": _format_recipients(raw.get("ccRecipients")),
            "date": raw.get("receivedDateTime", ""),
            "body_text": body.get("content", "") if body.get("contentType") == "text" else _strip_html(body.get("content", "")),
            "body_html": body.get("content", "") if body.get("contentType") == "html" else "",
            "attachments": attachments,
        }

    def download_attachment(self, message_id: str, attachment_id: str, filename: str = "attachment") -> tuple[bytes, str]:
        token = self._get_valid_token()
        url = f"{GRAPH_BASE}/me/messages/{message_id}/attachments/{attachment_id}/$value"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), filename
        except urllib.error.HTTPError as exc:
            detail = extract_http_error_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Failed to download Outlook attachment: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Failed to download Outlook attachment: {str(exc)}")


def _strip_html(html: str) -> str:
    """Best-effort plain-text fallback when Graph returns an HTML body and
    no text-format alternative - good enough for embedding/search, not a
    full HTML renderer."""
    import re
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_outlook_connector(user_id: str) -> OutlookConnector:
    return OutlookConnector(user_id)
