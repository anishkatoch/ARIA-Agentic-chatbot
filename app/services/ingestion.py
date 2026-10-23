import asyncio
import logging
import os
import tempfile
import time
import zipfile
from io import BytesIO

import httpx
from liteparse import LiteParse
from playwright.async_api import async_playwright

from app.config import cfg

logger = logging.getLogger(__name__)

_parser = LiteParse()
_docling_converter = None

# Source/code and other plain-text-like extensions - read as raw UTF-8 text
# and chunked like .txt/.md, no layout parsing needed.
PLAIN_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".json", ".html", ".htm", ".js", ".ts", ".jsx", ".tsx",
    ".yaml", ".yml", ".xml", ".css", ".java", ".c", ".cpp", ".h", ".go", ".rb",
    ".php", ".sh", ".sql", ".ini", ".cfg", ".log", ".toml",
}


def _avg_confidence(page) -> float | None:
    scores = [item.confidence for item in page.text_items if item.confidence is not None]
    return round(sum(scores) / len(scores), 4) if scores else None


def _parse_with_liteparse(tmp_path: str) -> tuple[str, list[dict]]:
    result = _parser.parse(tmp_path)
    full_text  = ""
    page_spans = []
    for page in result.pages:
        start = len(full_text)
        page_spans.append({
            "page_number": page.page_num,
            "start":       start,
            "end":         start + len(page.text),
            "confidence":  _avg_confidence(page),
        })
        full_text += page.text + "\n"
    if not full_text.strip():
        raise ValueError("LiteParse returned no extractable text")
    return full_text, page_spans


def _get_docling_converter():
    # Imported lazily - Docling pulls in heavy ML deps and downloads models
    # on first use, so we only pay that cost when LiteParse actually fails.
    global _docling_converter
    if _docling_converter is None:
        from docling.document_converter import DocumentConverter
        _docling_converter = DocumentConverter()
    return _docling_converter


def _docling_page_confidence(report, page_no: int) -> float | None:
    score = report.pages.get(page_no)
    if score is None or score.mean_score != score.mean_score:  # NaN check
        return None
    return round(float(score.mean_score), 4)


def _parse_with_docling(tmp_path: str) -> tuple[str, list[dict]]:
    converter = _get_docling_converter()
    result = converter.convert(tmp_path)
    doc = result.document

    if not result.pages:
        text = doc.export_to_text()
        return text, [{"page_number": 1, "start": 0, "end": len(text), "confidence": None}]

    full_text  = ""
    page_spans = []
    for page in result.pages:
        page_text = doc.export_to_text(page_no=page.page_no)
        start = len(full_text)
        page_spans.append({
            "page_number": page.page_no,
            "start":       start,
            "end":         start + len(page_text),
            "confidence":  _docling_page_confidence(result.confidence, page.page_no),
        })
        full_text += page_text + "\n"
    return full_text, page_spans


async def _parse_zip(content: bytes, filename: str) -> tuple[str, list[dict]]:
    """Extracts every entry in the zip and parses each one through
    parse_file() recursively, concatenating the results with a per-file
    header so the combined text reads as one document but still lets the
    user ask about any individual file inside by name. Entries with a
    completely unparseable type are noted and skipped rather than failing
    the whole zip, same policy as a Gmail attachment that fails to parse."""
    full_text = ""
    page_spans: list[dict] = []

    max_entry_bytes = cfg.max_file_size_mb * 1024 * 1024

    def _read_entries() -> list[tuple[str, bytes]]:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            infos = [i for i in zf.infolist() if not i.filename.endswith("/")][:cfg.zip_max_entries]
            entries = []
            for info in infos:
                # Guard against a decompression-bomb entry (tiny compressed
                # size, huge uncompressed size) before reading it into memory.
                if info.file_size > max_entry_bytes:
                    entries.append((info.filename, None))
                    continue
                entries.append((info.filename, zf.read(info)))
            return entries

    try:
        entries = await asyncio.to_thread(_read_entries)
    except Exception as e:
        raise RuntimeError(f"Failed to open {filename} as a zip archive: {e}") from e

    if not entries:
        raise ValueError(f"{filename} is an empty zip archive")

    for entry_name, entry_content in entries:
        try:
            if entry_content is None:
                raise ValueError(f"exceeds {cfg.max_file_size_mb} MB per-file limit, skipped")
            entry_text, _ = await parse_file(entry_content, entry_name)
        except Exception as e:
            logger.warning(f"[PARSE][ZIP] {filename} -> could not parse entry {entry_name}: {e}")
            entry_text = f"(could not extract text from this file: {e})"

        start = len(full_text)
        section = f"--- File: {entry_name} ---\n{entry_text}\n"
        full_text += section
        page_spans.append({
            "page_number": len(page_spans) + 1,
            "start": start,
            "end": start + len(section),
            "confidence": None,
        })

    return full_text, page_spans


