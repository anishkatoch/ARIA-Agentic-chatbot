"""Source-system connector endpoints - ported from CONNECTOR_POC/app/main.py.

Three routers are exported:

- `router` (prefix="/connectors") - all data/listing endpoints, login-initiation
  endpoints for the 3 OAuth-based connectors, and the ingest bridge endpoint.
  This whole prefix is gated behind the existing auth middleware (see
  app/main.py's _PROTECTED_PREFIXES), so every route here requires a logged-in
  user - same as /chat/ and /upload/. On top of that, every route on this
  router also requires a valid connectors-unlock token (see access_gate.py
  and the `require_connectors_unlock` dependency below) - a second, separate
  secret the deployment owner sets in CONNECTORS_ACCESS_KEY, since a connected
  Gmail/GitHub/Jira/Salesforce session is real access to someone's real
  third-party accounts, not just app data.

- `unlock_router` (prefix="/connectors") - just the one POST /connectors/unlock
  endpoint used to exchange the raw access key for a short-lived unlock token.
  Deliberately NOT behind `require_connectors_unlock` itself (a lock can't
  require its own key to ask about opening it).

- `oauth_callback_router` (no prefix) - the 3 OAuth callback endpoints, which
  MUST live at a literal fixed path matching GOOGLE_REDIRECT_URI /
  GOOGLE_DRIVE_REDIRECT_URI / SALESFORCE_REDIRECT_URI (the browser lands on
  these directly from Google/Salesforce with no Authorization header, so they
  can't be gated behind either Bearer auth or the unlock token - they must
  stay in app/main.py's _PUBLIC_PREFIXES). Reaching this point already
  required completing the gated /auth/login step first, so this stays safe.

This module (and everything else in app/connectors/ except pipeline_bridge.py)
has no knowledge of this host app's RAG internals - the one place that
knowledge lives is app.connectors.pipeline_bridge.ingest_bytes(), which this
router calls after fetching connector-sourced bytes. See
app/connectors/README.md for the full picture of how this package is bounded.
"""
import logging
import os
import uuid
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.connectors import access_gate, token_store
from app.connectors.clients.file_share import get_connector as get_file_share_connector
from app.connectors.clients.lms import get_lms_connector
from app.connectors.clients.github import get_github_connector
from app.connectors.clients.gmail import get_gmail_connector
from app.connectors.clients.google_drive import get_google_drive_connector
from app.connectors.clients.salesforce import get_salesforce_connector
from app.connectors.clients.servicenow import get_servicenow_connector
from app.connectors.clients.jira import get_jira_connector
from app.connectors.clients.microsoft_graph import MicrosoftGraphError
from app.connectors.clients.sharepoint import SharePointConnector
from app.connectors.clients.teams import TeamsConnector
from app.connectors.clients.azure_devops import AzureDevOpsConnector, AzureDevOpsError
from app.connectors import pipeline_bridge

logger = logging.getLogger(__name__)


def _user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return user["id"]


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
    {"id": "file_share",   "path": "file-share",   "name": "File Share",        "type": "files",              "auth": "config"},
    {"id": "lms",          "path": "lms",           "name": "LMS",               "type": "learning",           "auth": "none"},
    {"id": "github",       "path": "github",        "name": "GitHub",            "type": "code",               "auth": "config"},
    {"id": "gmail",        "path": "gmail",         "name": "Gmail",             "type": "email",              "auth": "oauth"},
    {"id": "google_drive", "path": "google-drive",  "name": "Google Drive",      "type": "documents",          "auth": "oauth"},
    {"id": "sharepoint",   "path": "sharepoint",    "name": "SharePoint",        "type": "documents",          "auth": "config"},
    {"id": "teams",        "path": "teams",         "name": "Microsoft Teams",   "type": "chat",               "auth": "config"},
    {"id": "azure_devops", "path": "azure-devops",  "name": "Azure DevOps Repos","type": "code",               "auth": "config"},
    {"id": "jira",         "path": "jira",          "name": "Jira",              "type": "project_management", "auth": "config"},
    {"id": "servicenow",   "path": "servicenow",    "name": "ServiceNow",        "type": "itsm",               "auth": "config"},
    {"id": "salesforce",   "path": "salesforce",    "name": "Salesforce",        "type": "crm",                "auth": "oauth"},
]


