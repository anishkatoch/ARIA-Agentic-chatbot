"""Source-system connector endpoints - ported from CONNECTOR_POC/app/main.py.

Three routers are exported:

- `router` (prefix="/connectors") - all data/listing endpoints, login-initiation
  endpoints for the OAuth-based connectors, and the ingest bridge endpoint.
  This whole prefix is gated behind the existing auth middleware (see
  app/main.py's _PROTECTED_PREFIXES), so every route here requires a logged-in
  user - same as /chat/ and /upload/. On top of that, every route on this
  router also requires a valid connectors-unlock token (see access_gate.py
  and the `require_connectors_unlock` dependency below) - a second, separate
  secret the deployment owner sets in CONNECTORS_ACCESS_KEY, since a connected
  Gmail/GitHub session is real access to someone's real third-party accounts,
  not just app data.

- `unlock_router` (prefix="/connectors") - just the one POST /connectors/unlock
  endpoint used to exchange the raw access key for a short-lived unlock token.
  Deliberately NOT behind `require_connectors_unlock` itself (a lock can't
  require its own key to ask about opening it).

- `oauth_callback_router` (no prefix) - the OAuth callback endpoints, which
  MUST live at a literal fixed path matching GOOGLE_REDIRECT_URI /
  GOOGLE_DRIVE_REDIRECT_URI (the browser lands on these directly from Google
  with no Authorization header, so they can't be gated behind either Bearer
  auth or the unlock token - they must stay in app/main.py's
  _PUBLIC_PREFIXES). Reaching this point already required completing the
  gated /auth/login step first, so this stays safe.

This module (and everything else in app/connectors/ except pipeline_bridge.py)
has no knowledge of this host app's RAG internals - the one place that
knowledge lives is app.connectors.pipeline_bridge.ingest_bytes(), which this
router calls after fetching connector-sourced bytes. See
app/connectors/README.md for the full picture of how this package is bounded.
"""
import asyncio
import logging
import os
import time
import uuid
from io import BytesIO
from typing import AsyncGenerator
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.config import cfg
from app.connectors import access_gate, token_store, credential_store
from app.connectors.clients.github import GitHubConnector, get_github_connector
from app.connectors.clients.gmail import get_gmail_connector
from app.connectors.clients.google_drive import get_google_drive_connector
from app.connectors.clients.google_chat import get_google_chat_connector
from app.connectors.clients.outlook import get_outlook_connector
from app.connectors.clients.outlook_chat import get_outlook_chat_connector
from app.connectors.clients.microsoft_graph import MicrosoftGraphError
from app.connectors.clients.sharepoint import SharePointConnector
from app.connectors.clients.teams import TeamsConnector
from app.connectors.clients.azure_devops import get_azure_devops_connector, AzureDevOpsConnector, AzureDevOpsError
from app.connectors import pipeline_bridge
from app.routers.upload import sse, SSE_HEADERS

logger = logging.getLogger(__name__)


def _content_disposition_header(filename: str) -> dict[str, str]:
    """Builds a Content-Disposition header for a file download - shared by
    every connector's download endpoint instead of each one reimplementing
    its own filename handling (previously: three endpoints manually
    stripped quote/CR/LF characters, one used only the RFC 5987 extended
    form, and one sent no filename at all). Includes both a plain ASCII-safe
    `filename=` fallback for older clients and the precise RFC 5987
    `filename*=UTF-8''...` form every modern browser actually prefers, so a
    real filename with non-ASCII or special characters is never mangled or
    allowed to break the header."""
    clean = filename.replace("\r", "").replace("\n", "")
    ascii_fallback = clean.encode("ascii", "replace").decode("ascii").replace('"', "'") or "download"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"}


def _user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return user["id"]


def _normalize_provider(provider: str) -> str:
    """Canonical provider id (matches CONNECTOR_CATALOG's "id" field, and
    what every client module hardcodes as its provider string when reading/
    writing token_store or credential_store) - e.g. "google_drive",
    "azure_devops". Most of this router's URLs instead use CONNECTOR_CATALOG's
    "path" field, which is hyphenated for some providers (e.g. "google-drive",
    "azure-devops" - see /auth/login, /status's login_url, every browse
    endpoint). /disconnect and /credentials act on token_store/credential_store
    directly, so they need the canonical id - this converts whichever form
    the caller sent."""
    return provider.replace("-", "_")


def require_connectors_unlock(
    request: Request,
    x_connectors_unlock: str = Header(default=""),
) -> None:
    """Dependency applied to every route on `router` below. Returning normally
    lets the request through; raising blocks it. A valid Bearer token alone is
    not enough here - the caller must also present a valid unlock token,
    obtained from POST /connectors/unlock, scoped to this exact user."""
    if not access_gate.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Connectors are disabled: CONNECTORS_ACCESS_KEY is not configured on this server.",
        )
    user_id = _user_id(request)
    if not access_gate.verify_unlock_token(x_connectors_unlock, user_id):
        raise HTTPException(
            status_code=403,
            detail="Connectors are locked. POST /connectors/unlock with the access key first.",
        )


router = APIRouter(
    prefix="/connectors", tags=["connectors"],
    dependencies=[Depends(require_connectors_unlock)],
)
unlock_router = APIRouter(prefix="/connectors", tags=["connectors"])
oauth_callback_router = APIRouter(tags=["connectors-oauth-callbacks"])


@unlock_router.post("/unlock")
def unlock_connectors(request: Request, body: dict = Body(...)):
    """Exchanges the shared CONNECTORS_ACCESS_KEY for a short-lived, user-scoped
    token that subsequent /connectors/* calls send back in X-Connectors-Unlock.
    The raw key itself is never stored or echoed back - only this derived token
    leaves the server, and it proves nothing beyond "this user supplied the
    real key within the last few hours"."""
    user_id = _user_id(request)
    if not access_gate.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Connectors are disabled: CONNECTORS_ACCESS_KEY is not configured on this server.",
        )
    access_key = (body or {}).get("access_key", "")
    if not access_gate.verify_access_key(access_key):
        # 403, not 401 - this app's frontend treats any 401 as "your login
        # session is invalid" and force-logs-out the user. A wrong connectors
        # access key is a completely different thing (the login is fine, this
        # one extra permission just isn't granted yet), so it must not collide.
        raise HTTPException(status_code=403, detail="Incorrect access key.")
    return {"unlock_token": access_gate.issue_unlock_token(user_id)}


