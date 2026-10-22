import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.core.logging_config import setup_logging
from app.routers import chat, upload, auth as auth_router, admin as admin_router, knowledge_base
from app.connectors import router as connectors_router
from app.services import auth_service

load_dotenv(override=True)
setup_logging()

logger = logging.getLogger(__name__)

from app.config import DEFAULT_SESSION_ID

_STATIC_DIR  = os.path.join(os.path.dirname(__file__), "static")
_SAMPLE_FILE = os.path.join(os.path.dirname(__file__), "data", "sample-ai-overview.txt")

# Routes that do NOT require authentication
# The OAuth callback paths are here (not under /connectors/) because the
# browser lands on them directly from Google's/Microsoft's redirect with no
# Authorization header - they can't be gated behind Bearer auth. They must
# match GOOGLE_REDIRECT_URI / GOOGLE_DRIVE_REDIRECT_URI / OUTLOOK_REDIRECT_URI.
# (Google Chat and Outlook Chat have no callback of their own - they share
# the Gmail/Outlook token via mirroring in gmail_callback/outlook_callback.)
_PUBLIC_PREFIXES = (
    "/auth", "/health", "/auth.html", "/auth.css", "/auth.js",
    "/gmail/callback", "/google-drive/callback", "/outlook/callback",
    "/privacy", "/terms",
    "/admin.html", "/admin.css", "/admin.js",
)
_PROTECTED_PREFIXES = ("/chat/", "/upload/", "/connectors/", "/knowledge-bases", "/sessions/")

# Routes under here require a token whose decoded role == "admin" - stricter
# than _PROTECTED_PREFIXES, which just requires any logged-in user.
# /auth/admin-login itself is public (it's how an admin token is obtained),
# handled by the "/auth" entry in _PUBLIC_PREFIXES above.
_ADMIN_PREFIXES = ("/admin/",)


async def _seed_default_session():
    """Ingest the sample file into the default session if not already done."""
    try:
        from app.services.vector_store import get_vector_store, session_has_data
        from app.services.rag import chunk_text
        from app.routers.upload import _add_texts_batched

        if session_has_data(DEFAULT_SESSION_ID):
            logger.info("[DEFAULT] Sample session already seeded - skipping")
            return

        def _read_sample() -> str:
            with open(_SAMPLE_FILE, "r", encoding="utf-8") as f:
                return f.read()

        text = await asyncio.to_thread(_read_sample)

        chunks = chunk_text(text)
        store = get_vector_store(DEFAULT_SESSION_ID)
        metadatas = [{"source": "sample-ai-overview.txt", "chunk_index": i} for i in range(len(chunks))]
        _add_texts_batched(store, chunks, metadatas)
        logger.info(f"[DEFAULT] Sample seeded - {len(chunks)} chunks into session={DEFAULT_SESSION_ID}")
    except Exception as e:
        logger.warning(f"[DEFAULT] Could not seed sample session: {e}")


async def _warmup_embeddings():
    """Make a dummy embed call so the HF connection + credentials are validated before the first user."""
    try:
        from app.services.vector_store import get_embeddings
        embeddings = get_embeddings()
        await asyncio.to_thread(embeddings.embed_query, "warmup")
        logger.info("[WARMUP] Embedding model ready")
    except Exception as e:
        logger.warning(f"[WARMUP] Embedding warmup failed: {e}")


async def _warmup_bm25():
    """Pre-build the BM25 index for the default session so the first retrieval is instant."""
    try:
        from app.services.vector_store import get_vector_store
        from app.services.rag import _get_or_build_bm25
        store = get_vector_store(DEFAULT_SESSION_ID)
        _get_or_build_bm25(store, DEFAULT_SESSION_ID)
        logger.info("[WARMUP] BM25 index ready for default session")
    except Exception as e:
        logger.warning(f"[WARMUP] BM25 warmup failed: {e}")


async def _warmup_reranker():
    """Make a dummy reranker call so the active reranker provider's connection
    is established. Reuses rag._rerank() (not a second hand-rolled call) so
    this warmup can't drift out of sync with the real reranking code path."""
    try:
        from app.services.rag import _rerank
        from langchain_core.documents import Document
        await asyncio.to_thread(_rerank, "warmup query", [Document(page_content="warmup passage")], 1)
        logger.info("[WARMUP] Reranker ready")
    except Exception as e:
        logger.warning(f"[WARMUP] Reranker warmup failed: {e}")


