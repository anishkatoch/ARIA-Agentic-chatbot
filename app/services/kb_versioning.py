"""Duplicate/version detection for Knowledge Base document uploads.

See planning/PLAN-document-versioning.md. Kept separate from
app.routers.admin so the decision logic (not the DB/HTTP plumbing around it)
can be unit tested with a plain mock session.
"""
import logging
import uuid

logger = logging.getLogger(__name__)

# Returned by decide_upload_action().action
NEW = "new"
DUPLICATE = "duplicate"
VERSION = "version"


class UploadDecision:
    def __init__(self, action: str, existing=None):
        self.action = action      # NEW | DUPLICATE | VERSION
        self.existing = existing  # the current active KnowledgeBaseDocument row, if any


def find_active_document(db, knowledge_base_id, filename: str, item_id: str | None):
    """The current non-superseded row this upload matches against - looked
    up by item_id when supplied (filename-independent, handles renames), by
    filename otherwise."""
    from app.models.db import KnowledgeBaseDocument

    q = db.query(KnowledgeBaseDocument).filter(
        KnowledgeBaseDocument.knowledge_base_id == knowledge_base_id,
        KnowledgeBaseDocument.is_superseded == False,  # noqa: E712
    )
    if item_id:
        q = q.filter(KnowledgeBaseDocument.item_id == item_id)
    else:
        q = q.filter(
            KnowledgeBaseDocument.item_id.is_(None),
            KnowledgeBaseDocument.filename == filename,
        )
    return q.first()


def decide_upload_action(db, knowledge_base_id, filename: str, item_id: str | None,
                          content_hash: str) -> UploadDecision:
    existing = find_active_document(db, knowledge_base_id, filename, item_id)
    if existing is None:
        return UploadDecision(NEW)
    if existing.content_hash == content_hash:
        return UploadDecision(DUPLICATE, existing)
    return UploadDecision(VERSION, existing)


def start_new_version(db, knowledge_base_id, filename: str, file_size: int,
                       item_id: str | None, content_hash: str, previous=None):
    """Supersedes `previous` (if given) and inserts the next version's row,
    both in the caller's existing transaction (commit is the caller's
    responsibility, same pattern admin.py already uses for KnowledgeBaseDocument
    writes). Returns the new row (flushed, not yet committed)."""
    from app.models.db import KnowledgeBaseDocument

    if previous is not None:
        previous.is_superseded = True

    new_version = (previous.version + 1) if previous is not None else 1
    doc = KnowledgeBaseDocument(
        id=uuid.uuid4(),
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        file_size=file_size,
        status="pending",
        item_id=item_id,
        content_hash=content_hash,
        version=new_version,
        previous_version_id=previous.id if previous is not None else None,
        is_superseded=False,
    )
    db.add(doc)
    db.flush()
    return doc