# ==========================================
# Status - real per-connector configured/connected state (not demo data)
# ==========================================

# id: matches /{provider}/ingest and token_store provider keys (underscores).
# path: matches this router's URL segments (hyphens where the route uses them).
CONNECTOR_CATALOG = [
    {"id": "github",       "path": "github",        "name": "GitHub",            "type": "code",               "auth": "byok"},
    {"id": "gmail",        "path": "gmail",         "name": "Gmail",             "type": "email",              "auth": "oauth"},
    {"id": "google_drive", "path": "google-drive",  "name": "Google Drive",      "type": "documents",          "auth": "oauth"},
    {"id": "google_chat",  "path": "google-chat",   "name": "Google Chat",       "type": "chat",               "auth": "oauth"},
    {"id": "outlook",      "path": "outlook",       "name": "Outlook",           "type": "email",              "auth": "oauth"},
    {"id": "outlook_chat", "path": "outlook-chat",  "name": "Outlook Chat",      "type": "chat",               "auth": "oauth"},
    {"id": "sharepoint",   "path": "sharepoint",    "name": "SharePoint",        "type": "documents",          "auth": "config"},
    {"id": "teams",        "path": "teams",         "name": "Microsoft Teams",   "type": "chat",               "auth": "config"},
    {"id": "azure_devops", "path": "azure-devops",  "name": "Azure DevOps Repos","type": "code",               "auth": "byok"},
]

# auth="byok" ("bring your own key") connectors: a user can enter their own
# credentials here to override the shared app-level env var default for just
# their own requests (see app.connectors.credential_store). Field shape is
# per-provider since each one's credential form is different - exposed via
# /status's credential_fields so the frontend builds the right form without
# duplicating this list.
BYOK_CREDENTIAL_FIELDS = {
    "github": [
        {"key": "token", "label": "Personal Access Token", "type": "password"},
    ],
    "azure_devops": [
        {"key": "org_url", "label": "Organization URL", "type": "text", "placeholder": "https://dev.azure.com/your-org"},
        {"key": "pat", "label": "Personal Access Token", "type": "password"},
    ],
}


def _is_placeholder(*values: str) -> bool:
    """True if any value still looks like the literal placeholder text written
    into .env/.env.example/env.example (e.g. "your-github-personal-access-token"),
    so an unfilled template doesn't get reported as a real, working credential."""
    return any("your-" in (v or "").lower() for v in values)


