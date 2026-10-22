from pydantic import BaseModel
from typing import Optional, Literal
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
    headers: Optional[dict[str, str]] = None
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


class ConfirmRequest(BaseModel):
    confirm_token: str
    action: Literal["reuse", "reprocess"]


class ConfirmResponse(BaseModel):
    status: str
    message: str


class KnowledgeBaseCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    # None/[] = unrestricted (every signed-in user can see it) - omitting
    # this field entirely keeps a knowledge base's historical default.
    allowed_roles: Optional[list[str]] = None


class KnowledgeBaseUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["active", "inactive"]] = None
    # Distinguishing "not sent" (don't touch) from "sent as []" (clear the
    # restriction, make it unrestricted again) needs a sentinel, not plain
    # None - see admin.py's update_knowledge_base for how this is read.
    allowed_roles: Optional[list[str]] = None
    clear_allowed_roles: bool = False


class KnowledgeBaseOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    document_count: int = 0
    allowed_roles: Optional[list[str]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class KnowledgeBasePublicOut(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None


class UserRolesUpdateRequest(BaseModel):
    roles: list[str]


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    roles: list[str] = []


class KnowledgeBaseDocumentOut(BaseModel):
    id: UUID
    filename: str
    file_size: int
    chunks_stored: Optional[int] = None
    status: str
    uploaded_at: Optional[str] = None
