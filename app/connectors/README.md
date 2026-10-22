# app/connectors - source-system connectors package

This package is a self-contained integration layer that lets a host
application browse and pull content from 9 external source systems (GitHub,
Gmail, Google Drive, Google Chat, Outlook, Outlook Chat, SharePoint,
Microsoft Teams, Azure DevOps Repos) and feed whatever the user picks into
the host's own RAG/document-ingestion pipeline.

It was built inside the "ARIA" RAG chatbot project but is written to be
**lifted out and dropped into a different host app** with minimal untangling.
This document is written for the developer doing that lift.

## What's in here

```
app/connectors/
  clients/              9 connector client modules + microsoft_graph.py (shared
                         Graph API helper used by sharepoint.py and teams.py).
                         Each client module exposes either a get_<provider>_connector()
                         factory function or a <Provider>Connector class. None of
                         these files import anything from the host app.
  token_store.py         Per-user OAuth token persistence (Gmail, Google Drive,
                         Google Chat, Outlook, Outlook Chat) backed by one DB
                         table (see below). Only host dependency:
                         app.db.session.get_session_factory, resolved lazily
                         and wrapped in try/except.
  models.py               The ConnectorToken SQLAlchemy model, on its OWN
                         declarative_base() - this package does not import or
                         depend on the host app's model module in any way.
  pipeline_bridge.py      THE ONE FILE that knows about the host app's RAG
                         internals. Exposes a single adapter function,
                         ingest_bytes(), that a new host app must re-implement
                         to point at its own ingestion pipeline. Every other
                         file in this package is ignorant of RAG/ingestion
                         concepts entirely.
  access_gate.py          The second lock in front of every /connectors/*
                         route - a shared CONNECTORS_ACCESS_KEY the deployment
                         owner sets, independent of normal user login. See
                         "The access gate" below.
  router.py               The FastAPI router(s). Exports `router` (prefix
                         "/connectors", all data-listing + login-initiation +
                         ingest endpoints, gated behind access_gate.py),
                         `unlock_router` (prefix "/connectors", just the one
                         POST /connectors/unlock endpoint, NOT gated - that's
                         how a caller gets a key to unlock the rest), and
                         `oauth_callback_router` (no prefix, the OAuth
                         callback endpoints that must live at a fixed literal
                         path).
```

## The access gate

Every route on `router` (everything except `/connectors/unlock` and the OAuth
callbacks) requires a second secret beyond the normal login token: a value
the deployment owner sets once in `CONNECTORS_ACCESS_KEY` (generate one with
`python -c "import secrets; print(secrets.token_urlsafe(32))"`). This exists
because a connected Gmail/GitHub session is real access to
someone's real third-party account - a step above ordinary app data, worth
its own separate gate rather than relying solely on the regular login.

Flow: the client calls `POST /connectors/unlock {"access_key": "..."}`; on a
match (compared in constant time via `hmac.compare_digest`, so a wrong guess
can't be timed to learn how close it was) the server returns a short-lived
(12 hour) signed token scoped to that one user. Every other `/connectors/*`
call must then include that token in an `X-Connectors-Unlock` header - missing
or invalid, and `require_connectors_unlock` (the dependency on `router`)
rejects the request with 403, regardless of how valid the normal Bearer login
token is. The raw access key itself is never stored server-side beyond the
single `os.getenv` read per check, never logged, and never echoed back in any
response - only the derived, non-reversible unlock token leaves the server.

If `CONNECTORS_ACCESS_KEY` isn't set at all, every gated route (and the
unlock endpoint itself) returns 503 rather than silently allowing access -
connectors stay off by default until a deployment owner deliberately turns
them on by setting this value.

## The one DB table

One table, `connector_tokens`, used by the 5 OAuth-based connectors (Gmail,
Google Drive, Google Chat, Outlook, Outlook Chat) to store per-user access/
refresh tokens. Gmail+Google Chat share one Google OAuth grant, and
Outlook+Outlook Chat share one Microsoft OAuth grant - each pair gets two
rows with identical tokens (mirrored at callback time; see gmail_callback/
outlook_callback in router.py), not one row shared between two providers.
The other 3 connectors (GitHub, SharePoint+Teams, Azure DevOps) are
authenticated with a single shared service credential from env vars instead.

Schema (see `app/connectors/models.py`):

| column         | type      | notes                                               |
|----------------|-----------|------------------------------------------------------|
| id             | UUID (pk) |                                                      |
| user_id        | String    | host app's user id (plain string, not assumed UUID) |
| provider       | String    | "gmail" \| "google_drive" \| "google_chat" \| "outlook" \| "outlook_chat" |
| access_token   | Text      |                                                      |
| refresh_token  | Text      | nullable                                            |
| expires_at     | DateTime  | nullable                                            |
| extra          | Text      | JSON-encoded provider-specific metadata             |
| created_at     | DateTime  |                                                      |
| updated_at     | DateTime  |                                                      |

Unique constraint on (user_id, provider).

### Running the migration in this project