def _is_configured(provider_id: str) -> bool:
    if provider_id == "github":
        token = os.getenv("GITHUB_TOKEN", "")
        return bool(token) and not _is_placeholder(token)
    if provider_id in ("gmail", "google_drive", "google_chat"):
        vals = (os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", ""))
        return all(vals) and not _is_placeholder(*vals)
    if provider_id in ("sharepoint", "teams"):
        vals = (
            os.getenv("MICROSOFT_TENANT_ID", ""),
            os.getenv("MICROSOFT_CLIENT_ID", ""),
            os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        )
        return all(vals) and not _is_placeholder(*vals)
    if provider_id in ("outlook", "outlook_chat"):
        # Delegated user OAuth via the "common" authority - unlike
        # sharepoint/teams' app-only flow, this doesn't require a specific
        # MICROSOFT_TENANT_ID to be set.
        vals = (os.getenv("MICROSOFT_CLIENT_ID", ""), os.getenv("MICROSOFT_CLIENT_SECRET", ""))
        return all(vals) and not _is_placeholder(*vals)
    if provider_id == "azure_devops":
        vals = (os.getenv("AZURE_DEVOPS_ORG_URL", ""), os.getenv("AZURE_DEVOPS_PAT", ""))
        return all(vals) and not _is_placeholder(*vals)
    return False


def _delete_token_and_credentials(user_id: str, provider: str) -> None:
    """Deletes from both connector_tokens and connector_credentials in a
    single DB session/commit, instead of calling token_store.delete_token()
    and credential_store.delete_credentials() separately (each opens and
    closes its own session - roughly doubles the Supabase round-trip cost of
    one disconnect click)."""
    try:
        from app.db.session import get_session_factory
        factory = get_session_factory()
        if factory is None:
            return
        db = factory()
    except Exception as e:
        logger.warning(f"[CONNECTORS] disconnect DB session unavailable: {e}")
        return
    try:
        from app.connectors.models import ConnectorToken, ConnectorCredential
        db.query(ConnectorToken).filter(
            ConnectorToken.user_id == user_id, ConnectorToken.provider == provider
        ).delete()
        db.query(ConnectorCredential).filter(
            ConnectorCredential.user_id == user_id, ConnectorCredential.provider == provider
        ).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[CONNECTORS] disconnect failed for {provider}/{user_id}: {e}")
    finally:
        db.close()


# Gmail+Google Chat (one Google OAuth grant) and Outlook+Outlook Chat (one
# Microsoft OAuth grant) each share a single underlying token - see
# gmail.py/outlook.py's SCOPES and gmail_callback/outlook_callback's
# token-mirroring below. From the user's perspective, each pair is always
# connected/disconnected together, so disconnecting either half of a pair
# must delete both its token rows, not just the one the user clicked.
_LINKED_OAUTH_PROVIDERS = {
    "gmail": ("gmail", "google_chat"),
    "google_chat": ("gmail", "google_chat"),
    "outlook": ("outlook", "outlook_chat"),
    "outlook_chat": ("outlook", "outlook_chat"),
}


@router.post("/{provider}/disconnect")
def disconnect_connector(provider: str, request: Request):
    """Removes this user's stored OAuth token (Gmail/Google Drive/Google Chat/
    Outlook/Outlook Chat)
    and/or their own API-key credentials (GitHub/Azure DevOps)
    for `provider` - whichever applies, the other is a harmless no-op. /status
    then shows it as not-connected (oauth) or falls back to the shared env var
    default if one is configured (byok). A fresh /{provider}/auth/login for an
    oauth provider can link a different account (each client requests the
    provider's account-chooser / re-login prompt, not a silent same-account
    reauth - see get_authorization_url in gmail.py and google_drive.py)."""
    provider = _normalize_provider(provider)
    user_id = _user_id(request)
    for linked in _LINKED_OAUTH_PROVIDERS.get(provider, (provider,)):
        _delete_token_and_credentials(user_id, linked)
    return {"disconnected": True, "provider": provider}


@router.post("/{provider}/credentials")
def save_connector_credentials(provider: str, request: Request, body: dict = Body(...)):
    """Lets this user "bring their own key" for a byok connector (GitHub,
    Azure DevOps), overriding the shared app-level env var default for their
    own requests only - every other user is unaffected."""
    provider = _normalize_provider(provider)
    if provider not in BYOK_CREDENTIAL_FIELDS:
        raise HTTPException(400, f"'{provider}' does not support per-user credentials.")
    fields = BYOK_CREDENTIAL_FIELDS[provider]
    body = body or {}
    missing = [f["key"] for f in fields if not str(body.get(f["key"], "")).strip()]
    if missing:
        raise HTTPException(400, f"Missing required field(s): {', '.join(missing)}")
    creds = {f["key"]: str(body[f["key"]]).strip() for f in fields}

    # Validates the credentials actually work against the real API before
    # persisting them - previously this only checked the fields were
    # non-empty, so e.g. a non-URL typed into Azure DevOps' "Organization
    # URL" field, or a password typed into the PAT field by mistake, would
    # save "successfully", the row would show "Connected", and the actual
    # rejection would only surface later as a confusing error the first time
    # the connector was used (this has happened twice with different bad
    # values in the same field - format-only validation wasn't enough).
    if provider == "azure_devops":
        try:
            AzureDevOpsConnector(org_url=creds["org_url"], pat=creds["pat"]).list_projects()
        except AzureDevOpsError as exc:
            raise HTTPException(400, str(exc))
    elif provider == "github":
        try:
            GitHubConnector(token=creds["token"])._request("/user")
        except HTTPException as exc:
            raise HTTPException(400, f"Couldn't verify this GitHub token: {exc.detail}")

    user_id = _user_id(request)
    if not credential_store.save_credentials(user_id, provider, creds):
        raise HTTPException(503, "Could not save credentials - database unavailable.")
    return {"saved": True, "provider": provider}


@router.get("/status")
def connectors_status(request: Request):
    """Real per-user connector status - unlike CONNECTOR_POC's old endpoint,
    nothing here is hardcoded demo data. 'configured' reflects actual env vars;
    'connected' additionally checks the per-user token for OAuth connectors."""
    user_id = _user_id(request)
    # One query each for tokens/credentials across ALL providers, instead of
    # a separate DB session open/close per oauth or byok provider in the loop
    # below - this alone took /status from ~4.7s down to well under 1s.
    connected_tokens = token_store.get_connected_providers(user_id)
    own_credential_providers = credential_store.get_own_credential_providers(user_id)

    sources = []
    for meta in CONNECTOR_CATALOG:
        shared_configured = _is_configured(meta["id"])
        own_credentials = False
        if meta["auth"] == "oauth":
            configured = shared_configured
            connected = configured and meta["id"] in connected_tokens
        elif meta["auth"] == "byok":
            # "connected" mirrors oauth's meaning exactly: THIS user has their
            # own key on file - not "a shared admin default happens to work."
            # The shared env var (shared_configured) is still used as a
            # fallback by the connector client itself if they haven't set one,
            # but the UI's Connect/Disconnect toggle is per-user, same as oauth.
            own_credentials = meta["id"] in own_credential_providers
            configured = shared_configured or own_credentials
            connected = own_credentials
        else:
            configured = shared_configured
            connected = configured
        sources.append({
            **meta,
            "configured": configured,
            "connected": connected,
            "own_credentials": own_credentials,
            "login_url": f"/connectors/{meta['path']}/auth/login" if meta["auth"] == "oauth" else None,
            "credential_fields": BYOK_CREDENTIAL_FIELDS.get(meta["id"]) if meta["auth"] == "byok" else None,
        })
    return {
        "connected_count": sum(1 for s in sources if s["connected"]),
        "total_count": len(sources),
        "sources": sources,
    }


# ==========================================
# GitHub Connector Endpoints
# ==========================================
@router.get("/github/repos")
def list_github_repositories(
    request: Request,
    visibility: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
):
    connector = get_github_connector(_user_id(request))
    return connector.list_repositories(visibility=visibility, page=page, per_page=per_page)


@router.get("/github/repos/{owner}/{repo}/contents")
def list_github_contents(
    owner: str,
    repo: str,
    request: Request,
    path: str = Query(default=""),
    ref: str | None = Query(default=None),
):
    connector = get_github_connector(_user_id(request))
    return connector.list_contents(owner, repo, path, ref)


@router.get("/github/repos/{owner}/{repo}/file")
def download_github_file(
    owner: str,
    repo: str,
    request: Request,
    path: str = Query(...),
    ref: str | None = Query(default=None),
):
    connector = get_github_connector(_user_id(request))
    content, filename = connector.download_file(owner, repo, path, ref)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_content_disposition_header(filename),
    )


# ==========================================
# Gmail Connector Endpoints (Google OAuth2 + Gmail API)
# ==========================================

# Gmail's standard category tabs, mapped to the labelIds the API expects.
# "all" omits labelIds entirely - Gmail's default list (no labelIds, no q)
# already excludes only SPAM/TRASH, same as the "All Mail" view.
GMAIL_CATEGORY_LABELS: dict[str, list[str] | None] = {
    "inbox":      ["INBOX"],
    "spam":       ["SPAM"],
    "promotions": ["CATEGORY_PROMOTIONS"],
    "social":     ["CATEGORY_SOCIAL"],
    "updates":    ["CATEGORY_UPDATES"],
    "forums":     ["CATEGORY_FORUMS"],
    "sent":       ["SENT"],
    "all":        None,
}


def _gmail_label_ids(label: str) -> list[str] | None:
    label = (label or "all").lower()
    if label not in GMAIL_CATEGORY_LABELS:
        raise HTTPException(400, f"Unknown label '{label}'. Allowed: {', '.join(GMAIL_CATEGORY_LABELS)}")
    return GMAIL_CATEGORY_LABELS[label]


