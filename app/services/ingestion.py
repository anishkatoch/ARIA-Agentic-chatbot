import logging
import os
import tempfile
import time
import httpx
from liteparse import LiteParse
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", 15))
MAX_FILES_PER_SESSION = int(os.getenv("MAX_FILES_PER_SESSION", 3))

_parser = LiteParse()


def _avg_confidence(page) -> float | None:
    scores = [item.confidence for item in page.text_items if item.confidence is not None]
    return round(sum(scores) / len(scores), 4) if scores else None


async def parse_file(content: bytes, filename: str) -> tuple[str, list[dict]]:
    ext = os.path.splitext(filename)[1].lower()
    size_kb = len(content) / 1024
    logger.info(f"[PARSE] Starting — file={filename}, size={size_kb:.1f} KB, type={ext}")
    t0 = time.time()

    if ext == ".txt":
        text = content.decode("utf-8", errors="ignore")
        logger.info(f"[PARSE] Done (plain text) — chars={len(text)}, time={time.time()-t0:.2f}s")
        page_spans = [{"page_number": 1, "start": 0, "end": len(text), "confidence": None}]
        return text, page_spans

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = _parser.parse(tmp_path)
        full_text = ""
        page_spans = []
        for page in result.pages:
            start = len(full_text)
            page_spans.append({
                "page_number": page.page_num,
                "start": start,
                "end": start + len(page.text),
                "confidence": _avg_confidence(page),
            })
            full_text += page.text + "\n"
        logger.info(f"[PARSE] Done (LiteParse) — pages={len(page_spans)}, chars={len(full_text)}, time={time.time()-t0:.2f}s")
        return full_text, page_spans
    finally:
        os.unlink(tmp_path)


async def scrape_url(url: str) -> str:
    logger.info(f"[SCRAPE] Starting — url={url}")
    t0 = time.time()
    try:
        from crawl4ai import AsyncWebCrawler
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
            if result.markdown:
                logger.info(f"[SCRAPE] Done (Crawl4AI) — chars={len(result.markdown)}, time={time.time()-t0:.2f}s")
                return result.markdown
            raise ValueError("Empty content from Crawl4AI")
    except Exception as e:
        logger.warning(f"[SCRAPE] Crawl4AI failed ({e}) — falling back to Playwright")
        return await _scrape_with_playwright(url, t0)


async def _scrape_with_playwright(url: str, t0: float = None) -> str:
    if t0 is None:
        t0 = time.time()
    logger.info(f"[SCRAPE] Playwright launching — url={url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        content = await page.inner_text("body")
        await browser.close()
    logger.info(f"[SCRAPE] Done (Playwright) — chars={len(content)}, time={time.time()-t0:.2f}s")
    return content


async def fetch_api(url: str, headers: dict[str, str] | None = None) -> str:
    logger.info(f"[API] Fetching — url={url}, auth_headers={'yes' if headers else 'none'}")
    t0 = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()
        data = response.json()
    text = _flatten_json(data)
    logger.info(f"[API] Done — status={response.status_code}, chars={len(text)}, time={time.time()-t0:.2f}s")
    return text


def _flatten_json(data, separator: str = "\n") -> str:
    parts = []
    if isinstance(data, dict):
        for value in data.values():
            parts.append(_flatten_json(value))
    elif isinstance(data, list):
        for item in data:
            parts.append(_flatten_json(item))
    elif isinstance(data, str):
        parts.append(data)
    else:
        parts.append(str(data))
    return separator.join(filter(None, parts))