This project hand-writes Alembic migrations (not autogenerated from
`Base.metadata`); the table is created by `alembic/versions/003_connector_tokens.py`.
Run it the same way any other migration in this repo is applied:

```
uv run alembic upgrade head
```

### Wiring the table into a different host app

`app/connectors/models.py` intentionally defines its own `declarative_base()`
instead of importing the host app's shared `Base`, specifically so this
package has zero coupling to the host's model module. A new host app has two
options:

1. If it also hand-writes migrations (recommended, and what this project
   does): write one migration that creates the `connector_tokens` table using
   the column list above (copy `alembic/versions/003_connector_tokens.py` as
   a starting point - it's already host-agnostic).
2. If it autogenerates schema from SQLAlchemy metadata, it can call
   `app.connectors.models.Base.metadata.create_all(engine)` as its own
   separate step alongside (not merged into) its own Base's `create_all()`.
   SQLAlchemy supports multiple independent declarative bases living in the
   same process without issue - this only becomes unsafe if something expects
   a single `Base.metadata` covering every table across both packages, which
   nothing in this codebase does.

## Env vars needed per connector

All of these are read directly via `os.getenv(...)` inside `clients/*.py` -
there is no required config object; a connector that's missing its vars just
reports as "not configured" at `GET /connectors/status` rather than crashing.
Pulled verbatim from this project's `.env.example`:

```
# Google OAuth - shared by Gmail + Google Drive + Google Chat connectors
# (Google Chat shares Gmail's grant - no separate client id/secret/redirect)
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
# Must match the redirect URI registered in Google Cloud Console.
GOOGLE_REDIRECT_URI=http://localhost:8000/gmail/callback
GOOGLE_DRIVE_REDIRECT_URI=http://localhost:8000/google-drive/callback

# Microsoft Entra ID / Microsoft Graph - SharePoint + Teams (app-only,
# client-credentials) AND Outlook + Outlook Chat (delegated user OAuth,
# same app registration's client id/secret, Outlook Chat shares Outlook's
# grant - no separate client id/secret/redirect for it).
MICROSOFT_TENANT_ID=your-tenant-id
MICROSOFT_CLIENT_ID=your-application-client-id
MICROSOFT_CLIENT_SECRET=your-client-secret
# Must match a "Web" platform redirect URI on that same app registration,
# with Mail.Read + Chat.Read + offline_access delegated permissions granted.
OUTLOOK_REDIRECT_URI=http://localhost:8000/outlook/callback

# Azure DevOps Repos
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-organization
AZURE_DEVOPS_PAT=your-personal-access-token

# GitHub - personal access token (repo read scope)
GITHUB_TOKEN=your-github-personal-access-token
```

Three of these vars (`GOOGLE_REDIRECT_URI`, `GOOGLE_DRIVE_REDIRECT_URI`,
`OUTLOOK_REDIRECT_URI`) are **not just config** - they must exactly match
the literal path the `oauth_callback_router` is mounted at in the host app
(see below), because the browser is redirected there directly by Google/
Microsoft with no `Authorization` header, so that path cannot sit behind
the host's normal auth middleware.

## The one adapter function a new host app must implement

`app/connectors/pipeline_bridge.py` exposes exactly one function:

```python
async def ingest_bytes(provider: str, content: bytes, filename: str,
                        user_id: str, client_token: str) -> dict:
    ...
    return {"session_id": str, "filename": str, "chunks": int, "source": provider}
```

`router.py`'s `POST /connectors/{provider}/ingest` endpoint fetches
connector-sourced bytes (via `_fetch_connector_file`, which only talks to
`clients/*` - no RAG knowledge) and then calls this one function to run them
through ingestion. A new host app rewrites the body of `ingest_bytes` to call
its own parse -> chunk -> embed (-> optional dedup) pipeline instead of this
project's `app.services.{ingestion,dedup,rag,vector_store}` and
`app.routers.upload`, keeping the same input/output contract so `router.py`
and everything upstream of it (the frontend, the 9 client modules) needs no
changes at all.

## Wiring this package into a host FastAPI app

This is literally all `app/main.py` does in this project (see it there for
the exact context: the auth middleware and its `_PROTECTED_PREFIXES` /
`_PUBLIC_PREFIXES` tuples):

```python
from app.connectors import router as connectors_router

app.include_router(connectors_router.router)
app.include_router(connectors_router.unlock_router)
app.include_router(connectors_router.oauth_callback_router)
```

Plus two entries in the host's own auth-gating logic:

```python
# Protect every /connectors/... route behind the same auth as the rest of the app:
_PROTECTED_PREFIXES = (..., "/connectors/")

# Leave the OAuth callback paths PUBLIC (no Bearer token on these requests):
_PUBLIC_PREFIXES = (..., "/gmail/callback", "/google-drive/callback", "/outlook/callback")
```

That's the entire integration surface: two `include_router` calls and two
auth-middleware prefix entries. Everything else (which connectors are
configured, OAuth login/callback flows, browsing, and the ingest bridge) is
self-contained inside this package plus the one `ingest_bytes` adapter.