@router.get("/gmail/auth/login")
def gmail_login(request: Request):
    # Returns the auth URL as JSON rather than a redirect - this endpoint is
    # behind the auth middleware (needs the Bearer header), but a raw browser
    # navigation can't send that header. The frontend fetches this with the
    # header, then does window.location.href = data.auth_url itself.
    connector = get_gmail_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/gmail/status")
def gmail_status(request: Request):
    connector = get_gmail_connector(_user_id(request))
    return connector.get_status()


@router.get("/gmail/labels")
def gmail_list_labels():
    """The fixed set of category tabs the frontend renders (Inbox, Spam,
    Promotions, Social, Updates, Forums, Sent, All) - these map to Gmail's
    own system/category labelIds, not a live call to the Gmail API."""
    return {"labels": list(GMAIL_CATEGORY_LABELS.keys())}


@router.get("/gmail/messages")
def gmail_list_messages(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=50),
    q: str = Query(default="", description="Search query e.g. from:someone or has:attachment"),
    page_token: str = Query(default="", description="next_page_token from a previous page to continue listing"),
    label: str = Query(default="all", description="One of: " + ", ".join(GMAIL_CATEGORY_LABELS)),
):
    connector = get_gmail_connector(_user_id(request))
    label_ids = _gmail_label_ids(label)
    return connector.list_messages(max_results=max_results, q=q, page_token=page_token, label_ids=label_ids)


@router.get("/gmail/messages/{message_id}")
def gmail_get_message(message_id: str, request: Request):
    connector = get_gmail_connector(_user_id(request))
    return connector.get_message(message_id)


@router.get("/gmail/messages/{message_id}/attachments/{attachment_id}")
def gmail_download_attachment(
    message_id: str,
    attachment_id: str,
    request: Request,
    filename: str = Query(default="attachment"),
):
    connector = get_gmail_connector(_user_id(request))
    content, attachment_filename = connector.download_attachment(message_id, attachment_id, filename)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_content_disposition_header(attachment_filename),
    )


class GmailBulkIngestRequest(BaseModel):
    # "selected": ingest exactly `message_ids`.
    # "count": ingest the `count` most recent messages matching `label` (Gmail
    #          lists newest-first, so this is naturally "latest N").
    # "all": ingest every message matching `label`, up to cfg.gmail_bulk_ingest_limit.
    mode: str
    label: str = "all"
    message_ids: list[str] | None = None
    count: int | None = None
    session_id: str | None = None


@router.post("/gmail/ingest/bulk")
async def gmail_ingest_bulk(request: Request, body: GmailBulkIngestRequest):
    """Ingests many Gmail messages in one call, streaming SSE progress
    events (reusing the same event shape the file-upload SSE stream emits -
    see app.routers.upload.upload_files - so the frontend's existing
    ProgressCard/handleProgressEvent can render it unchanged). Each message
    goes through pipeline_bridge.ingest_gmail_message exactly as the
    single-email ingest path does (body + every attachment's extracted
    text), so dedup/chunk/embed behavior is identical - this just loops."""
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"
    connector = get_gmail_connector(user_id)
    label_ids = _gmail_label_ids(body.label)

    if body.mode == "selected":
        if not body.message_ids:
            raise HTTPException(400, "message_ids is required for mode='selected'")
        message_ids = body.message_ids[:cfg.gmail_bulk_ingest_limit]
    elif body.mode == "count":
        if not body.count or body.count < 1:
            raise HTTPException(400, "count is required (>=1) for mode='count'")
        limit = min(body.count, cfg.gmail_bulk_ingest_limit)
        message_ids = connector.list_message_ids(label_ids=label_ids, limit=limit)
    elif body.mode == "all":
        message_ids = connector.list_message_ids(label_ids=label_ids, limit=cfg.gmail_bulk_ingest_limit)
    else:
        raise HTTPException(400, "mode must be one of: selected, count, all")

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        session_id = body.session_id
        total = len(message_ids)
        ingested = 0

        if total == 0:
            yield sse({"type": "complete", "session_id": session_id, "total_elapsed": 0.0,
                       "completion_message": "No matching emails found"})
            return

        if body.mode == "all" and total >= cfg.gmail_bulk_ingest_limit:
            yield sse({"type": "warning",
                       "message": f"Capped at {cfg.gmail_bulk_ingest_limit} most recent emails for this bulk ingest."})
            await asyncio.sleep(0)

        for i, message_id in enumerate(message_ids):
            stage = f"gmail_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Ingesting emails ({i + 1}/{total})",
                       "message": f"Fetching email {i + 1} of {total}..."})
            await asyncio.sleep(0)
            t1 = time.time()
            try:
                result = await pipeline_bridge.ingest_gmail_message(
                    message_id=message_id, user_id=user_id, client_token=client_token, session_id=session_id,
                )
                session_id = result["session_id"]
                ingested += 1
                yield sse({"type": "step", "status": "done", "stage": stage,
                           "message": result["filename"], "elapsed": round(time.time() - t1, 2)})
            except Exception as e:
                logger.warning(f"[CONNECTORS][GMAIL][BULK] message {message_id} failed: {e}")
                yield sse({"type": "step", "status": "error", "stage": stage,
                           "message": f"Email {i + 1} failed: {e}"})
            await asyncio.sleep(0)

        yield sse({
            "type": "complete", "session_id": session_id,
            "total_elapsed": round(time.time() - t0, 2),
            "completion_message": f"Ingested {ingested}/{total} emails - ready to chat!",
        })

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@oauth_callback_router.get("/gmail/callback")
def gmail_callback(code: str = Query(...), state: str = Query(default="")):
    # Redirects back into the app instead of leaving the browser on a bare
    # JSON page - the frontend reads ?connected=gmail on load to reopen the
    # connectors panel with a fresh, already-connected status, instead of the
    # user having to manually navigate back and reload it themselves.
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Gmail OAuth callback")
    connector = get_gmail_connector(user_id)
    connector.exchange_code_for_token(code)

    # Google Chat shares this exact OAuth grant (gmail.py's SCOPES already
    # request the Chat read scopes) - mirror the token into a second
    # "google_chat" row so connecting Gmail connects Chat too, with no
    # separate consent step or callback.
    gmail_token = token_store.get_token(user_id, "gmail")
    if gmail_token:
        token_store.save_token(
            user_id, "google_chat",
            access_token=gmail_token["access_token"],
            refresh_token=gmail_token["refresh_token"],
            expires_at=gmail_token["expires_at"],
        )

    return RedirectResponse(url="/?connected=gmail")


