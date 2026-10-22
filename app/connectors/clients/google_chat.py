"""Read-only Google Chat connector.

Deliberately has NO OAuth login/callback of its own. It shares the exact
same Google OAuth grant as the Gmail connector (see gmail.py's SCOPES, which
already request the Chat read scopes alongside Gmail's) - one consent screen
covers both. app.connectors.router's gmail_callback mirrors the token it
receives into a second "google_chat" token_store row right after saving the
"gmail" one, and /connectors/google-chat/auth/login just returns the same
authorization URL Gmail's login does. Disconnecting either provider deletes
both rows (see router.py's _LINKED_OAUTH_PROVIDERS) - from the user's
perspective, Gmail and Google Chat are always connected/disconnected
together, because underneath they're one Google sign-in, not two.
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

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CHAT_API_BASE = "https://chat.googleapis.com/v1"

PROVIDER = "google_chat"

# Google Chat's "Space" resource covers direct messages, group chats, and
# named spaces alike (distinguished only by spaceType) - these map to the
# tabs the frontend renders. "all" sends no filter at all.
SPACE_TYPE_FILTERS = {
    "all": None,
    "space": 'space_type = "SPACE"',
    "group_chat": 'space_type = "GROUP_CHAT"',
    "direct_message": 'space_type = "DIRECT_MESSAGE"',
}


class GoogleChatConnector:
    def __init__(self, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        self.user_id = user_id
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=500,
                detail="GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not configured in .env",
            )

        self._oauth = OAuth2TokenClient(user_id, PROVIDER, self.client_id, self.client_secret, TOKEN_ENDPOINT)
        self._self_resource: str | None = None

    def _get_valid_token(self) -> str:
        row = self._oauth.stored_token()
        if row is None:
            raise HTTPException(
                status_code=401,
                detail="Google Chat is not connected. Connect Gmail (they share one Google sign-in), "
                       "or visit /connectors/google-chat/auth/login directly.",
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail="Google Chat token expired and no refresh token available. Reconnect Gmail/Google Chat.",
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
                detail=f"Failed to refresh Google Chat token: {str(exc)}. Please reconnect.",
            )

        new_access = new_data["access_token"]
        self._oauth.save(new_access, refresh_token, self._oauth.expires_at_from(new_data))
        return new_access

    def _api_request(self, path: str, params: dict | None = None, token: str | None = None) -> dict:
        token = token or self._get_valid_token()
        url = f"{CHAT_API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = extract_http_error_detail(exc)
            if exc.code == 401:
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(401, f"Google Chat access token is invalid or expired{suffix}")
            if exc.code == 403:
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(
                    403,
                    "Google Chat API access denied - make sure the Google Chat API is enabled "
                    f"on the Google Cloud project behind GOOGLE_CLIENT_ID{suffix}.",
                )
            if exc.code == 404:
                raise HTTPException(404, "Google Chat resource not found")
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Google Chat API error: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to reach Google Chat API: {str(exc)}")

    def get_status(self) -> dict:
        row = token_store.get_token(self.user_id, PROVIDER)
        if row is None:
            return {
                "authenticated": False,
                "message": "Not connected. Connect Gmail (they share one Google sign-in), "
                           "or visit /connectors/google-chat/auth/login directly.",
            }
        try:
            self._api_request("/spaces", {"pageSize": 1})
            return {"authenticated": True}
        except HTTPException as e:
            return {"authenticated": False, "detail": e.detail}

    def _get_self_resource(self) -> str | None:
        """Chat's User.name is "users/{id}" where {id} is the same numeric
        Google account id the OAuth userinfo endpoint returns - there's no
        "me" alias on spaces.members like Graph's /me, so a direct-message
        space's other participant can only be told apart from the signed-in
        user by resolving this once and excluding it from that space's
        member list. Cached on the instance for the life of one request."""
        if self._self_resource is None:
            try:
                token = self._get_valid_token()
                req = urllib.request.Request(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {token}"}, method="GET",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    account_id = json.loads(resp.read().decode("utf-8")).get("id")
                self._self_resource = f"users/{account_id}" if account_id else ""
            except Exception:
                self._self_resource = ""
        return self._self_resource or None

    def _space_name(self, s: dict) -> str:
        display_name = s.get("displayName")
        if display_name:
            return display_name
        others = self._other_member_names(s["name"])
        if others:
            return ", ".join(others)
        return "Direct message" if s.get("spaceType") == "DIRECT_MESSAGE" else s["name"]

    def _other_member_names(self, space_name: str) -> list[str]:
        """space_name is a full resource path, e.g. "spaces/AAAAAA"."""
        try:
            raw = self._api_request(f"/{space_name}/members", {"filter": 'member.type = "HUMAN"', "pageSize": 10})
        except HTTPException:
            return []
        self_resource = self._get_self_resource()
        return [
            m.get("member", {}).get("displayName")
            for m in raw.get("memberships", [])
            if m.get("member", {}).get("displayName") and m.get("member", {}).get("name") != self_resource
        ]

    def list_spaces(self, space_type: str = "all", page_size: int = 50, page_token: str = "") -> dict:
        params = {"pageSize": min(max(page_size, 1), 100)}
        space_filter = SPACE_TYPE_FILTERS.get(space_type)
        if space_filter:
            params["filter"] = space_filter
        if page_token:
            params["pageToken"] = page_token

        raw = self._api_request("/spaces", params)
        spaces = [
            {
                "id": s["name"].split("/")[-1],
                "name": self._space_name(s),
                "space_type": s.get("spaceType", "SPACE"),
            }
            for s in raw.get("spaces", [])
        ]
        return {"spaces": spaces, "next_page_token": raw.get("nextPageToken")}

    def list_space_ids(self, space_type: str = "all", limit: int | None = None) -> list[str]:
        """Paginates through every space matching space_type, id-only - used
        for bulk ingestion ("select all") where we may need hundreds of ids
        and will re-fetch each space's full message history anyway."""
        ids: list[str] = []
        page_token = ""
        while True:
            page = self.list_spaces(space_type=space_type, page_size=100, page_token=page_token)
            ids.extend(s["id"] for s in page["spaces"])
            if limit and len(ids) >= limit:
                return ids[:limit]
            page_token = page.get("next_page_token")
            if not page_token:
                break
        return ids

    def list_messages(self, space_id: str, page_size: int = 100, page_token: str = "") -> dict:
        params = {"pageSize": min(max(page_size, 1), 1000)}
        if page_token:
            params["pageToken"] = page_token
        raw = self._api_request(f"/spaces/{space_id}/messages", params)
        return {"messages": raw.get("messages", []), "next_page_token": raw.get("nextPageToken")}

    def get_all_messages(self, space_id: str, max_messages: int = 500) -> list[dict]:
        """Paginates through every message in a space (oldest-first, as the
        API returns them), capped at max_messages so one chat/space can't
        make a single ingest call unbounded."""
        messages: list[dict] = []
        page_token = ""
        while True:
            page = self.list_messages(space_id, page_size=100, page_token=page_token)
            messages.extend(page["messages"])
            if len(messages) >= max_messages:
                return messages[:max_messages]
            page_token = page.get("next_page_token")
            if not page_token:
                break
        return messages

    def download_attachment(self, resource_name: str, filename: str = "attachment") -> tuple[bytes, str]:
        """`resource_name` is a message attachment's full resource path (e.g.
        "spaces/AAA/messages/BBB/attachments/CCC"), taken verbatim from
        message["attachment"][i]["attachmentDataRef"]["resourceName"]."""
        token = self._get_valid_token()
        url = f"{CHAT_API_BASE}/media/{resource_name}?alt=media"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), filename
        except urllib.error.HTTPError as exc:
            detail = extract_http_error_detail(exc)
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Failed to download Google Chat attachment: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Failed to download Google Chat attachment: {str(exc)}")


def get_google_chat_connector(user_id: str) -> GoogleChatConnector:
    return GoogleChatConnector(user_id)
