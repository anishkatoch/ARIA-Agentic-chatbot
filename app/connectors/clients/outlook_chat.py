"""Read-only Microsoft Teams/Outlook personal chat connector (1:1 chats,
group chats, meeting chats - the "/me/chats" Graph resource), via delegated
user OAuth.

Deliberately has NO OAuth login/callback of its own - it shares the exact
same Microsoft OAuth grant as the Outlook connector (see outlook.py's
SCOPES, which already request Chat.Read alongside Mail.Read).
One consent screen covers both. app.connectors.router's outlook_callback
mirrors the token it receives into a second "outlook_chat" token_store row
right after saving the "outlook" one, and
/connectors/outlook-chat/auth/login just returns the same authorization URL
Outlook's login does. Disconnecting either provider deletes both rows (see
router.py's _LINKED_OAUTH_PROVIDERS) - from the user's perspective, Outlook
and Outlook Chat are always connected/disconnected together, because
underneath they're one Microsoft sign-in, not two.

Distinct from app/connectors/clients/teams.py, which reads CHANNEL messages
app-only (client-credentials, no per-user login, needs a team/channel id
picked in advance). This module reads the signed-in user's own 1:1/group
chats instead, the Teams/Outlook equivalent of Google Chat's "spaces".
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

_TENANT = os.getenv("MICROSOFT_TENANT_ID") or "common"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{_TENANT}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

PROVIDER = "outlook_chat"

# Graph's /me/chats chatType values map to the tabs the frontend renders.
CHAT_TYPE_FILTERS = {
    "all": None,
    "one_on_one": "oneOnOne",
    "group": "group",
    "meeting": "meeting",
}


class OutlookChatConnector:
    def __init__(self, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        self.user_id = user_id
        self.client_id = os.getenv("MICROSOFT_CLIENT_ID")
        self.client_secret = os.getenv("MICROSOFT_CLIENT_SECRET")

        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=500,
                detail="MICROSOFT_CLIENT_ID or MICROSOFT_CLIENT_SECRET not configured in .env",
            )

        self._oauth = OAuth2TokenClient(user_id, PROVIDER, self.client_id, self.client_secret, TOKEN_ENDPOINT)
        self._self_id: str | None = None

    def _get_valid_token(self) -> str:
        row = self._oauth.stored_token()
        if row is None:
            raise HTTPException(
                status_code=401,
                detail="Outlook Chat is not connected. Connect Outlook (they share one Microsoft sign-in), "
                       "or visit /connectors/outlook-chat/auth/login directly.",
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail="Outlook Chat token expired and no refresh token available. Reconnect Outlook/Outlook Chat.",
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
            raise HTTPException(
                status_code=401,
                detail=f"Failed to refresh Outlook Chat token: {str(exc)}. Please reconnect.",
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
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(
                    401,
                    f"Microsoft Graph rejected the Outlook Chat access token{suffix}. "
                    "Reconnect Outlook - if that doesn't help, the signed-in account "
                    "likely has no Microsoft 365 mailbox/license.",
                )
            if exc.code == 403:
                # Graph's most common 403 here: "Failed to get license
                # information for the user" - i.e. no Teams/M365 license, not
                # a permissions problem at all. Surfacing the real message
                # (instead of a generic "access denied") is the difference
                # between a user retrying forever vs. knowing to use a
                # licensed account.
                suffix = f": {detail}" if detail else ""
                raise HTTPException(403, f"Outlook Chat/Graph API access denied or insufficient permissions{suffix}")
            if exc.code == 404:
                raise HTTPException(404, "Outlook Chat resource not found")
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Microsoft Graph error: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to reach Microsoft Graph: {str(exc)}")

    def get_status(self) -> dict:
        row = token_store.get_token(self.user_id, PROVIDER)
        if row is None:
            return {
                "authenticated": False,
                "message": "Not connected. Connect Outlook (they share one Microsoft sign-in), "
                           "or visit /connectors/outlook-chat/auth/login directly.",
            }
        try:
            self._api_request("/me/chats", {"$top": 1})
            return {"authenticated": True}
        except HTTPException as e:
            return {"authenticated": False, "detail": e.detail}

    def _get_self_id(self) -> str | None:
        """Graph's /me/chats never says which of a chat's members is the
        signed-in user, so a oneOnOne/group chat with no topic needs this to
        know which member to exclude when building a display name from the
        other participant(s). Cached on the instance since it can't change
        for the lifetime of one request."""
        if self._self_id is None:
            try:
                self._self_id = self._api_request("/me", {"$select": "id"}).get("id")
            except HTTPException:
                self._self_id = ""
        return self._self_id or None

    def _chat_name(self, c: dict) -> str:
        topic = c.get("topic")
        if topic:
            return topic
        self_id = self._get_self_id()
        others = [
            m.get("displayName") for m in c.get("members", [])
            if m.get("displayName") and m.get("userId") != self_id
        ]
        if others:
            return ", ".join(others)
        return "Direct message" if c.get("chatType") == "oneOnOne" else "Group chat"

    def list_chats(self, chat_type: str = "all", page_size: int = 50, next_link: str = "") -> dict:
        if next_link:
            raw = self._api_request(next_link)
        else:
            params = {"$top": min(max(page_size, 1), 50), "$expand": "members"}
            type_filter = CHAT_TYPE_FILTERS.get(chat_type)
            if type_filter:
                params["$filter"] = f"chatType eq '{type_filter}'"
            raw = self._api_request("/me/chats", params)

        chats = [
            {
                "id": c["id"],
                "name": self._chat_name(c),
                "chat_type": c.get("chatType", "oneOnOne"),
            }
            for c in raw.get("value", [])
        ]
        return {"chats": chats, "next_link": raw.get("@odata.nextLink")}

    def list_chat_ids(self, chat_type: str = "all", limit: int | None = None) -> list[str]:
        ids: list[str] = []
        page = self.list_chats(chat_type=chat_type, page_size=50)
        while True:
            ids.extend(c["id"] for c in page["chats"])
            if limit and len(ids) >= limit:
                return ids[:limit]
            next_link = page.get("next_link")
            if not next_link:
                break
            page = self.list_chats(chat_type=chat_type, next_link=next_link)
        return ids

    def get_all_messages(self, chat_id: str, max_messages: int = 500) -> list[dict]:
        """Paginates through a chat's messages (newest-first, as Graph
        returns them), capped at max_messages to bound a single ingest."""
        messages: list[dict] = []
        raw = self._api_request(f"/me/chats/{chat_id}/messages", {"$top": 50})
        while True:
            messages.extend(raw.get("value", []))
            if len(messages) >= max_messages:
                return messages[:max_messages]
            next_link = raw.get("@odata.nextLink")
            if not next_link:
                break
            raw = self._api_request(next_link)
        return messages


def get_outlook_chat_connector(user_id: str) -> OutlookChatConnector:
    return OutlookChatConnector(user_id)