# ==========================================
# Google Chat Connector Endpoints (shares Gmail's OAuth grant - no auth/login
# or callback of its own; see gmail_callback above and get_authorization_url
# in gmail.py)
# ==========================================
GOOGLE_CHAT_SPACE_TYPES = ("all", "space", "group_chat", "direct_message")


@router.get("/google-chat/auth/login")
def google_chat_login(request: Request):
    # Returns the exact same authorization URL Gmail's login does - one
    # consent screen grants both, and gmail_callback mirrors the resulting
    # token into the "google_chat" row too.
    connector = get_gmail_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/google-chat/status")
def google_chat_status(request: Request):
    connector = get_google_chat_connector(_user_id(request))
    return connector.get_status()


@router.get("/google-chat/spaces")
def google_chat_list_spaces(
    request: Request,
    space_type: str = Query(default="all", description="One of: " + ", ".join(GOOGLE_CHAT_SPACE_TYPES)),
    page_size: int = Query(default=50, ge=1, le=100),
    page_token: str = Query(default=""),
):
    if space_type not in GOOGLE_CHAT_SPACE_TYPES:
        raise HTTPException(400, f"Unknown space_type '{space_type}'. Allowed: {', '.join(GOOGLE_CHAT_SPACE_TYPES)}")
    connector = get_google_chat_connector(_user_id(request))
    return connector.list_spaces(space_type=space_type, page_size=page_size, page_token=page_token)


@router.get("/google-chat/spaces/{space_id}/messages")
def google_chat_list_messages(space_id: str, request: Request, page_token: str = Query(default="")):
    connector = get_google_chat_connector(_user_id(request))
    return connector.list_messages(space_id, page_token=page_token)


class GoogleChatBulkIngestRequest(BaseModel):
    # "selected": ingest exactly `space_ids`.
    # "all": ingest every space matching `space_type`, up to cfg.gmail_bulk_ingest_limit.
    mode: str
    space_type: str = "all"
    space_ids: list[str] | None = None
    session_id: str | None = None


@router.post("/google-chat/ingest/bulk")
async def google_chat_ingest_bulk(request: Request, body: GoogleChatBulkIngestRequest):
    """Ingests many Google Chat spaces/chats in one call, streaming SSE
    progress events in the same shape the Gmail bulk endpoint and the
    file-upload SSE stream use (see app.routers.upload.upload_files), so the
    frontend's existing ProgressCard/handleProgressEvent renders it
    unchanged. Each space goes through pipeline_bridge.ingest_google_chat_space
    (every message + attachment + linked page in that space, combined into
    one document) exactly as a single-space ingest would - this just loops."""
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"
    connector = get_google_chat_connector(user_id)

    if body.space_type not in GOOGLE_CHAT_SPACE_TYPES:
        raise HTTPException(400, f"Unknown space_type '{body.space_type}'. Allowed: {', '.join(GOOGLE_CHAT_SPACE_TYPES)}")

    if body.mode == "selected":
        if not body.space_ids:
            raise HTTPException(400, "space_ids is required for mode='selected'")
        space_ids = body.space_ids[:cfg.gmail_bulk_ingest_limit]
    elif body.mode == "all":
        space_ids = connector.list_space_ids(space_type=body.space_type, limit=cfg.gmail_bulk_ingest_limit)
    else:
        raise HTTPException(400, "mode must be one of: selected, all")

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        session_id = body.session_id
        total = len(space_ids)
        ingested = 0

        if total == 0:
            yield sse({"type": "complete", "session_id": session_id, "total_elapsed": 0.0,
                       "completion_message": "No matching chats/spaces found"})
            return

        if body.mode == "all" and total >= cfg.gmail_bulk_ingest_limit:
            yield sse({"type": "warning",
                       "message": f"Capped at {cfg.gmail_bulk_ingest_limit} most recent chats/spaces for this bulk ingest."})
            await asyncio.sleep(0)

        for i, space_id in enumerate(space_ids):
            stage = f"gchat_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Ingesting chats/spaces ({i + 1}/{total})",
                       "message": f"Fetching chat {i + 1} of {total}..."})
            await asyncio.sleep(0)
            t1 = time.time()
            try:
                result = await pipeline_bridge.ingest_google_chat_space(
                    space_id=space_id, user_id=user_id, client_token=client_token, session_id=session_id,
                )
                session_id = result["session_id"]
                ingested += 1
                yield sse({"type": "step", "status": "done", "stage": stage,
                           "message": result["filename"], "elapsed": round(time.time() - t1, 2)})
            except Exception as e:
                logger.warning(f"[CONNECTORS][GOOGLE_CHAT][BULK] space {space_id} failed: {e}")
                yield sse({"type": "step", "status": "error", "stage": stage,
                           "message": f"Chat {i + 1} failed: {e}"})
            await asyncio.sleep(0)

        yield sse({
            "type": "complete", "session_id": session_id,
            "total_elapsed": round(time.time() - t0, 2),
            "completion_message": f"Ingested {ingested}/{total} chats/spaces - ready to chat!",
        })

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ==========================================
# Google Drive Connector Endpoints
# ==========================================
@router.get("/google-drive/auth/login")
def google_drive_login(request: Request):
    connector = get_google_drive_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/google-drive/status")
def google_drive_status(request: Request):
    connector = get_google_drive_connector(_user_id(request))
    return connector.get_status()


@router.get("/google-drive/files")
def google_drive_list_files(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=100),
    q: str = Query(default="", description="Search query e.g. name contains 'project'"),
    page_token: str = Query(default="", description="next_page_token from a previous page to continue listing"),
):
    connector = get_google_drive_connector(_user_id(request))
    return connector.list_files(max_results=max_results, q=q, page_token=page_token)


@router.get("/google-drive/files/{file_id}")
def google_drive_get_file(file_id: str, request: Request):
    connector = get_google_drive_connector(_user_id(request))
    return connector.get_file_metadata(file_id)