def _is_placeholder(*values: str) -> bool:
    """True if any value still looks like the literal placeholder text written
    into .env/.env.example/env.example (e.g. "your-github-personal-access-token"),
    so an unfilled template doesn't get reported as a real, working credential."""
    return any("your-" in (v or "").lower() for v in values)


def _is_configured(provider_id: str) -> bool:
    if provider_id == "file_share":
        root = os.getenv("FILE_SHARE_ROOT", "")
        return bool(root) and not _is_placeholder(root) and os.path.isdir(root)
    if provider_id == "lms":
        return True  # mock connector, no config needed
    if provider_id == "github":
        token = os.getenv("GITHUB_TOKEN", "")
        return bool(token) and not _is_placeholder(token)
    if provider_id in ("gmail", "google_drive"):
        vals = (os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", ""))
        return all(vals) and not _is_placeholder(*vals)
    if provider_id in ("sharepoint", "teams"):
        vals = (
            os.getenv("MICROSOFT_TENANT_ID", ""),
            os.getenv("MICROSOFT_CLIENT_ID", ""),
            os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        )
        return all(vals) and not _is_placeholder(*vals)
    if provider_id == "azure_devops":
        vals = (os.getenv("AZURE_DEVOPS_ORG_URL", ""), os.getenv("AZURE_DEVOPS_PAT", ""))
        return all(vals) and not _is_placeholder(*vals)
    if provider_id == "jira":
        vals = (os.getenv("JIRA_BASE_URL", ""), os.getenv("JIRA_EMAIL", ""), os.getenv("JIRA_API_TOKEN", ""))
        return all(vals) and not _is_placeholder(*vals)
    if provider_id == "servicenow":
        vals = (
            os.getenv("SERVICENOW_INSTANCE_URL", ""),
            os.getenv("SERVICENOW_USERNAME", ""),
            os.getenv("SERVICENOW_PASSWORD", ""),
        )
        return all(vals) and not _is_placeholder(*vals)
    if provider_id == "salesforce":
        vals = (os.getenv("SALESFORCE_CLIENT_ID", ""), os.getenv("SALESFORCE_CLIENT_SECRET", ""))
        return all(vals) and not _is_placeholder(*vals)
    return False


@router.get("/status")
def connectors_status(request: Request):
    """Real per-user connector status - unlike CONNECTOR_POC's old endpoint,
    nothing here is hardcoded demo data. 'configured' reflects actual env vars;
    'connected' additionally checks the per-user token for OAuth connectors."""
    user_id = _user_id(request)
    sources = []
    for meta in CONNECTOR_CATALOG:
        configured = _is_configured(meta["id"])
        if meta["auth"] == "oauth":
            connected = configured and token_store.get_token(user_id, meta["id"]) is not None
        else:
            connected = configured
        sources.append({
            **meta,
            "configured": configured,
            "connected": connected,
            "login_url": f"/connectors/{meta['path']}/auth/login" if meta["auth"] == "oauth" else None,
        })
    return {
        "connected_count": sum(1 for s in sources if s["connected"]),
        "total_count": len(sources),
        "sources": sources,
    }


# ==========================================
# File Share Connector Endpoints
# ==========================================
@router.get("/file-share/files")
def list_files(
    path: str = Query(default="", description="Relative path inside configured root"),
):
    connector = get_file_share_connector()
    return connector.list_directory(path)


@router.get("/file-share/download")
def download_file(
    path: str = Query(..., description="Relative file path inside configured root")
):
    connector = get_file_share_connector()
    file_path = connector.get_file(path)
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# ==========================================
# LMS Connector Endpoints (Mock / Modular)
# ==========================================
@router.get("/lms/courses")
def lms_list_courses():
    connector = get_lms_connector()
    return connector.list_courses()


@router.get("/lms/courses/{course_id}/modules")
def lms_list_modules(course_id: str):
    connector = get_lms_connector()
    return connector.list_modules(course_id)


@router.get("/lms/courses/{course_id}/files")
def lms_list_course_files(course_id: str):
    connector = get_lms_connector()
    return connector.list_course_files(course_id)


@router.get("/lms/files/{file_id}/download")
def lms_download_file(file_id: str):
    connector = get_lms_connector()
    file_path = connector.get_file(file_id)
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


# ==========================================
# GitHub Connector Endpoints
# ==========================================
@router.get("/github/repos")
def list_github_repositories(
    visibility: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
):
    connector = get_github_connector()
    return connector.list_repositories(visibility=visibility, page=page, per_page=per_page)


@router.get("/github/repos/{owner}/{repo}/contents")
def list_github_contents(
    owner: str,
    repo: str,
    path: str = Query(default=""),
    ref: str | None = Query(default=None),
):
    connector = get_github_connector()
    return connector.list_contents(owner, repo, path, ref)


@router.get("/github/repos/{owner}/{repo}/file")
def download_github_file(
    owner: str,
    repo: str,
    path: str = Query(...),
    ref: str | None = Query(default=None),
):
    connector = get_github_connector()
    content, filename = connector.download_file(owner, repo, path, ref)
    safe_filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


# ==========================================
# Gmail Connector Endpoints (Google OAuth2 + Gmail API)
# ==========================================
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


@router.get("/gmail/messages")
def gmail_list_messages(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=50),
    q: str = Query(default="", description="Search query e.g. from:someone or has:attachment"),
    page_token: str = Query(default="", description="next_page_token from a previous page to continue listing"),
):
    connector = get_gmail_connector(_user_id(request))
    return connector.list_messages(max_results=max_results, q=q, page_token=page_token)


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
    content, safe_filename = connector.download_attachment(message_id, attachment_id, filename)
    clean_filename = safe_filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{clean_filename}"'},
    )


