"""Standalone microservice wrapper around this package's parse_file/
scrape_url/fetch_api. Run directly:

    uvicorn ingestion_pipeline.service:app --host 0.0.0.0 --port 8080

Or via the Dockerfile in this same folder. Nothing in this file is required
to use the package as a library - import from ingestion_pipeline directly
for in-process use with no HTTP overhead.
"""
import logging

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from .core import fetch_api, parse_file, scrape_url

logger = logging.getLogger(__name__)

app = FastAPI(title="Ingestion Pipeline", version="1.0.0")


class ParseResponse(BaseModel):
    text: str
    page_spans: list[dict]


class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    text: str


class FetchApiRequest(BaseModel):
    url: str
    headers: dict[str, str] | None = None


class FetchApiResponse(BaseModel):
    text: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/parse", response_model=ParseResponse)
async def parse_endpoint(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text, page_spans = await parse_file(content, file.filename)
    except Exception as e:
        logger.error(f"[SERVICE] parse failed for {file.filename}: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    return ParseResponse(text=text, page_spans=page_spans)


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_endpoint(req: ScrapeRequest):
    try:
        text = await scrape_url(req.url)
    except Exception as e:
        logger.error(f"[SERVICE] scrape failed for {req.url}: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    return ScrapeResponse(text=text)


@app.post("/fetch", response_model=FetchApiResponse)
async def fetch_endpoint(req: FetchApiRequest):
    try:
        text = await fetch_api(req.url, req.headers)
    except Exception as e:
        logger.error(f"[SERVICE] fetch failed for {req.url}: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    return FetchApiResponse(text=text)
