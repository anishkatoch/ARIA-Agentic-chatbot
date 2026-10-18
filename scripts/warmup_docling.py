"""Run at Docker build time to pre-download Docling's layout/table models into
the image, so the first real request that falls back to Docling isn't the one
paying the multi-minute first-run download cost.
"""
import os
import tempfile

from docling.document_converter import DocumentConverter

_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 200 200]/Parent 2 0 R"
    b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
    b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"5 0 obj<</Length 30>>stream\nBT /F1 12 Tf 10 100 Td (warmup) Tj ET\nendstream\nendobj\n"
    b"trailer<</Size 6/Root 1 0 R>>\n%%EOF"
)


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(_MINIMAL_PDF)
        tmp_path = tmp.name
    try:
        DocumentConverter().convert(tmp_path)
    finally:
        os.unlink(tmp_path)
    print("[DOCLING] Models pre-warmed")


if __name__ == "__main__":
    main()