@oauth_callback_router.get("/gmail/callback")
def gmail_callback(code: str = Query(...), state: str = Query(default="")):
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Gmail OAuth callback")
    connector = get_gmail_connector(user_id)
    connector.exchange_code_for_token(code)
    return {
        "status": "authenticated",
        "message": "Gmail authentication successful! You can now access /connectors/gmail/messages and /connectors/gmail/status",
    }


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
    clean_filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{clean_filename}"'},
    )


@oauth_callback_router.get("/google-drive/callback")
def google_drive_callback(code: str = Query(...), state: str = Query(default="")):
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Google Drive OAuth callback")
    connector = get_google_drive_connector(user_id)
    connector.exchange_code_for_token(code)
    return {
        "status": "authenticated",
        "message": "Google Drive authentication successful! You can now access /connectors/google-drive/files and /connectors/google-drive/status",
    }


# ==========================================
# ServiceNow Connector Endpoints
# ==========================================
@router.get("/servicenow/status")
def servicenow_status():
    connector = get_servicenow_connector()
    return connector.get_status()


@router.get("/servicenow/incidents")
def servicenow_list_incidents(
    max_results: int = Query(default=10, ge=1, le=100),
    query: str = Query(default="", description="ServiceNow query string or filter"),
):
    connector = get_servicenow_connector()
    return connector.list_incidents(max_results=max_results, query=query)


@router.get("/servicenow/incidents/{sys_id}")
def servicenow_get_incident(sys_id: str):
    connector = get_servicenow_connector()
    return connector.get_incident(sys_id)


@router.get("/servicenow/requests")
def servicenow_list_requests(
    max_results: int = Query(default=10, ge=1, le=100),
    query: str = Query(default="", description="ServiceNow query string or filter"),
):
    connector = get_servicenow_connector()
    return connector.list_requests(max_results=max_results, query=query)


@router.get("/servicenow/knowledge")
def servicenow_search_knowledge(
    query: str = Query(default="", description="Search query for knowledge articles"),
    max_results: int = Query(default=10, ge=1, le=100),
):
    connector = get_servicenow_connector()
    return connector.search_knowledge(query=query, max_results=max_results)


# ==========================================
# Jira Connector Endpoints
# ==========================================
@router.get("/jira/status")
def jira_status():
    connector = get_jira_connector()
    return connector.get_status()


@router.get("/jira/issues")
def jira_search_issues(
    jql: str = Query(default="", description="JQL query to search issues"),
    max_results: int = Query(default=10, ge=1, le=50),
):
    connector = get_jira_connector()
    return connector.search_issues(jql=jql, max_results=max_results)


