import os
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException

from app.connectors import token_store
from app.connectors.clients._errors import extract_http_error_detail
from app.connectors.clients.oauth2_token_client import OAuth2TokenClient

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
]

PROVIDER = "google_drive"


class GoogleDriveConnector:
    """Read-only Google Drive connector using Google OAuth 2.0.

    Tokens are stored per-user in the `connector_tokens` table (via
    token_store) rather than a shared flat JSON file.
    """

    def __init__(self, user_id: str):
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        self.user_id = user_id
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        self.redirect_uri = os.getenv(
            "GOOGLE_DRIVE_REDIRECT_URI",
            "http://localhost:8000/google-drive/callback",
        )

        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=500,
                detail="GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not configured in .env",
            )

        self._oauth = OAuth2TokenClient(user_id, PROVIDER, self.client_id, self.client_secret, TOKEN_ENDPOINT)

    def get_authorization_url(self) -> str:
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
                detail="Google Drive is not authenticated. Please authenticate by visiting /connectors/google-drive/auth/login",
            )

        access_token = row["access_token"]
        refresh_token = row["refresh_token"]

        if self._oauth.is_expiring(row["expires_at"]):
            if not refresh_token:
                raise HTTPException(
                    status_code=401,
                    detail="Google Drive token expired and no refresh token available. Visit /connectors/google-drive/auth/login",
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
                detail=f"Failed to refresh Google Drive token: {str(exc)}. Please re-authenticate.",
            )

        new_access = new_data["access_token"]
        self._oauth.save(new_access, refresh_token, self._oauth.expires_at_from(new_data))
        return new_access

    def _api_request(self, endpoint: str, params: dict | None = None) -> dict:
        token = self._get_valid_token()
        url = f"{DRIVE_API_BASE}{endpoint}"
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
            detail = extract_http_error_detail(exc)
            if exc.code == 401:
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(401, f"Google Drive access token is invalid or expired{suffix}")
            if exc.code == 403:
                suffix = f": {detail}" if detail else ""
                raise HTTPException(403, f"Google Drive API access denied or insufficient permissions{suffix}")
            if exc.code == 404:
                raise HTTPException(404, "Google Drive resource not found")
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Google Drive API error: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to reach Google Drive API: {str(exc)}")

    def get_status(self) -> dict:
        row = token_store.get_token(self.user_id, PROVIDER)
        if row is None:
            return {
                "authenticated": False,
                "message": "Not authenticated. Visit /connectors/google-drive/auth/login",
            }
        try:
            about = self._api_request("/about", {"fields": "user,storageQuota"})
            user = about.get("user", {})
            storage = about.get("storageQuota", {})
            return {
                "authenticated": True,
                "email": user.get("emailAddress"),
                "email_address": user.get("emailAddress"),
                "display_name": user.get("displayName"),
                "storage_used": storage.get("usage"),
                "storage_limit": storage.get("limit"),
                "storage_used_in_drive": storage.get("usageInDrive"),
            }
        except HTTPException as e:
            return {"authenticated": False, "detail": e.detail}
        except Exception as e:
            return {"authenticated": False, "detail": str(e)}

    def list_files(
        self, max_results: int = 10, q: str = "", order_by: str = "modifiedTime desc", page_token: str = "",
    ) -> dict:
        page_size = min(max(max_results, 1), 100)
        params = {
            "pageSize": page_size,
            "orderBy": order_by,
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink, owners, iconLink)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        base_filter = "trashed = false"
        if q and q.strip():
            clean_q = q.strip()
            operators = ["contains", "=", "!=", "<", ">", " in ", "has "]
            if any(op in clean_q for op in operators):
                if "trashed" in clean_q.lower():
                    params["q"] = clean_q
                else:
                    params["q"] = f"{base_filter} and ({clean_q})"
            else:
                escaped = clean_q.replace("'", "\\'")
                params["q"] = f"{base_filter} and (name contains '{escaped}' or fullText contains '{escaped}')"
        else:
            params["q"] = base_filter

        raw = self._api_request("/files", params)
        raw_files = raw.get("files", [])

        items = []
        for f in raw_files:
            items.append({
                "id": f.get("id"),
                "name": f.get("name"),
                "mimeType": f.get("mimeType"),
                "modifiedTime": f.get("modifiedTime"),
                "size": int(f.get("size")) if f.get("size") is not None else None,
                "webViewLink": f.get("webViewLink"),
                "owners": [
                    {
                        "displayName": o.get("displayName"),
                        "emailAddress": o.get("emailAddress"),
                    }
                    for o in f.get("owners", [])
                ],
                "iconLink": f.get("iconLink"),
            })

        return {
            "files": items,
            "next_page_token": raw.get("nextPageToken"),
        }

    def list_file_ids_bulk(self, folder_id: str | None = None, limit: int = 500) -> list[dict]:
        """Returns up to `limit` non-folder file records ({id, name,
        mimeType, size}) for bulk ingest. folder_id=None lists every file
        across this user's My Drive + shared drives; a given folder_id
        instead walks that folder and all of its subfolders breadth-first,
        collecting files along the way."""
        fields = "nextPageToken, files(id, name, mimeType, size)"

        def _list_children(parent_id: str | None, remaining: int) -> list[dict]:
            children: list[dict] = []
            page_token = ""
            q = f"'{parent_id}' in parents and trashed = false" if parent_id else "trashed = false"
            while True:
                params = {
                    "pageSize": min(remaining, 1000) if remaining else 1000,
                    "fields": fields,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "q": q,
                }
                if page_token:
                    params["pageToken"] = page_token
                raw = self._api_request("/files", params)
                children.extend(raw.get("files", []))
                page_token = raw.get("nextPageToken")
                if not page_token or len(children) >= remaining:
                    break
            return children

        results: list[dict] = []

        if folder_id:
            queue = [folder_id]
            while queue and len(results) < limit:
                current = queue.pop(0)
                for f in _list_children(current, limit - len(results)):
                    if f.get("mimeType") == "application/vnd.google-apps.folder":
                        queue.append(f["id"])
                    else:
                        results.append(f)
                        if len(results) >= limit:
                            break
        else:
            for f in _list_children(None, limit):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                results.append(f)
                if len(results) >= limit:
                    break

        return results[:limit]

    def get_file_metadata(self, file_id: str) -> dict:
        params = {
            "fields": "*",
            "supportsAllDrives": "true",
        }
        return self._api_request(f"/files/{file_id}", params)

    def download_file(self, file_id: str) -> tuple[bytes, str]:
        metadata = self.get_file_metadata(file_id)
        name = metadata.get("name", file_id)
        mime_type = metadata.get("mimeType", "")
        token = self._get_valid_token()

        if mime_type == "application/vnd.google-apps.folder":
            raise HTTPException(status_code=400, detail="Cannot download a folder")

        export_mappings = {
            "application/vnd.google-apps.document": (
                "application/pdf",
                ".pdf",
            ),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xlsx",
            ),
            "application/vnd.google-apps.presentation": (
                "application/pdf",
                ".pdf",
            ),
            "application/vnd.google-apps.drawing": (
                "application/pdf",
                ".pdf",
            ),
        }

        if mime_type in export_mappings:
            export_mime, ext = export_mappings[mime_type]
            params = {"mimeType": export_mime}
            url = f"{DRIVE_API_BASE}/files/{file_id}/export?{urllib.parse.urlencode(params)}"
            download_name = name if name.lower().endswith(ext) else f"{name}{ext}"
        elif mime_type.startswith("application/vnd.google-apps."):
            export_mime = "application/pdf"
            params = {"mimeType": export_mime}
            url = f"{DRIVE_API_BASE}/files/{file_id}/export?{urllib.parse.urlencode(params)}"
            download_name = name if name.lower().endswith(".pdf") else f"{name}.pdf"
        else:
            params = {"alt": "media", "supportsAllDrives": "true"}
            url = f"{DRIVE_API_BASE}/files/{file_id}?{urllib.parse.urlencode(params)}"
            download_name = name

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read()
        except urllib.error.HTTPError as exc:
            detail = extract_http_error_detail(exc)
            if exc.code == 401:
                suffix = f" ({detail})" if detail else ""
                raise HTTPException(401, f"Google Drive access token is invalid or expired{suffix}")
            if exc.code == 403:
                suffix = f": {detail}" if detail else ""
                raise HTTPException(403, f"Google Drive API access denied or file cannot be downloaded{suffix}")
            if exc.code == 404:
                raise HTTPException(404, "Google Drive file not found")
            suffix = f": {detail}" if detail else ""
            raise HTTPException(502, f"Google Drive API error: HTTP {exc.code}{suffix}")
        except Exception as exc:
            raise HTTPException(502, f"Unable to download file from Google Drive: {str(exc)}")

        return content, download_name

    def get_file_text_content(self, file_id: str, mime_type: str = "") -> str:
        """Extract readable text content from a Google Drive file or document."""
        token = self._get_valid_token()
        if not mime_type:
            meta = self.get_file_metadata(file_id)
            mime_type = meta.get("mimeType", "")

        try:
            if mime_type == "application/vnd.google-apps.document":
                url = f"{DRIVE_API_BASE}/files/{file_id}/export?mimeType=text/plain"
            elif mime_type == "application/vnd.google-apps.spreadsheet":
                url = f"{DRIVE_API_BASE}/files/{file_id}/export?mimeType=text/csv"
            elif mime_type.startswith("text/") or mime_type in ["application/json", "application/javascript"]:
                url = f"{DRIVE_API_BASE}/files/{file_id}?alt=media"
            else:
                return ""

            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
                return content.decode("utf-8", errors="replace")[:3000]
        except Exception as e:
            return f"(Could not extract file text: {str(e)})"


def get_google_drive_connector(user_id: str) -> GoogleDriveConnector:
    return GoogleDriveConnector(user_id)
