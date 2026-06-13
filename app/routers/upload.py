import asyncio
import json
import os
import time
import uuid
import logging
from typing import List, AsyncGenerator
from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse

from app.models.schemas import APIIngestRequest, IngestResponse
from app.services.ingestion import (
    parse_file, scrape_url, fetch_api,
    MAX_FILE_SIZE_MB, MAX_FILES_PER_SESSION,
)
from app.services.vector_store import get_vector_store
from app.services.rag import chunk_text, chunk_text_with_offsets, find_page

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt"}

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@router.post("/files")
async def upload_files(files: List[UploadFile] = File(...)):
    # Validate before streaming starts
    if len(files) > MAX_FILES_PER_SESSION:
        raise HTTPException(400, f"Maximum {MAX_FILES_PER_SESSION} files allowed per session")
    for file in files:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    # Read all content before streaming (multipart is consumed once)
    file_data = []
    for file in files:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise HTTPException(400, f"{file.filename} exceeds {MAX_FILE_SIZE_MB}MB limit")
        file_data.append((file.filename, content))

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        session_id = uuid.uuid4()
        all_chunks = []
        all_metadatas = []

        logger.info(f"[UPLOAD] session={session_id}, files={len(file_data)}")

        for i, (filename, content) in enumerate(file_data):
            ext = os.path.splitext(filename)[1].upper().lstrip('.') or 'DOC'
            stage = f"parse_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage,
                       "title": f"Processing {ext}",
                       "message": f"Processing the doc — {filename}"})
            await asyncio.sleep(0)
            t1 = time.time()
            text, page_spans = await parse_file(content, filename)
            elapsed = round(time.time() - t1, 2)
            logger.info(f"[PARSE] {filename} → {elapsed}s")
            yield sse({"type": "step", "status": "done", "stage": stage,
                       "message": f"Processing the doc — {filename}", "elapsed": elapsed})
            await asyncio.sleep(0)

            stage_ext = f"chunk_{i}"
            yield sse({"type": "step", "status": "start", "stage": stage_ext,
                       "title": "Extracting text",
                       "message": "Extracting the text"})
            await asyncio.sleep(0)
            t1 = time.time()
            chunks_with_offsets = chunk_text_with_offsets(text)
            for chunk, start_idx in chunks_with_offsets:
                page_number, confidence = find_page(start_idx, page_spans)
                all_chunks.append(chunk)
                all_metadatas.append({
                    "source": filename,
                    "chunk_index": len(all_metadatas),
                    "page_number": page_number,
                    "confidence": confidence,
                })
            chunks = [c for c, _ in chunks_with_offsets]
            elapsed = round(time.time() - t1, 2)
            logger.info(f"[CHUNK] {filename} → {len(chunks)} chunks, {elapsed}s")
            yield sse({"type": "step", "status": "done", "stage": stage_ext,
                       "message": f"Extracting the text — {len(chunks)} chunks", "elapsed": elapsed})
            await asyncio.sleep(0)

        yield sse({"type": "step", "status": "start", "stage": "embed",
                   "title": "Storing chunks",
                   "message": "Storing the chunks"})
        await asyncio.sleep(0)
        t1 = time.time()
        try:
            vector_store = get_vector_store(str(session_id))
            vector_store.add_texts(all_chunks, metadatas=all_metadatas)
        except Exception as e:
            logger.error(f"[UPLOAD] Embed/store failed: {e}")
            yield sse({"type": "error", "message": f"Storage failed: {e}"})
            return
        elapsed = round(time.time() - t1, 2)
        logger.info(f"[STORE] {len(all_chunks)} vectors stored, {elapsed}s")
        yield sse({"type": "step", "status": "done", "stage": "embed",
                   "message": f"Storing the chunks — {len(all_chunks)} vectors", "elapsed": elapsed})
        await asyncio.sleep(0)

        total = round(time.time() - t0, 2)
        logger.info(f"[UPLOAD] Complete — session={session_id}, total={total}s")
        yield sse({"type": "complete", "session_id": str(session_id),
                   "files_processed": len(file_data), "total_elapsed": total,
                   "completion_message": "Document processed"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/url")
async def ingest_url(request: dict):
    url = request.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    session_id = str(uuid.uuid4())

    async def stream() -> AsyncGenerator[str, None]:
        t0 = time.time()
        logger.info(f"[URL] session={session_id}, url={url}")

        yield sse({"type": "step", "status": "start", "stage": "scrape",
                   "title": "Processing URL",
                   "message": "Processing the doc — fetching URL"})
        await asyncio.sleep(0)
        t1 = time.time()
        try:
            text = await scrape_url(url)
        except Exception as e:
            logger.error(f"[URL] Scrape failed: {e}")
            yield sse({"type": "error", "message": f"Failed to scrape URL: {e}"})
            return
        elapsed = round(time.time() - t1, 2)
        logger.info(f"[SCRAPE] Done — chars={len(text)}, {elapsed}s")
        yield sse({"type": "step", "status": "done", "stage": "scrape",
                   "message": "Processing the doc — fetched URL", "elapsed": elapsed})
        await asyncio.sleep(0)

        yield sse({"type": "step", "status": "start", "stage": "chunk",
                   "title": "Extracting text",
                   "message": "Extracting the text"})
        await asyncio.sleep(0)
        t1 = time.time()
        chunks = chunk_text(text)
        elapsed = round(time.time() - t1, 2)
        logger.info(f"[CHUNK] URL → {len(chunks)} chunks, {elapsed}s")
        yield sse({"type": "step", "status": "done", "stage": "chunk",
                   "message": f"Extracting the text — {len(chunks)} chunks", "elapsed": elapsed})
        await asyncio.sleep(0)

        yield sse({"type": "step", "status": "start", "stage": "embed",
                   "title": "Storing chunks",
                   "message": "Storing the chunks"})
        await asyncio.sleep(0)
        t1 = time.time()
        try:
            vector_store = get_vector_store(session_id)
            metadatas = [{"source": url, "chunk_index": i} for i in range(len(chunks))]
            vector_store.add_texts(chunks, metadatas=metadatas)
        except Exception as e:
            logger.error(f"[URL] Embed/store failed: {e}")
            yield sse({"type": "error", "message": f"Storage failed: {e}"})
            return
        elapsed = round(time.time() - t1, 2)
        logger.info(f"[STORE] {len(chunks)} vectors stored, {elapsed}s")
        yield sse({"type": "step", "status": "done", "stage": "embed",
                   "message": f"Storing the chunks — {len(chunks)} vectors", "elapsed": elapsed})
        await asyncio.sleep(0)

        total = round(time.time() - t0, 2)
        logger.info(f"[URL] Complete — session={session_id}, total={total}s")
        yield sse({"type": "complete", "session_id": session_id, "total_elapsed": total,
                   "completion_message": "Document processed"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/api", response_model=IngestResponse)
async def ingest_api(request: APIIngestRequest):
    t0 = time.time()
    session_id = request.session_id or uuid.uuid4()
    logger.info(f"[API] Starting ingest — url={request.url}, session={session_id}")

    try:
        text = await fetch_api(request.url, request.headers)
    except Exception as e:
        logger.error(f"[API] Fetch failed — url={request.url}, error={e}")
        raise HTTPException(422, f"Failed to fetch API: {e}")

    chunks = chunk_text(text)
    logger.info(f"[CHUNK] API content → {len(chunks)} chunks")
    vector_store = get_vector_store(str(session_id))
    metadatas = [{"source": request.url, "chunk_index": i} for i in range(len(chunks))]
    vector_store.add_texts(chunks, metadatas=metadatas)
    logger.info(f"[API] Complete — {len(chunks)} chunks stored, time={time.time()-t0:.2f}s")

    return IngestResponse(session_id=session_id, message="API data processed and ready for chat")