@router.get("/jira/issues/{issue_key}")
def jira_get_issue(issue_key: str):
    connector = get_jira_connector()
    return connector.get_issue(issue_key)


@router.get("/jira/projects")
def jira_list_projects():
    connector = get_jira_connector()
    return connector.list_projects()


# ==========================================
# Salesforce Connector Endpoints
# ==========================================
@router.get("/salesforce/auth/login")
def salesforce_login(request: Request):
    connector = get_salesforce_connector(_user_id(request))
    auth_url = connector.get_authorization_url()
    return {"auth_url": auth_url}


@router.get("/salesforce/status")
def salesforce_status(request: Request):
    try:
        connector = get_salesforce_connector(_user_id(request))
        return connector.get_status()
    except HTTPException as exc:
        return {"authenticated": False, "detail": exc.detail}


@router.get("/salesforce/accounts")
def salesforce_list_accounts(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=100),
    search: str = Query(default=""),
):
    connector = get_salesforce_connector(_user_id(request))
    return connector.list_accounts(max_results=max_results, search_term=search)


@router.get("/salesforce/leads")
def salesforce_list_leads(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=100),
    search: str = Query(default=""),
):
    connector = get_salesforce_connector(_user_id(request))
    return connector.list_leads(max_results=max_results, search_term=search)


@router.get("/salesforce/opportunities")
def salesforce_list_opportunities(
    request: Request,
    max_results: int = Query(default=10, ge=1, le=100),
    search: str = Query(default=""),
):
    connector = get_salesforce_connector(_user_id(request))
    return connector.list_opportunities(max_results=max_results, search_term=search)


@router.get("/salesforce/records/{sobject}/{record_id}")
def salesforce_get_record(sobject: str, record_id: str, request: Request):
    connector = get_salesforce_connector(_user_id(request))
    return connector.get_record(sobject_type=sobject, record_id=record_id)


