FROM python:3.13-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create non-root user matching HF Spaces UID
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install dependencies (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Install Playwright to a fixed path so non-root user can access it
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright-browsers
RUN uv run playwright install --with-deps chromium

# Pre-download Docling's layout/table models into the image (cached layer -
# only reruns when dependencies change) so the first request that falls back
# to Docling from LiteParse doesn't pay the multi-minute first-run download.
ENV HF_HOME=/app/.cache/huggingface
COPY scripts/warmup_docling.py ./scripts/warmup_docling.py
RUN uv run python scripts/warmup_docling.py

# Copy application code
COPY . .

# Create runtime directories and hand ownership to appuser
RUN mkdir -p chroma_data logs && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
