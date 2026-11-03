# ingestion_pipeline

Self-contained document/web ingestion pipeline: PDF/DOCX via LiteParse with
Docling fallback, native Docling backends for xlsx/xls/csv/ppt/pptx, plain
text/code files, zip archives (recursive), web scraping (Crawl4AI with
Playwright fallback), and generic JSON API fetching.

This folder has no dependency on the rest of this repo - copy it into any
other Python project as-is.

## As a library (in-process, no HTTP)

```python
from ingestion_pipeline import parse_file, scrape_url, fetch_api

text, page_spans = await parse_file(file_bytes, "report.pdf")
text = await scrape_url("https://example.com")
text = await fetch_api("https://api.example.com/data")
```

Install deps: `pip install -r ingestion_pipeline/requirements.txt && playwright install chromium`

Limits (zip entry size/count) default to env vars `INGESTION_MAX_FILE_SIZE_MB`
(default 15) and `INGESTION_ZIP_MAX_ENTRIES` (default 200), or pass them
per-call: `parse_file(content, name, max_file_size_mb=25, zip_max_entries=500)`.

## As a standalone microservice

```bash
cd ingestion_pipeline
pip install -r requirements.txt
playwright install --with-deps chromium
uvicorn service:app --host 0.0.0.0 --port 8080
```

Or with Docker (build context is this folder):

```bash
docker build -t ingestion-service .
docker run -p 8080:8080 ingestion-service
```

Endpoints: `POST /parse` (multipart file upload), `POST /scrape` (`{"url": ...}`),
`POST /fetch` (`{"url": ..., "headers": {...}}`), `GET /health`.