@oauth_callback_router.get("/salesforce/callback")
def salesforce_callback(code: str = Query(...), state: str = Query(default="")):
    user_id = state
    if not user_id:
        raise HTTPException(400, "Missing state (user id) on Salesforce OAuth callback")
    connector = get_salesforce_connector(user_id)
    connector.exchange_code_for_token(code)
    return {
        "status": "authenticated",
        "message": "Salesforce authentication successful! You can now access Salesforce endpoints and /connectors/salesforce/status",
    }


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
        safe_filename = filename.replace("\r", "").replace("\n", "")
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_filename, safe='')}"
        }
        return StreamingResponse(
            BytesIO(content),
            media_type="application/octet-stream",
            headers=headers,
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
def azure_devops_projects():
    try:
        connector = AzureDevOpsConnector()
        return {"value": connector.list_projects()}
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/azure-devops/projects/{project}/repos")
def azure_devops_repositories(project: str):
    try:
        connector = AzureDevOpsConnector()
        return {"value": connector.list_repositories(project)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AzureDevOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/azure-devops/projects/{project}/repos/{repo_id}/items")
def azure_devops_items(
    project: str,
    repo_id: str,
    path: str = Query(default="/"),
    recursion_level: str = Query(default="Full"),
):
    try:
        connector = AzureDevOpsConnector()
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
    path: str = Query(..., description="Repository file path"),
):
    try:
        connector = AzureDevOpsConnector()
        content = connector.get_file(project=project, repository_id=repo_id, path=path)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment"},
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
    identifier dict. Record-oriented connectors (Jira, ServiceNow, Salesforce,
    Teams) don't expose raw files in the PoC, so their record data is rendered
    to plain text and ingested as a .txt document instead of a raw download.

    This function only talks to connector clients (app.connectors.clients.*)
    - it has no knowledge of the RAG pipeline. The RAG-specific orchestration
    lives in app.connectors.pipeline_bridge.ingest_bytes, which the /ingest
    endpoint below calls with the (content, filename) this function returns."""

    if provider == "file_share":
        path = body.get("path")
        if not path:
            raise HTTPException(400, "path is required for file_share")
        connector = get_file_share_connector()
        file_path = connector.get_file(path)
        return file_path.read_bytes(), file_path.name

    if provider == "lms":
        file_id = body.get("file_id")
        if not file_id:
            raise HTTPException(400, "file_id is required for lms")
        connector = get_lms_connector()
        file_path = connector.get_file(file_id)
        return file_path.read_bytes(), file_path.name

    if provider == "github":
        owner, repo, path = body.get("owner"), body.get("repo"), body.get("path")
        if not (owner and repo and path):
            raise HTTPException(400, "owner, repo, and path are required for github")
        connector = get_github_connector()
        return connector.download_file(owner, repo, path, body.get("ref"))

    if provider == "gmail":
        message_id, attachment_id = body.get("message_id"), body.get("attachment_id")
        if not (message_id and attachment_id):
            raise HTTPException(400, "message_id and attachment_id are required for gmail")
        connector = get_gmail_connector(user_id)
        return connector.download_attachment(message_id, attachment_id, body.get("filename", "attachment"))

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
            connector = AzureDevOpsConnector()
            content = connector.get_file(project=project, repository_id=repo_id, path=path)
            return content, os.path.basename(path.rstrip("/")) or "azure_devops_file"
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except AzureDevOpsError as exc:
            raise HTTPException(502, str(exc)) from exc

    if provider == "jira":
        issue_key = body.get("issue_key")
        if not issue_key:
            raise HTTPException(400, "issue_key is required for jira")
        connector = get_jira_connector()
        issue = connector.get_issue(issue_key)
        text = (
            f"Jira Issue {issue.get('key')}: {issue.get('summary')}\n"
            f"Status: {issue.get('status')} | Priority: {issue.get('priority')} | Type: {issue.get('type')}\n"
            f"Assignee: {issue.get('assignee')} | Reporter: {issue.get('reporter')}\n\n"
            f"Description:\n{issue.get('description', '')}\n\n"
            + "\n".join(f"Comment by {c.get('author')} ({c.get('created')}):\n{c.get('body')}\n"
                        for c in issue.get("comments", []))
        )
        return text.encode("utf-8"), f"jira_{issue_key}.txt"

    if provider == "servicenow":
        sys_id = body.get("sys_id")
        if not sys_id:
            raise HTTPException(400, "sys_id is required for servicenow")
        connector = get_servicenow_connector()
        incident = connector.get_incident(sys_id)
        text = "\n".join(f"{k}: {v}" for k, v in incident.items())
        return text.encode("utf-8"), f"servicenow_{sys_id}.txt"

    if provider == "salesforce":
        sobject, record_id = body.get("sobject"), body.get("record_id")
        if not (sobject and record_id):
            raise HTTPException(400, "sobject and record_id are required for salesforce")
        connector = get_salesforce_connector(user_id)
        record = connector.get_record(sobject_type=sobject, record_id=record_id)
        text = "\n".join(f"{k}: {v}" for k, v in record.items() if not str(k).startswith("attributes"))
        return text.encode("utf-8"), f"salesforce_{sobject}_{record_id}.txt"

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
      file_share:    {"path": "..."}
      lms:           {"file_id": "..."}
      github:        {"owner": "...", "repo": "...", "path": "...", "ref": "..."}
      gmail:         {"message_id": "...", "attachment_id": "...", "filename": "..."}
      google_drive:  {"file_id": "..."}
      sharepoint:    {"drive_id": "...", "item_id": "..."}
      azure_devops:  {"project": "...", "repo_id": "...", "path": "..."}
      jira:          {"issue_key": "..."}
      servicenow:    {"sys_id": "..."}
      salesforce:    {"sobject": "...", "record_id": "..."}
      teams:         {"team_id": "...", "channel_id": "...", "limit": 50}

    An optional "session_id" key in the body (any provider) attaches this
    document to that already-active chat session instead of starting a new,
    isolated one - the frontend sends its current session_id once a chat is
    underway, so adding a second/third connector file doesn't orphan the
    ones added before it.
    """
    user_id = _user_id(request)
    client_token = f"connector:{user_id}"

    content, filename = await _fetch_connector_file(provider, body, user_id)

    return await pipeline_bridge.ingest_bytes(
        provider=provider, content=content, filename=filename,
        user_id=user_id, client_token=client_token,
        session_id=body.get("session_id") or None,
    )
