from pydantic import BaseModel, HttpUrl
from typing import Optional
from uuid import UUID


class UploadResponse(BaseModel):
    session_id: UUID
    files_processed: int
    message: str


class URLIngestRequest(BaseModel):
    url: str
    session_id: Optional[UUID] = None


class APIIngestRequest(BaseModel):
    url: str
    headers: Optional[dict[str, str]] = None  # optional — leave empty for public APIs
    session_id: Optional[UUID] = None


class IngestResponse(BaseModel):
    session_id: UUID
    message: str


class ChatRequest(BaseModel):
    session_id: UUID
    question: str


class ChatResponse(BaseModel):
    session_id: UUID
    answer: str
    elapsed_ms: int
    citations: list[dict] = []