async def parse_file(content: bytes, filename: str) -> tuple[str, list[dict]]:
    ext = os.path.splitext(filename)[1].lower()
    size_kb = len(content) / 1024
    logger.info(f"[PARSE] Starting - file={filename}, size={size_kb:.1f} KB, type={ext}")
    t0 = time.time()

    if ext == ".zip":
        full_text, page_spans = await _parse_zip(content, filename)
        logger.info(f"[PARSE] Done (zip) - entries={len(page_spans)}, chars={len(full_text)}, time={time.time()-t0:.2f}s")
        return full_text, page_spans

    if ext in PLAIN_TEXT_EXTENSIONS:
        text = content.decode("utf-8", errors="ignore")
        logger.info(f"[PARSE] Done (plain text) - chars={len(text)}, time={time.time()-t0:.2f}s")
        page_spans = [{"page_number": 1, "start": 0, "end": len(text), "confidence": None}]
        return text, page_spans

    def _write_tmp() -> str:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            return tmp.name

    tmp_path = await asyncio.to_thread(_write_tmp)
    try:
        if ext in (".xlsx", ".xls", ".csv", ".ppt", ".pptx"):
            # LiteParse is a PDF/DOCX page-layout parser with no spreadsheet
            # or slide-deck support - it would fail on every single one of
            # these, so go straight to Docling (which has native xlsx/xls/
            # csv AND ppt/pptx backends) instead of paying for a doomed
            # first attempt.
            try:
                full_text, page_spans = await asyncio.to_thread(_parse_with_docling, tmp_path)
            except Exception as docling_error:
                logger.error(f"[PARSE] Docling failed on {filename} ({docling_error})")
                raise RuntimeError(f"Failed to parse {filename}: {docling_error}") from docling_error
            logger.info(f"[PARSE] Done (Docling) - pages={len(page_spans)}, chars={len(full_text)}, time={time.time()-t0:.2f}s")
            return full_text, page_spans

        # Run in thread pool - both parsers are CPU-bound and would block the event loop
        try:
            full_text, page_spans = await asyncio.to_thread(_parse_with_liteparse, tmp_path)
            logger.info(f"[PARSE] Done (LiteParse) - pages={len(page_spans)}, chars={len(full_text)}, time={time.time()-t0:.2f}s")
            return full_text, page_spans
        except Exception as liteparse_error:
            logger.warning(f"[PARSE] LiteParse failed ({liteparse_error}) - falling back to Docling")

        try:
            full_text, page_spans = await asyncio.to_thread(_parse_with_docling, tmp_path)
        except Exception as docling_error:
            logger.error(f"[PARSE] Docling fallback also failed ({docling_error})")
            raise RuntimeError(
                f"Failed to parse {filename}: LiteParse and Docling both failed "
                f"(Docling error: {docling_error})"
            ) from docling_error

        logger.info(f"[PARSE] Done (Docling fallback) - pages={len(page_spans)}, chars={len(full_text)}, time={time.time()-t0:.2f}s")
        return full_text, page_spans
    finally:
        os.unlink(tmp_path)


async def scrape_url(url: str) -> str:
    logger.info(f"[SCRAPE] Starting - url={url}")
    t0 = time.time()
    try:
        from crawl4ai import AsyncWebCrawler
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
            if result.markdown:
                logger.info(f"[SCRAPE] Done (Crawl4AI) - chars={len(result.markdown)}, time={time.time()-t0:.2f}s")
                return result.markdown
            raise ValueError("Empty content from Crawl4AI")
    except Exception as e:
        logger.warning(f"[SCRAPE] Crawl4AI failed ({e}) - falling back to Playwright")
        return await _scrape_with_playwright(url, t0)


async def _scrape_with_playwright(url: str, t0: float = None) -> str:
    if t0 is None:
        t0 = time.time()
    logger.info(f"[SCRAPE] Playwright launching - url={url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page    = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        content = await page.inner_text("body")
        await browser.close()
    logger.info(f"[SCRAPE] Done (Playwright) - chars={len(content)}, time={time.time()-t0:.2f}s")
    return content


async def fetch_api(url: str, headers: dict[str, str] | None = None) -> str:
    logger.info(f"[API] Fetching - url={url}, auth_headers={'yes' if headers else 'none'}")
    t0 = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=headers or {})
        response.raise_for_status()
        data = response.json()
    text = _flatten_json(data)
    logger.info(f"[API] Done - status={response.status_code}, chars={len(text)}, time={time.time()-t0:.2f}s")
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