@router.get("/google-drive/files/{file_id}/download")
def google_drive_download(file_id: str, request: Request):
    connector = get_google_drive_connector(_user_id(request))
    content, filename = connector.download_file(file_id)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_content_disposition_header(filename),
    )


@oauth_callback_router.get("/google-drive/callback")
def google_drive_callback(code: str = Query(...), state: str = Query(default="")):
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Google Drive OAuth callback")
    connector = get_google_drive_connector(user_id)
    connector.exchange_code_for_token(code)
    return RedirectResponse(url="/?connected=google_drive")


# ==========================================
# Outlook Mail Connector Endpoints
# ==========================================
OUTLOOK_FOLDERS = ("inbox", "junk", "sent", "drafts", "deleted", "all")


@router.get("/outlook/auth/login")
def outlook_login(request: Request):
    connector = get_outlook_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/outlook/status")
def outlook_status(request: Request):
    connector = get_outlook_connector(_user_id(request))
    return connector.get_status()


@router.get("/outlook/messages")
def outlook_list_messages(
    request: Request,
    folder: str = Query(default="inbox", description="One of: " + ", ".join(OUTLOOK_FOLDERS)),
    max_results: int = Query(default=25, ge=1, le=50),
    next_link: str = Query(default="", description="@odata.nextLink from a previous page to continue listing"),
):
    if folder not in OUTLOOK_FOLDERS:
        raise HTTPException(400, f"Unknown folder '{folder}'. Allowed: {', '.join(OUTLOOK_FOLDERS)}")
    connector = get_outlook_connector(_user_id(request))
    return connector.list_messages(folder=folder, max_results=max_results, next_link=next_link)


@router.get("/outlook/messages/{message_id}")
def outlook_get_message(message_id: str, request: Request):
    connector = get_outlook_connector(_user_id(request))
    return connector.get_message(message_id)


@router.get("/outlook/messages/{message_id}/attachments/{attachment_id}")
def outlook_download_attachment(
    message_id: str,
    attachment_id: str,
    request: Request,
    filename: str = Query(default="attachment"),
):
    connector = get_outlook_connector(_user_id(request))
    content, attachment_filename = connector.download_attachment(message_id, attachment_id, filename)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers=_content_disposition_header(attachment_filename),
    )


class OutlookBulkIngestRequest(BaseModel):
    # "selected": ingest exactly `message_ids`.
    # "count": ingest the `count` most recent messages matching `folder`.
    # "all": ingest every message matching `folder`, up to cfg.gmail_bulk_ingest_limit.
    mode: str
    folder: str = "inbox"
    message_ids: list[str] | None = None
    count: int | None = None
    session_id: str | None = None


@router.post("/outlook/ingest/bulk")
async def outlook_ingest_bulk(request: Request, body: OutlookBulkIngestRequest):
    """Mirrors /connectors/gmail/ingest/bulk exactly (same modes, same SSE
    event shape) - see that endpoint's docstring. Each message goes through
    pipeline_bridge.ingest_outlook_message (body + every attachment's
    extracted text + any linked page), looped the same way."""
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"
    connector = get_outlook_connector(user_id)

    if body.folder not in OUTLOOK_FOLDERS:
        raise HTTPException(400, f"Unknown folder '{body.folder}'. Allowed: {', '.join(OUTLOOK_FOLDERS)}")

    if body.mode == "selected":
        if not body.message_ids:
            raise HTTPException(400, "message_ids is required for mode='selected'")
        message_ids = body.message_ids[:cfg.gmail_bulk_ingest_limit]
    elif body.mode == "count":
        if not body.count or body.count < 1:
            raise HTTPException(400, "count is required (>=1) for mode='count'")
        limit = min(body.count, cfg.gmail_bulk_ingest_limit)
        message_ids = connector.list_message_ids(folder=body.folder, limit=limit)
    elif body.mode == "all":
        message_ids = connector.list_message_ids(folder=body.folder, limit=cfg.gmail_bulk_ingest_limit)
    else:
        raise HTTPException(400, "mode must be one of: selected, count, all")

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        session_id = body.session_id
        total = len(message_ids)
        ingested = 0

        if total == 0:
            yield sse({"type": "complete", "session_id": session_id, "total_elapsed": 0.0,
                       "completion_message": "No matching emails found"})
            return

        if body.mode == "all" and total >= cfg.gmail_bulk_ingest_limit:
            yield sse({"type": "warning",
                       "message": f"Capped at {cfg.gmail_bulk_ingest_limit} most recent emails for this bulk ingest."})
            await asyncio.sleep(0)

        for i, message_id in enumerate(message_ids):
            stage = f"outlook_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Ingesting emails ({i + 1}/{total})",
                       "message": f"Fetching email {i + 1} of {total}..."})
            await asyncio.sleep(0)
            t1 = time.time()
            try:
                result = await pipeline_bridge.ingest_outlook_message(
                    message_id=message_id, user_id=user_id, client_token=client_token, session_id=session_id,
                )
                session_id = result["session_id"]
                ingested += 1
                yield sse({"type": "step", "status": "done", "stage": stage,
                           "message": result["filename"], "elapsed": round(time.time() - t1, 2)})
            except Exception as e:
                logger.warning(f"[CONNECTORS][OUTLOOK][BULK] message {message_id} failed: {e}")
                yield sse({"type": "step", "status": "error", "stage": stage,
                           "message": f"Email {i + 1} failed: {e}"})
            await asyncio.sleep(0)

        yield sse({
            "type": "complete", "session_id": session_id,
            "total_elapsed": round(time.time() - t0, 2),
            "completion_message": f"Ingested {ingested}/{total} emails - ready to chat!",
        })

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@oauth_callback_router.get("/outlook/callback")
def outlook_callback(code: str = Query(...), state: str = Query(default="")):
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Outlook OAuth callback")
    connector = get_outlook_connector(user_id)
    connector.exchange_code_for_token(code)

    # Outlook Chat shares this exact OAuth grant (outlook.py's SCOPES already
    # request Chat.Read) - mirror the token into a second "outlook_chat" row
    # so connecting Outlook connects Outlook Chat too, with no separate
    # consent step or callback.
    outlook_token = token_store.get_token(user_id, "outlook")
    if outlook_token:
        token_store.save_token(
            user_id, "outlook_chat",
            access_token=outlook_token["access_token"],
            refresh_token=outlook_token["refresh_token"],
            expires_at=outlook_token["expires_at"],
        )

    return RedirectResponse(url="/?connected=outlook")


