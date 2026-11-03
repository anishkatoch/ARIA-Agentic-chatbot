FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium

COPY . ./ingestion_pipeline

EXPOSE 8080
CMD ["uvicorn", "ingestion_pipeline.service:app", "--host", "0.0.0.0", "--port", "8080"]
