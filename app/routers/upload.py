import asyncio
import json
import os
import time
import uuid
import logging
from datetime import datetime, timezone
from typing import List, AsyncGenerator
from fastapi import APIRouter, File, UploadFile, HTTPException, Header
from fastapi.responses import StreamingResponse

from app.config import cfg
from app.models.schemas import APIIngestRequest, IngestResponse, ConfirmRequest, ConfirmResponse
from app.services.ingestion import parse_file, scrape_url, fetch_api
from app.services.vector_store import get_vector_store
from app.services.rag import chunk_text, chunk_text_with_offsets, find_page, get_llm
from app.services import dedup as dedup_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])

EMBED_BATCH_SIZE = 32


def _add_texts_batched(store, texts: list[str], metadatas: list[dict]) -> None:
    """Embed and store in fixed-size batches to avoid HF API payload/timeout limits."""
    total_batches = -(-len(texts) // EMBED_BATCH_SIZE)  # ceiling division
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch_t = texts[i: i + EMBED_BATCH_SIZE]
        batch_m = metadatas[i: i + EMBED_BATCH_SIZE]
        store.add_texts(batch_t, metadatas=batch_m)
        logger.info(f"[EMBED] batch {i // EMBED_BATCH_SIZE + 1}/{total_batches} — {len(batch_t)} chunks")

EMBED_BATCH_SIZE = 32  # max chunks per HuggingFace API call


def _add_texts_batched(store, texts: list[str], metadatas: list[dict]) -> None:
    """Call store.add_texts in fixed-size batches to avoid HF API payload/timeout limits."""
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch_t = texts[i: i + EMBED_BATCH_SIZE]
        batch_m = metadatas[i: i + EMBED_BATCH_SIZE]
        store.add_texts(batch_t, metadatas=batch_m)
        logger.info(f"[EMBED] batch {i // EMBED_BATCH_SIZE + 1}/{-(-len(texts) // EMBED_BATCH_SIZE)} — {len(batch_t)} chunks")


async def _build_graph_async(chunks: list[str], session_id: str, filename: str):
    try:
        from app.services.graph_store import build_graph
        llm = get_llm()
        ok = await asyncio.to_thread(build_graph, chunks, session_id, llm)
        if ok:
            logger.info(f"[GRAPH] Built graph for {filename} session={session_id}")
        else:
            logger.warning(f"[GRAPH] Graph build skipped for {filename} (no Neo4j or no entities)")
    except Exception as e:
        logger.warning(f"[GRAPH] entity extraction failed for {filename}: {e}")


async def _copy_graph_async(old_session_id: str, new_session_id: str):
    try:
        from app.services.graph_store import copy_graph_session
        await asyncio.to_thread(copy_graph_session, old_session_id, new_session_id)
    except Exception as e:
        logger.warning(f"[GRAPH] copy_graph_session failed: {e}")

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _human_ago(dt) -> str:
    if dt is None:
        return "unknown"
    diff = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else datetime.now(timezone.utc) - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 86400:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


# ── /upload/confirm ───────────────────────────────────────────────────────────

@router.post("/confirm", response_model=ConfirmResponse)
async def confirm_dedup(request: ConfirmRequest):
    resolved = dedup_svc.resolve_confirm(request.confirm_token, request.action)
    if not resolved:
        raise HTTPException(404, "Confirm token not found or already expired")
    return ConfirmResponse(status="ok", message=f"Action '{request.action}' received")


# ── /upload/files ─────────────────────────────────────────────────────────────

@router.post("/files")
async def upload_files(
    files: List[UploadFile] = File(...),
    x_client_token: str = Header(default="anonymous"),
):
    if len(files) > cfg.max_files_per_session:
        raise HTTPException(400, f"Maximum {cfg.max_files_per_session} files allowed per session")

    for file in files:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    file_data = []
    total_size = 0
    for file in files:
        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        total_size += len(content)
        if size_mb > cfg.max_file_size_mb:
            raise HTTPException(400, f"{file.filename} exceeds {cfg.max_file_size_mb} MB limit")
        if total_size > cfg.max_session_size_mb * 1024 * 1024:
            raise HTTPException(400, f"Total upload size exceeds {cfg.max_session_size_mb} MB session limit")
        file_data.append((file.filename, content))

    client_token = x_client_token or "anonymous"

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        master_session_id = str(uuid.uuid4())
        master_store = get_vector_store(master_session_id)
        total_chunks = 0

        logger.info(f"[UPLOAD] session={master_session_id}, files={len(file_data)}, client={client_token}")

        for i, (filename, content) in enumerate(file_data):
            ext   = os.path.splitext(filename)[1].upper().lstrip('.') or 'DOC'
            stage = f"parse_{i}"

            # ── Dedup check ──────────────────────────────────────────────────
            yield sse({"type": "step", "status": "start", "stage": f"dedup_{i}",
                       "title": "Checking for duplicates",
                       "message": f"Checking {filename}..."})
            await asyncio.sleep(0)

            t1 = time.time()
            text_for_dedup, page_spans = await parse_file(content, filename)
            avg_conf = None
            if page_spans:
                confs = [float(p["confidence"]) for p in page_spans if p.get("confidence")]
                avg_conf = sum(confs) / len(confs) if confs else None

            result = await dedup_svc.check(
                client_token  = client_token,
                filename      = filename,
                file_size     = len(content),
                content       = content,
                parsed_text   = text_for_dedup,
                avg_confidence= avg_conf,
            )

            if result.action == "confirm":
                # Ask user — pause SSE stream
                confirm_token = str(uuid.uuid4())
                gate = dedup_svc.create_confirm_gate(confirm_token)
                doc  = result.doc

                cached_chunks = dedup_svc._mem_cache.get(
                    (client_token, dedup_svc.sha256(content)), (None, 0)
                )[1]
                chunks_stored = (doc.chunks_stored if doc else None) or cached_chunks
                yield sse({
                    "type":          "dedup_confirm",
                    "confirm_token": confirm_token,
                    "filename":      filename,
                    "file_size":     _human_size(len(content)),
                    "chunks_stored": chunks_stored,
                    "reason":        result.reason,
                    "stage":         f"dedup_{i}",
                })

                try:
                    await asyncio.wait_for(gate.wait(), timeout=dedup_svc.CONFIRM_TIMEOUT_S)
                    action = dedup_svc.consume_confirm(confirm_token)
                except asyncio.TimeoutError:
                    dedup_svc.consume_confirm(confirm_token)
                    action = "reprocess"
                    logger.info(f"[DEDUP] {filename} → confirmation timeout, reprocessing")

                if action == "reuse":
                    copied = await dedup_svc.copy_vectors_to_master(result.session_id, master_store)
                    elapsed = round(time.time() - t1, 2)
                    logger.info(f"[DEDUP] {filename} → reused, copied {copied} vectors")
                    # Copy Neo4j graph entities from old session to new master session
                    asyncio.ensure_future(_copy_graph_async(result.session_id, master_session_id))

                    yield sse({"type": "step", "status": "done", "stage": f"dedup_{i}",
                               "message": f"{filename} — duplicate detected, reusing existing embeddings",
                               "elapsed": elapsed})
                    await asyncio.sleep(0)

                    yield sse({"type": "step", "status": "start", "stage": f"parse_{i}",
                               "title": f"Processing {ext}",
                               "message": f"Loading from cache — {filename}"})
                    await asyncio.sleep(0)
                    yield sse({"type": "step", "status": "done", "stage": f"parse_{i}",
                               "message": f"Source: {filename}",
                               "elapsed": 0.0})
                    await asyncio.sleep(0)

                    yield sse({"type": "step", "status": "start", "stage": f"chunk_{i}",
                               "title": "Extracting text",
                               "message": "Re-embedding from cached text" if copied == 0 else "Loading chunks from cache"})
                    await asyncio.sleep(0)

                    if copied == 0:
                        # Old collection gone — re-embed from already-parsed text (batched)
                        logger.warning(f"[DEDUP] {filename} → vector copy failed, re-embedding from parsed text")
                        chunks_with_offsets = chunk_text_with_offsets(text_for_dedup)
                        batch_texts, batch_metas = [], []
                        for chunk, start_idx in chunks_with_offsets:
                            page_number, confidence = find_page(start_idx, page_spans)
                            batch_metas.append({
                                "source":      filename,
                                "chunk_index": total_chunks + len(batch_texts),
                                "page_number": page_number,
                                "confidence":  confidence,
                            })
                            batch_texts.append(chunk)
                        if batch_texts:
                            _add_texts_batched(master_store, batch_texts, batch_metas)
                        file_chunks   = len(batch_texts)
                        total_chunks += file_chunks
                        logger.info(f"[CHUNK][REUSE] {filename} → re-embedded {file_chunks} chunks into master session={master_session_id}")
                        dedup_svc.update_cache(client_token, dedup_svc.sha256(content), master_session_id, file_chunks)
                    else:
                        file_chunks = copied
                        total_chunks += copied
                        logger.info(f"[CHUNK][REUSE] {filename} → copied {file_chunks} chunks from old session={result.session_id} into master session={master_session_id}")

                    page_count = len(set(
                        p["page_number"] for p in page_spans if p.get("page_number")
                    )) if page_spans else 1
                    logger.info(f"[CHUNK][REUSE] {filename} → total_chunks={file_chunks}, pages={page_count}")
                    yield sse({"type": "step", "status": "done", "stage": f"chunk_{i}",
                               "message": f"Extracting the text — {file_chunks} chunks · {page_count} page(s)",
                               "elapsed": 0.0})
                    await asyncio.sleep(0)
                    continue
                # else fall through to process fresh

            elif result.action == "reuse":
                # Direct reuse (shouldn't happen with confirm gate, but safety)
                copied = await dedup_svc.copy_vectors_to_master(result.session_id, master_store)
                total_chunks += copied
                yield sse({"type": "step", "status": "done", "stage": f"dedup_{i}",
                           "message": f"{filename} — reused existing embeddings",
                           "elapsed": round(time.time() - t1, 2)})
                await asyncio.sleep(0)
                continue

            # ── Process fresh ────────────────────────────────────────────────
            yield sse({"type": "step", "status": "done", "stage": f"dedup_{i}",
                       "message": f"{filename} — new file, processing",
                       "elapsed": round(time.time() - t1, 2)})
            await asyncio.sleep(0)

            # Insert pending DB entry
            content_hash = dedup_svc.sha256(content)
            first_c, mid_c, last_c = dedup_svc._extract_chunks(text_for_dedup)
            db = await asyncio.to_thread(dedup_svc._get_db_session)
            if db is None:
                logger.warning(f"[DB] Could not get DB session — skipping pending insert for {filename}")
            else:
                try:
                    success = await asyncio.to_thread(
                        dedup_svc._insert_pending, db, client_token, master_session_id,
                        filename.lower(), len(content), content_hash,
                        first_c, mid_c, last_c, avg_conf
                    )
                    if not success:
                        logger.warning(f"[DB] pending insert returned False for {filename} — record was NOT written to DB")
                finally:
                    await asyncio.to_thread(db.close)

            # Parse
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Processing {ext}",
                       "message": f"Processing the doc — {filename}"})
            await asyncio.sleep(0)
            t1 = time.time()
            text, page_spans = text_for_dedup, page_spans  # already parsed above
            elapsed = round(time.time() - t1, 2)
            logger.info(f"[PARSE] {filename} → reused parsed text")
            yield sse({"type": "step", "status": "done", "stage": stage,
                       "message": f"Processing the doc — {filename}", "elapsed": elapsed})
            await asyncio.sleep(0)

            # Chunk
            stage_ext = f"chunk_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage_ext,
                       "title": "Extracting text", "message": "Extracting the text"})
            await asyncio.sleep(0)
            t1 = time.time()
            chunks_with_offsets = chunk_text_with_offsets(text)
            batch_texts, batch_metas = [], []
            for chunk, start_idx in chunks_with_offsets:
                page_number, confidence = find_page(start_idx, page_spans)
                batch_metas.append({
                    "source":      filename,
                    "chunk_index": total_chunks + len(batch_texts),
                    "page_number": page_number,
                    "confidence":  confidence,
                })
                batch_texts.append(chunk)
            if batch_texts:
                _add_texts_batched(master_store, batch_texts, batch_metas)
            file_chunks   = len(batch_texts)
            total_chunks += file_chunks
            elapsed = round(time.time() - t1, 2)
            logger.info(f"[CHUNK] {filename} → {file_chunks} chunks, {elapsed}s")
            dedup_svc.update_cache(client_token, content_hash, master_session_id, file_chunks)
            yield sse({"type": "step", "status": "done", "stage": stage_ext,
                       "message": f"Extracting the text — {file_chunks} chunks",
                       "elapsed": elapsed})
            await asyncio.sleep(0)

            # Build Neo4j graph async (doesn't block SSE stream)
            asyncio.ensure_future(_build_graph_async(batch_texts, master_session_id, filename))

            # Mark complete in DB
            db = await asyncio.to_thread(dedup_svc._get_db_session)
            if db is None:
                logger.warning(f"[DB] Could not get DB session — skipping mark_complete for {filename}")
            else:
                try:
                    await asyncio.to_thread(
                        dedup_svc._mark_complete, db, client_token, content_hash, file_chunks
                    )
                    logger.info(f"[DB] mark_complete succeeded for {filename}")
                except Exception as e:
                    logger.warning(f"[DB] mark_complete failed for {filename}: {type(e).__name__}: {e}")
                    try:
                        await asyncio.to_thread(dedup_svc._mark_failed, db, client_token, content_hash)
                    except Exception:
                        pass
                finally:
                    await asyncio.to_thread(db.close)

        total = round(time.time() - t0, 2)
        logger.info(f"[UPLOAD] Complete — session={master_session_id}, chunks={total_chunks}, total={total}s")
        yield sse({"type": "complete", "session_id": master_session_id,
                   "files_processed": len(file_data), "total_elapsed": total,
                   "completion_message": "Document processed"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── /upload/url ───────────────────────────────────────────────────────────────

@router.post("/url")
async def ingest_url(
    request: dict,
    x_client_token: str = Header(default="anonymous"),
):
    url = request.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    session_id   = str(uuid.uuid4())
    client_token = x_client_token or "anonymous"

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        logger.info(f"[URL] session={session_id}, url={url}")

        yield sse({"type": "step", "status": "start", "stage": "scrape",
                   "title": "Processing URL", "message": "Fetching URL content"})
        await asyncio.sleep(0)
        t1 = time.time()
        try:
            text = await scrape_url(url)
        except Exception as e:
            logger.error(f"[URL] Scrape failed: {e}")
            yield sse({"type": "error", "message": f"Failed to scrape URL: {e}"})
            return
        elapsed = round(time.time() - t1, 2)
        yield sse({"type": "step", "status": "done", "stage": "scrape",
                   "message": "URL fetched", "elapsed": elapsed})
        await asyncio.sleep(0)

        yield sse({"type": "step", "status": "start", "stage": "chunk",
                   "title": "Extracting text", "message": "Extracting the text"})
        await asyncio.sleep(0)
        t1 = time.time()
        chunks = chunk_text(text)
        elapsed = round(time.time() - t1, 2)
        yield sse({"type": "step", "status": "done", "stage": "chunk",
                   "message": f"Extracting the text — {len(chunks)} chunks", "elapsed": elapsed})
        await asyncio.sleep(0)

        yield sse({"type": "step", "status": "start", "stage": "embed",
                   "title": "Storing chunks", "message": "Storing the chunks"})
        await asyncio.sleep(0)
        t1 = time.time()
        try:
            vector_store = get_vector_store(session_id)
            metadatas    = [{"source": url, "chunk_index": i} for i in range(len(chunks))]
            vector_store.add_texts(chunks, metadatas=metadatas)
        except Exception as e:
            logger.error(f"[URL] Embed/store failed: {e}")
            yield sse({"type": "error", "message": f"Storage failed: {e}"})
            return
        elapsed = round(time.time() - t1, 2)
        yield sse({"type": "step", "status": "done", "stage": "embed",
                   "message": f"Stored {len(chunks)} chunks", "elapsed": elapsed})
        await asyncio.sleep(0)

        total = round(time.time() - t0, 2)
        yield sse({"type": "complete", "session_id": session_id, "total_elapsed": total,
                   "completion_message": "Document processed"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── /upload/api ───────────────────────────────────────────────────────────────

@router.post("/api", response_model=IngestResponse)
async def ingest_api(request: APIIngestRequest):
    t0         = time.time()
    session_id = request.session_id or uuid.uuid4()
    logger.info(f"[API] Starting ingest — url={request.url}, session={session_id}")

    try:
        text = await fetch_api(request.url, request.headers)
    except Exception as e:
        logger.error(f"[API] Fetch failed — url={request.url}, error={e}")
        raise HTTPException(422, f"Failed to fetch API: {e}")

    chunks       = chunk_text(text)
    vector_store = get_vector_store(str(session_id))
    metadatas    = [{"source": request.url, "chunk_index": i} for i in range(len(chunks))]
    vector_store.add_texts(chunks, metadatas=metadatas)
    logger.info(f"[API] Complete — {len(chunks)} chunks stored, time={time.time()-t0:.2f}s")

    return IngestResponse(session_id=session_id, message="API data processed and ready for chat")