# ==========================================
# Outlook Chat Connector Endpoints (shares Outlook's OAuth grant - no
# auth/login or callback of its own; see outlook_callback above and
# get_authorization_url in outlook.py)
# ==========================================
OUTLOOK_CHAT_TYPES = ("all", "one_on_one", "group", "meeting")


@router.get("/outlook-chat/auth/login")
def outlook_chat_login(request: Request):
    connector = get_outlook_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/outlook-chat/status")
def outlook_chat_status(request: Request):
    connector = get_outlook_chat_connector(_user_id(request))
    return connector.get_status()


@router.get("/outlook-chat/chats")
def outlook_chat_list_chats(
    request: Request,
    chat_type: str = Query(default="all", description="One of: " + ", ".join(OUTLOOK_CHAT_TYPES)),
    page_size: int = Query(default=50, ge=1, le=50),
    next_link: str = Query(default=""),
):
    if chat_type not in OUTLOOK_CHAT_TYPES:
        raise HTTPException(400, f"Unknown chat_type '{chat_type}'. Allowed: {', '.join(OUTLOOK_CHAT_TYPES)}")
    connector = get_outlook_chat_connector(_user_id(request))
    return connector.list_chats(chat_type=chat_type, page_size=page_size, next_link=next_link)


class OutlookChatBulkIngestRequest(BaseModel):
    mode: str  # "selected" | "all"
    chat_type: str = "all"
    chat_ids: list[str] | None = None
    session_id: str | None = None


