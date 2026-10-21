import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException

from app.connectors import token_store
from app.connectors.clients.oauth2_token_client import OAuth2TokenClient

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

PROVIDER = "gmail"


class GmailConnector:
    """Read-only Gmail connector using Google OAuth 2.0.

    Tokens are stored per-user in the `connector_tokens` table (via
    token_store), not in a shared flat JSON file like CONNECTOR_POC's
    original .gmail_token.json - that had no concept of "which user".
    """

    def __init__(self, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        self.user_id = user_id
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        self.redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/gmail/callback")

        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=500,
                detail="GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not configured in .env",
            )

        self._oauth = OAuth2TokenClient(user_id, PROVIDER, self.client_id, self.client_secret, TOKEN_ENDPOINT)

    def get_authorization_url(self) -> str:
        # The user_id rides through as `state` so the callback (which has no
        # Authorization header - the browser lands on it directly from Google)
        # can recover which user initiated the flow.
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent select_account",
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
        }
        try:
            token_data = self._oauth.post_token_endpoint(form)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise HTTPException(status_code=400, detail=f"Token exchange failed: {err_body}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to reach Google OAuth: {str(e)}")

        expires_at = self._oauth.expires_at_from(token_data)

        # If previous refresh token exists and new one is not returned, preserve it
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
                detail="Gmail is not authenticated. Please authenticate by visiting /connectors/gmail/auth/login",
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail="Gmail token expired and no refresh token available. Visit /connectors/gmail/auth/login",
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
                detail=f"Failed to refresh Gmail token: {str(exc)}. Please re-authenticate.",
            )

        new_access = new_data["access_token"]
        self._oauth.save(new_access, refresh_token, self._oauth.expires_at_from(new_data))
        return new_access

    def _api_request(self, endpoint: str, params: dict | None = None, token: str | None = None) -> dict:
        # `token` lets a caller resolve it once and share it across many
        # calls (see list_messages' parallel detail fetches) instead of
        # every call independently re-reading it from the DB.
        token = token or self._get_valid_token()
        url = f"{GMAIL_API_BASE}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise HTTPException(401, "Gmail access token is invalid or expired")
            if exc.code == 403:
                raise HTTPException(403, "Gmail API access denied or insufficient permissions")
            if exc.code == 404:
                raise HTTPException(404, "Gmail resource not found")
            raise HTTPException(502, f"Gmail API error: HTTP {exc.code}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to reach Gmail API: {str(exc)}")

    def get_status(self) -> dict:
        row = token_store.get_token(self.user_id, PROVIDER)
        if row is None:
            return {"authenticated": False, "message": "Not authenticated. Visit /connectors/gmail/auth/login"}
        try:
            profile = self._api_request("/profile")
            return {
                "authenticated": True,
                "email_address": profile.get("emailAddress"),
                "messages_total": profile.get("messagesTotal"),
                "threads_total": profile.get("threadsTotal"),
            }
        except HTTPException as e:
            return {"authenticated": False, "detail": e.detail}

    def list_messages(self, max_results: int = 10, q: str = "", page_token: str = "") -> dict:
        params = {"maxResults": min(max(max_results, 1), 50)}
        if q:
            params["q"] = q
        if page_token:
            params["pageToken"] = page_token

        raw = self._api_request("/messages", params)
        message_summaries = raw.get("messages", [])

        # Resolved once and shared across every parallel detail fetch below -
        # it's the same token for all of them, so each thread re-reading it
        # from the DB independently would be up to 50 redundant round-trips.
        token = self._get_valid_token()

        def _fetch_item_meta(msg: dict) -> dict:
            msg_id = msg["id"]
            try:
                details = self._api_request(
                    f"/messages/{msg_id}",
                    {"format": "metadata", "metadataHeaders": ["Subject", "From", "Date", "To"]},
                    token=token,
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in details.get("payload", {}).get("headers", [])
                }
                return {
                    "id": msg_id,
                    "thread_id": msg.get("threadId"),
                    "subject": headers.get("subject", "(No Subject)"),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "date": headers.get("date", ""),
                    "snippet": details.get("snippet", ""),
                }
            except Exception:
                return {"id": msg_id, "thread_id": msg.get("threadId")}

        items = []
        if message_summaries:
            with ThreadPoolExecutor(max_workers=min(len(message_summaries), 8)) as executor:
                items = list(executor.map(_fetch_item_meta, message_summaries))

        return {
            "messages": items,
            "result_size_estimate": raw.get("resultSizeEstimate", len(items)),
            "next_page_token": raw.get("nextPageToken"),
        }

    def get_message(self, message_id: str) -> dict:
        raw = self._api_request(f"/messages/{message_id}", {"format": "full"})
        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

        body_text = ""
        body_html = ""
        attachments = []

        def _parse_parts(part):
            nonlocal body_text, body_html
            mime = part.get("mimeType", "")
            filename = part.get("filename", "")
            body = part.get("body", {})

            if filename and body.get("attachmentId"):
                attachments.append({
                    "attachment_id": body["attachmentId"],
                    "filename": filename,
                    "mime_type": mime,
                    "size": body.get("size", 0),
                })
            elif mime == "text/plain" and body.get("data"):
                try:
                    body_text = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
                except Exception:
                    pass
            elif mime == "text/html" and body.get("data"):
                try:
                    body_html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
                except Exception:
                    pass

            for subpart in part.get("parts", []):
                _parse_parts(subpart)

        _parse_parts(payload)

        return {
            "id": raw["id"],
            "thread_id": raw.get("threadId"),
            "subject": headers.get("subject", "(No Subject)"),
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "snippet": raw.get("snippet", ""),
            "body_text": body_text or raw.get("snippet", ""),
            "body_html": body_html,
            "attachments": attachments,
            "labels": raw.get("labelIds", []),
        }

    def download_attachment(self, message_id: str, attachment_id: str, filename: str = "attachment") -> tuple[bytes, str]:
        raw = self._api_request(f"/messages/{message_id}/attachments/{attachment_id}")
        data_b64 = raw.get("data")
        if not data_b64:
            raise HTTPException(404, "Attachment data not found")

        try:
            data = base64.urlsafe_b64decode(data_b64)
        except Exception:
            raise HTTPException(502, "Failed to decode attachment data")

        return data, filename


def get_gmail_connector(user_id: str) -> GmailConnector:
    return GmailConnector(user_id)