async def _warmup_parser():
    """Parse a minimal PDF so LiteParse initialises its internal models before the first user upload."""
    try:
        import tempfile
        from app.services.ingestion import parse_file
        # Minimal valid single-page PDF (no text content - just enough to trigger LiteParse init)
        minimal_pdf = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
        )
        await parse_file(minimal_pdf, "warmup.pdf")
        logger.info("[WARMUP] LiteParse parser ready")
    except Exception as e:
        logger.warning(f"[WARMUP] Parser warmup failed: {e}")


async def _warmup():
    """Run all warmup tasks in parallel - failures are logged but never block startup."""
    logger.info("[WARMUP] Pre-loading models and indexes...")
    await asyncio.gather(
        _warmup_embeddings(),
        _warmup_bm25(),
        _warmup_reranker(),
        _warmup_parser(),
        return_exceptions=True,
    )
    logger.info("[WARMUP] Done - server is ready to serve requests without cold starts")


async def _periodic_cleanup():
    """Every 3 minutes mark pending DB rows older than 5 minutes as failed.

    Rows get stuck as 'pending' when a user refreshes mid-upload - the upload
    process never completes so mark_complete never runs. This janitor sweeps them
    up so the DB doesn't accumulate ghost records indefinitely.
    """
    await asyncio.sleep(60)  # let server fully start before first run
    while True:
        try:
            from app.services.dedup import cleanup_stale_pending
            await asyncio.to_thread(cleanup_stale_pending, 5)
        except Exception as e:
            logger.warning(f"[CLEANUP] Periodic cleanup error: {e}")
        await asyncio.sleep(180)  # run every 3 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting...")
    await _seed_default_session()
    await _warmup()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleanup_task
    logger.info("Server shutting down.")


app = FastAPI(
    title="RAG Data Assistant",
    description="Chat with your documents using AI",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Auth middleware ────────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Admin API routes need a token with role == "admin", not just any
    # logged-in user - checked before the generic protected-prefix block.
    if any(path.startswith(p) for p in _ADMIN_PREFIXES):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else None
        if not token:
            return JSONResponse({"detail": "Admin authentication required"}, status_code=401)
        user = auth_service.verify_token(token)
        if not user:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
        if user.get("role") != "admin":
            return JSONResponse({"detail": "Admin access required"}, status_code=403)
        request.state.user = user

    # Only enforce auth on protected API routes
    elif any(path.startswith(p) for p in _PROTECTED_PREFIXES):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else None
        if not token:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        user = auth_service.verify_token(token)
        if not user:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
        request.state.user = user

    response = await call_next(request)

    # Static assets (JS/CSS/HTML) change during active development - force the
    # browser to always revalidate with the server (a cheap 304 via ETag/
    # Last-Modified when unchanged) instead of silently serving a stale cached
    # copy with no request at all, which otherwise means a shipped frontend
    # fix doesn't reach an already-open tab until a hard refresh.
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"

    return response


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth_router.router)
app.include_router(upload.router)
app.include_router(chat.router)
app.include_router(admin_router.router)
app.include_router(knowledge_base.router)
app.include_router(connectors_router.router)
app.include_router(connectors_router.unlock_router)
app.include_router(connectors_router.oauth_callback_router)

if os.getenv("WHATSAPP_ENABLED", "false").lower() == "true":
    from app.routers import whatsapp
    app.include_router(whatsapp.router)
    logger.info("WhatsApp webhook active at /whatsapp/webhook")


@app.get("/health")
def health():
    return {"status": "ok"}


# Serve auth.html explicitly so the browser can reach it
@app.get("/auth.html")
def serve_auth():
    return FileResponse(os.path.join(_STATIC_DIR, "auth.html"))


# Serve admin.html explicitly, same reason - the login page for the single
# fixed admin account must be reachable with no token.
@app.get("/admin.html")
def serve_admin():
    return FileResponse(os.path.join(_STATIC_DIR, "admin.html"))


# Public Privacy Policy / Terms of Service pages - also what Google/Microsoft's
# OAuth consent screen setup asks for as "Privacy Policy URL" / "Terms of
# Service URL" when configuring Sign in with Google/Microsoft.
@app.get("/privacy")
def serve_privacy():
    return FileResponse(os.path.join(_STATIC_DIR, "privacy.html"))


@app.get("/terms")
def serve_terms():
    return FileResponse(os.path.join(_STATIC_DIR, "terms.html"))


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