@router.post("/outlook-chat/ingest/bulk")
async def outlook_chat_ingest_bulk(request: Request, body: OutlookChatBulkIngestRequest):
    """Mirrors /connectors/google-chat/ingest/bulk exactly (same modes, same
    SSE event shape) - see that endpoint's docstring. Each chat goes through
    pipeline_bridge.ingest_outlook_chat, looped the same way."""
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"
    connector = get_outlook_chat_connector(user_id)

    if body.chat_type not in OUTLOOK_CHAT_TYPES:
        raise HTTPException(400, f"Unknown chat_type '{body.chat_type}'. Allowed: {', '.join(OUTLOOK_CHAT_TYPES)}")

    if body.mode == "selected":
        if not body.chat_ids:
            raise HTTPException(400, "chat_ids is required for mode='selected'")
        chat_ids = body.chat_ids[:cfg.gmail_bulk_ingest_limit]
    elif body.mode == "all":
        chat_ids = connector.list_chat_ids(chat_type=body.chat_type, limit=cfg.gmail_bulk_ingest_limit)
    else:
        raise HTTPException(400, "mode must be one of: selected, all")

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        session_id = body.session_id
        total = len(chat_ids)
        ingested = 0

        if total == 0:
            yield sse({"type": "complete", "session_id": session_id, "total_elapsed": 0.0,
                       "completion_message": "No matching chats found"})
            return

        if body.mode == "all" and total >= cfg.gmail_bulk_ingest_limit:
            yield sse({"type": "warning",
                       "message": f"Capped at {cfg.gmail_bulk_ingest_limit} most recent chats for this bulk ingest."})
            await asyncio.sleep(0)

        for i, chat_id in enumerate(chat_ids):
            stage = f"outlook_chat_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Ingesting chats ({i + 1}/{total})",
                       "message": f"Fetching chat {i + 1} of {total}..."})
            await asyncio.sleep(0)
            t1 = time.time()
            try:
                result = await pipeline_bridge.ingest_outlook_chat(
                    chat_id=chat_id, user_id=user_id, client_token=client_token, session_id=session_id,
                )
                session_id = result["session_id"]
                ingested += 1
                yield sse({"type": "step", "status": "done", "stage": stage,
                           "message": result["filename"], "elapsed": round(time.time() - t1, 2)})
            except Exception as e:
                logger.warning(f"[CONNECTORS][OUTLOOK_CHAT][BULK] chat {chat_id} failed: {e}")
                yield sse({"type": "step", "status": "error", "stage": stage,
                           "message": f"Chat {i + 1} failed: {e}"})
            await asyncio.sleep(0)

        yield sse({
            "type": "complete", "session_id": session_id,
            "total_elapsed": round(time.time() - t0, 2),
            "completion_message": f"Ingested {ingested}/{total} chats - ready to chat!",
        })

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ==========================================
# Microsoft SharePoint Connector Endpoints
# ==========================================
@router.get("/sharepoint/sites")
def sharepoint_sites(search: str | None = None):
    try:
        return {"value": SharePointConnector().list_sites(search)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sharepoint/sites/{site_id}/drives")
def sharepoint_drives(site_id: str):
    try:
        return {"value": SharePointConnector().list_drives(site_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sharepoint/drives/{drive_id}/items")
def sharepoint_items(drive_id: str, folder_id: str = Query(default="root")):
    try:
        return {"value": SharePointConnector().list_items(drive_id, folder_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sharepoint/drives/{drive_id}/items/{item_id}/download")
def sharepoint_download(drive_id: str, item_id: str):
    try:
        content, filename = SharePointConnector().download_item(drive_id, item_id)
        return StreamingResponse(
            BytesIO(content),
            media_type="application/octet-stream",
            headers=_content_disposition_header(filename),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ==========================================
# Microsoft Teams Connector Endpoints
# ==========================================
@router.get("/teams/list")
def teams_list():
    try:
        return {"value": TeamsConnector().list_teams()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/teams/{team_id}/channels")
def teams_channels(team_id: str):
    try:
        return {"value": TeamsConnector().list_channels(team_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/teams/{team_id}/channels/{channel_id}/messages")
def teams_messages(
    team_id: str,
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=100),
):
    try:
        return {"value": TeamsConnector().list_channel_messages(team_id, channel_id, limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MicrosoftGraphError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ==========================================
# Azure DevOps Repos Endpoints
# ==========================================
@router.get("/azure-devops/projects")
def azure_devops_projects(request: Request):
    try:
        connector = get_azure_devops_connector(_user_id(request))
        return {"value": connector.list_projects()}
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/azure-devops/projects/{project}/repos")
def azure_devops_repositories(project: str, request: Request):
    try:
        connector = get_azure_devops_connector(_user_id(request))
        return {"value": connector.list_repositories(project)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/azure-devops/projects/{project}/repos/{repo_id}/items")
def azure_devops_items(
    project: str,
    repo_id: str,
    request: Request,
    path: str = Query(default="/"),
    recursion_level: str = Query(default="Full"),
):
    try:
        connector = get_azure_devops_connector(_user_id(request))
        items = connector.list_items(
            project=project, repository_id=repo_id, path=path, recursion_level=recursion_level,
        )
        return {"value": items}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/azure-devops/projects/{project}/repos/{repo_id}/file")
def azure_devops_file(
    project: str,
    repo_id: str,
    request: Request,
    path: str = Query(..., description="Repository file path"),
):
    try:
        connector = get_azure_devops_connector(_user_id(request))
        content = connector.get_file(project=project, repository_id=repo_id, path=path)
        filename = os.path.basename(path.rstrip("/")) or "azure_devops_file"
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers=_content_disposition_header(filename),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ==========================================
# Ingest bridge - connector-sourced file -> existing RAG pipeline
# ==========================================

async def _fetch_connector_file(provider: str, body: dict, user_id: str) -> tuple[bytes, str]:
    """Downloads (or synthesizes, for record-oriented connectors) the bytes +
    filename for one connector-sourced "document", given a small per-provider
    identifier dict. Record-oriented connectors (Teams) don't expose raw
    files in the PoC, so their record data is rendered
    to plain text and ingested as a .txt document instead of a raw download.

    This function only talks to connector clients (app.connectors.clients.*)
    - it has no knowledge of the RAG pipeline. The RAG-specific orchestration
    lives in app.connectors.pipeline_bridge.ingest_bytes, which the /ingest
    endpoint below calls with the (content, filename) this function returns."""

    if provider == "github":
        owner, repo, path = body.get("owner"), body.get("repo"), body.get("path")
        if not (owner and repo and path):
            raise HTTPException(400, "owner, repo, and path are required for github")
        connector = get_github_connector(user_id)
        return connector.download_file(owner, repo, path, body.get("ref"))

    # gmail is handled separately in ingest_connector_file() below, not here -
    # it ingests a whole email (body + every attachment's extracted text)
    # combined into one document, which needs parse_file() on each
    # attachment and so must go through pipeline_bridge.py (the one file in
    # this package allowed to know about RAG internals) rather than this
    # RAG-agnostic fetch function.

    if provider == "google_drive":
        file_id = body.get("file_id")
        if not file_id:
            raise HTTPException(400, "file_id is required for google_drive")
        connector = get_google_drive_connector(user_id)
        return connector.download_file(file_id)

    if provider == "sharepoint":
        drive_id, item_id = body.get("drive_id"), body.get("item_id")
        if not (drive_id and item_id):
            raise HTTPException(400, "drive_id and item_id are required for sharepoint")
        try:
            return SharePointConnector().download_item(drive_id, item_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except MicrosoftGraphError as exc:
            raise HTTPException(502, str(exc)) from exc

    if provider == "azure_devops":
        project, repo_id, path = body.get("project"), body.get("repo_id"), body.get("path")
        if not (project and repo_id and path):
            raise HTTPException(400, "project, repo_id, and path are required for azure_devops")
        try:
            connector = get_azure_devops_connector(user_id)
            content = connector.get_file(project=project, repository_id=repo_id, path=path)
            return content, os.path.basename(path.rstrip("/")) or "azure_devops_file"
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except AzureDevOpsError as exc:
            raise HTTPException(502, str(exc)) from exc

    if provider == "teams":
        team_id, channel_id = body.get("team_id"), body.get("channel_id")
        if not (team_id and channel_id):
            raise HTTPException(400, "team_id and channel_id are required for teams")
        try:
            messages = TeamsConnector().list_channel_messages(team_id, channel_id, body.get("limit", 50))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except MicrosoftGraphError as exc:
            raise HTTPException(502, str(exc)) from exc
        lines = []
        for m in messages:
            body_content = (m.get("body") or {}).get("content", "")
            sender = ((m.get("from") or {}).get("user") or {}).get("displayName", "unknown")
            lines.append(f"[{m.get('createdDateTime')}] {sender}: {body_content}")
        text = "\n".join(lines)
        return text.encode("utf-8"), f"teams_{team_id}_{channel_id}.txt"

    raise HTTPException(400, f"Unknown or unsupported provider for ingest: {provider}")


@router.post("/{provider}/ingest")
async def ingest_connector_file(provider: str, request: Request, body: dict = Body(...)):
    """Bridges a connector-sourced file into the existing RAG ingestion
    pipeline (the same parse/chunk/embed/dedup path /upload/files uses), so
    the result is a normal session_id the existing /chat endpoint can use.

    Body is a small per-provider identifier dict, e.g.:
      github:        {"owner": "...", "repo": "...", "path": "...", "ref": "..."}
      gmail:         {"message_id": "..."} - ingests the whole email (body + every attachment's extracted text)
      google_drive:  {"file_id": "..."}
      sharepoint:    {"drive_id": "...", "item_id": "..."}
      azure_devops:  {"project": "...", "repo_id": "...", "path": "..."}
      teams:         {"team_id": "...", "channel_id": "...", "limit": 50}

    An optional "session_id" key in the body (any provider) attaches this
    document to that already-active chat session instead of starting a new,
    isolated one - the frontend sends its current session_id once a chat is
    underway, so adding a second/third connector file doesn't orphan the
    ones added before it.
    """
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"
    session_id = body.get("session_id") or None

    if provider == "gmail":
        message_id = body.get("message_id")
        if not message_id:
            raise HTTPException(400, "message_id is required for gmail")
        return await pipeline_bridge.ingest_gmail_message(
            message_id=message_id, user_id=user_id, client_token=client_token, session_id=session_id,
        )

    content, filename = await _fetch_connector_file(provider, body, user_id)

    return await pipeline_bridge.ingest_bytes(
        provider=provider, content=content, filename=filename,
        user_id=user_id, client_token=client_token,
        session_id=session_id,
    )
