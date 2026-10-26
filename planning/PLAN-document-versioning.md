# Document Versioning & Diff - Plan

Status: proposed, not yet approved for implementation.

## Problem

Knowledge Base uploads (`KnowledgeBaseDocument`) have no hash, no version field, and no
history. Re-uploading a file with the same name today deletes the old chunks and replaces
them, with no record of when the change happened or what changed.

The personal-session `Document` model already has partial duplicate detection
(`app/services/dedup.py`: SHA256 `content_hash`, TF-IDF near-dup check, same-filename-
different-hash rule), but this does not exist for KB documents.

Goal: detect duplicate/changed uploads in a KB, keep a version history per filename, and
let a user diff two versions (or any two documents) to see what changed.

## Scope decision

This phase covers Knowledge Base uploads only. Personal-session uploads are ephemeral per
chat and are out of scope for now.

## Schema changes

Add to `KnowledgeBaseDocument` (mirrors what `Document` already does for sessions):

- `item_id` (str, nullable, indexed) - an optional, user-supplied identifier. When given on
  upload, it is the explicit signal "this upload is a new version of the same logical
  document," independent of filename. This is what lets a renamed file still be recognized
  as an update rather than a brand-new document (see "Known limitations" below - this is the
  fix for that gap).
- `content_hash` (str) - SHA256 of raw uploaded bytes, same function as
  `app/services/dedup.py` `sha256()`.
- `version` (int, starts at 1).
- `previous_version_id` (self FK, nullable) - links to the prior version's row.
- `is_superseded` (bool, default `false`) - a new, separate column. Investigated and
  decided: `status` on `KnowledgeBaseDocument` is already a strict whitelist used for
  retrieval (`app/routers/knowledge_base.py:71` filters `status == "complete"` to decide
  which documents are attachable/retrievable) and for dashboard pending-counts
  (`app/routers/admin.py:72,91`). Overloading it with `active`/`superseded` values would
  require touching that retrieval filter and risks silently dropping documents from
  retrieval if missed. Keeping `status` untouched (`pending/complete/failed`, pipeline
  state only) and adding `is_superseded` as an orthogonal flag is safer and needs no change
  to existing filters - existing code that doesn't know about `is_superseded` keeps working
  exactly as today.

Add two partial unique constraints (both needed, because the matching key differs depending
on whether `item_id` was supplied):

```sql
UNIQUE (knowledge_base_id, item_id) WHERE is_superseded = false AND item_id IS NOT NULL
UNIQUE (knowledge_base_id, filename) WHERE is_superseded = false AND item_id IS NULL
```

This prevents a concurrent-upload race where two near-simultaneous uploads matching the same
key (either `item_id` or `filename`, depending on path) both try to create an "active" row
at once. The insert of the new active row and the supersede of the old row happen in one DB
transaction; a second concurrent uploader's insert fails fast against the constraint instead
of silently producing two active rows.

## Upload flow

The request accepts an optional `item_id` field (any client-supplied string). It changes
which key is used to decide "is this a new document or a new version of an existing one."

**If `item_id` is supplied** (explicit versioning, filename-independent):

1. Compute `content_hash` of the uploaded file.
2. Look up the current non-superseded row for `(knowledge_base_id, item_id)` - filename is
   not part of this lookup at all.
3. If none exists: insert as `version = 1`, `is_superseded = false`, `item_id = <given>`,
   `filename = <whatever was uploaded>`. Parse, chunk, and embed fully.
4. If one exists with the same hash: reject as an exact duplicate (same file re-sent under
   the same item_id).
5. If one exists with a different hash: automatically, no confirmation prompt, in a single
   transaction - set the existing row `is_superseded = true`, insert a new row
   `version = existing.version + 1`, `previous_version_id = existing.id`,
   `is_superseded = false`, `item_id = <same given>`, `filename = <new upload's filename,
   which may differ from the previous version's filename>`. This is exactly what lets a
   renamed file be recognized as an update: the filename is free to change version to
   version, because `item_id` - not filename - is the identity key.

**If `item_id` is omitted** (today's filename-based fallback, unchanged from the earlier
plan):

6. Look up the current non-superseded row for `(knowledge_base_id, filename)` instead -
   same logic as before (new / exact-duplicate-reject / auto-version-bump), with
   `item_id = NULL` on all these rows.

**Either path, once version-insert decided:**

7. Once the new version's vectors are confirmed written, delete the old version's vectors
   from the vector store immediately (same mechanism used today for KB document delete:
   `KnowledgeBaseDocument.vector_ids` targeted delete). The vector store only ever holds
   chunks for the current (non-superseded) version of a document - the existing
   `knowledge_base.py:71` retrieval filter (`status == "complete"`) is untouched and needs no
   change, since superseded rows' vectors are gone from the store, not merely flagged.
8. Store the full extracted text (or enough of it to reconstruct a diff - e.g. the already-
   parsed text before chunking) against the new version's row, for use by the diff feature
   only. This text is never re-embedded or inserted into the vector store.

## Diff feature

- Diff is a text diff over previously-extracted document text, not a vector/embedding
  distance. Embedding similarity tells you how alike two things are, not what specifically
  changed.
- Before diffing, normalize both texts: collapse repeated whitespace/newlines, strip
  repeated header/footer/page-number lines (detectable heuristically as lines that repeat
  verbatim across many pages). This avoids spurious diffs caused by PDF/DOCX extraction
  artifacts (re-wrapped lines, page breaks) that aren't real content changes.
- Run the normalized texts through `difflib` (`unified_diff` or `ndiff`) to produce a
  human-readable diff.
- Works for two versions of the same filename (version chain) and equally for any two
  arbitrary document IDs the user picks - same diff engine either way.

### Example

```
Document: "Q3_Pricing.pdf"
  v1 uploaded 2026-06-10 - hash abc123
  v2 uploaded 2026-10-20 - hash def456

Diff v1 -> v2:
- Enterprise tier: $499/mo
+ Enterprise tier: $599/mo
  (unchanged: intro paragraph, Starter tier section, footer)
```

## New endpoints

- `GET /kb/{kb_id}/documents/{filename}/versions` - list versions for a filename (used when
  no `item_id` was given on upload).
- `GET /kb/{kb_id}/items/{item_id}/versions` - list versions by `item_id` instead (used when
  uploads were tagged with one; returns versions even if the filename changed between them).
- `GET /kb/documents/{doc_id}/diff?against={other_doc_id}` - unified diff payload between
  any two document rows (version chain or arbitrary pair).
- Upload response: when a duplicate filename with different content is detected, the server
  proceeds automatically (no confirm prompt) by saving it as the next version, and the
  upload response simply reports `{"versioned": true, "new_version": 2, "previous_version":
  1}` so the UI can show a toast/notice after the fact. No `ConfirmRequest` gate is added
  for this path (that pattern remains only for the existing session-upload exact-duplicate
  case, which is unrelated).

## Resolved: status field decision

Investigated `app/routers/knowledge_base.py:71` and `app/routers/admin.py:72,91`:
`KnowledgeBaseDocument.status` is a strict equality whitelist (`== "complete"`) gating
retrieval/attach, plus a pending-count aggregate. Overloading it with `active/superseded`
would require a coordinated change to that filter and risks silently dropping documents
from retrieval if missed. Decision: leave `status` untouched (`pending/complete/failed`,
pipeline state only) and add the separate `is_superseded` boolean column described above.
No existing filter needs to change.

## Build sequence (step by step)

### Phase 1 - Schema

1. Add an Alembic migration (`alembic revision`) adding to `KnowledgeBaseDocument`:
   `item_id` (String, nullable, indexed), `content_hash` (String, nullable, indexed),
   `version` (Integer, default 1, not null), `previous_version_id` (UUID, FK to
   `knowledge_base_documents.id`, nullable), `is_superseded` (Boolean, default false,
   not null).
2. In the same migration, backfill existing rows: `version = 1`, `is_superseded = false`,
   `content_hash = NULL`, `item_id = NULL` (unknown for already-ingested files; first
   re-upload will populate what it can going forward - no backfill hashing of old files in
   this phase).
3. Add both partial unique indexes:
   `CREATE UNIQUE INDEX ... ON knowledge_base_documents (knowledge_base_id, item_id)
   WHERE is_superseded = false AND item_id IS NOT NULL;` and
   `CREATE UNIQUE INDEX ... ON knowledge_base_documents (knowledge_base_id, filename)
   WHERE is_superseded = false AND item_id IS NULL;`
4. Run the migration locally against the dev DB and confirm existing KB listing/retrieval
   endpoints still work unchanged (regression check, not new behavior yet).

### Phase 2 - Hash + duplicate detection on upload

5. Add an optional `item_id` field to the KB upload request (form field or query param,
   client-supplied, any non-empty string). If omitted, behavior falls back to the
   filename-based path (steps 9-12 below).
6. In the KB upload handler (`app/routers/admin.py`, the upload endpoint around lines
   309-371), compute `content_hash = sha256(file_bytes)` reusing
   `app/services/dedup.py`'s existing `sha256()` function (don't duplicate the function).
7. If `item_id` was supplied: query for the current non-superseded row with
   `(knowledge_base_id, item_id)` - filename is not part of this lookup.
8. If found (by item_id) and hash matches: return a duplicate-rejection response (409 or
   similar) with the existing row's `uploaded_at`.
9. If found (by item_id) and hash differs: proceed to Phase 3 (version transition), new
   row's `filename` set to whatever this upload's filename is (may differ from the previous
   version's filename - this is the rename case, now handled).
10. If not found (by item_id): insert as `version = 1`, storing the given `item_id`.
11. If `item_id` was NOT supplied: fall back to the original filename-based lookup -
    query for the current non-superseded row with `(knowledge_base_id, filename)`, same
    found/same-hash-reject / different-hash-version-bump / not-found-insert logic as before,
    with `item_id` left `NULL` on these rows.

### Phase 3 - Version transition (replace-as-new-version)

10. Wrap the supersede-old + insert-new steps in one DB transaction:
    - set old row `is_superseded = true`
    - insert new row with `version = old.version + 1`, `previous_version_id = old.id`,
      `is_superseded = false`, `status = "pending"` (goes through the normal
      parse/chunk/embed pipeline exactly as a fresh upload today)
11. Let the existing ingestion pipeline (`ingestion.py` parse -> `rag.py` chunk ->
    `vector_store.py` embed) run unchanged on the new row.
12. On successful completion (existing `_mark(..., "complete")` path, `admin.py:371`), also
    store the new row's extracted full text (pre-chunking) for later diffing - either a new
    `extracted_text` column, or a reference to where it can be cheaply re-derived from
    stored chunks. Recommend a new nullable `extracted_text` column (Text) for simplicity,
    since re-deriving from chunk order is fragile.
13. After the new row reaches `status = "complete"`, delete the old row's vectors from the
    vector store using its existing `vector_ids` (same delete path used for manual document
    delete today). Do this only after the new version's embed succeeds, so a failed
    re-embed doesn't leave the KB with zero vectors for that filename.
14. Handle the failure case: if the new version's embedding fails (`status = "failed"`), do
    not delete the old version's vectors, and do not flip old row's `is_superseded` back -
    instead surface the failure and let the old (still superseded, vectors intact) row keep
    serving retrieval until the user retries the upload. (Flag for your review: this means
    a failed re-upload leaves `is_superseded = true` with vectors still present on that row
    temporarily - acceptable since retrieval doesn't filter on `is_superseded` directly, it
    relies on the vector store's actual contents.)

### Phase 4 - Upload response (no confirmation prompt)

15. When step 8 triggers (same filename, different hash), do not ask the user anything -
    proceed straight into Phase 3 (version transition) automatically.
16. The upload response includes versioning info so the UI can show an after-the-fact
    notice, e.g. `{"versioned": true, "new_version": 2, "previous_version": 1,
    "previous_uploaded_at": "..."}`, instead of a blocking confirm step.
17. "Keep both as separate documents" is dropped from this phase since there is no
    confirm gate to offer it from - every same-filename-different-hash upload becomes the
    next version automatically. (If you want a way to upload an unrelated file without it
    being treated as a new version, the workaround is simply to upload it under a different
    filename - no special handling needed for that.)
18. Wire the frontend to show a brief toast/notice after upload when `versioned: true` comes
    back, so the user knows a new version was created rather than discovering it later.

### Phase 5 - Version history + diff endpoints

19. `GET /kb/{kb_id}/documents/{filename}/versions`: query all rows (superseded and not)
    for that `(knowledge_base_id, filename)`, ordered by `version`, returning
    `version, uploaded_at, content_hash, file_size, is_superseded`.
20. Add a text-normalization helper (new small function, e.g. in `app/services/dedup.py` or
    a new `app/services/diff.py`): collapse whitespace, strip lines that repeat verbatim
    across many pages (simple frequency-based heuristic over line-split text).
21. `GET /kb/documents/{doc_id}/diff?against={other_doc_id}`: load both rows'
    `extracted_text`, normalize both, run `difflib.unified_diff`, return the diff lines
    (plus basic stats: lines added/removed) as JSON.
22. Authorization check on both endpoints: confirm the requesting user has access to the KB
    those documents belong to (reuse whatever role/permission check already gates KB access
    in `app/routers/knowledge_base.py`).

### Phase 6 - Frontend

23. Version history view: on a KB document's detail/list row, show version count and a
    "history" action opening the versions list (endpoint from step 19).
24. Diff view: given two versions (or any two picked documents), call the diff endpoint and
    render as a simple +/- colored line view.
25. Upload notice: when an upload response comes back with `versioned: true` (Phase 4, step
    16), show a brief toast/notice ("Saved as version 2, replacing the version from
    <date>") - no blocking modal, no choice to make, since the version-bump already happened
    automatically server-side by the time the response is returned.

### Phase 7 - Tests

26. Unit tests: hash computation reuse, version-transition transaction (old row superseded
    + new row created atomically), unique-constraint race (simulate two concurrent inserts
    for the same filename, confirm one fails cleanly and is retried as a version-bump).
27. Integration test: upload v1, upload v2 (same filename, different content), assert
    retrieval only returns v2's chunks, assert `/versions` lists both, assert `/diff` shows
    the expected added/removed lines.
28. Regression test: confirm unrelated KBs/documents and existing session-upload dedup flow
    are unaffected.

## Full A-to-Z walkthrough (what happens, in order, on disk and in the DB)

This section traces one concrete file going through the system twice (first upload, then
a changed re-upload), step by step, naming exactly which table/column/vector-store call is
touched at each point. No code yet - this is the logic that the code in Phases 1-7 above
will implement.

### A. First upload of "Q3_Pricing.pdf" (brand new filename in this KB)

1. User uploads the file through the KB upload endpoint
   (`app/routers/admin.py`, upload handler around line 309+).
2. Server reads the raw bytes into memory, computes `content_hash = sha256(bytes)`
   (reusing `app/services/dedup.py`'s `sha256()`).
3. Server queries `KnowledgeBaseDocument` for
   `(knowledge_base_id = X, filename = "q3_pricing.pdf", is_superseded = false)`.
   Nothing found -> this is a new document.
4. Server inserts one row into `knowledge_base_documents`:
   `version=1, is_superseded=false, content_hash=<hash>, status="pending"`,
   `chunks_stored=NULL, vector_ids=NULL` (same as today's insert, with the two new fields).
5. Parsing: `ingestion.py: parse_file()` extracts raw text from the PDF
   (LiteParse/Docling depending on file type). This full extracted text is what gets saved
   to the new `extracted_text` column on this row (step added for the diff feature - it is
   not embedded, just stored as plain text for later comparison).
6. Chunking: `rag.py: chunk_text_with_offsets()` splits that text into overlapping chunks
   with page/offset metadata.
7. Embedding: `vector_store.py: get_embeddings()` embeds each chunk; chunks are written into
   the KB's Chroma/pgvector collection with metadata `source=filename`, `chunk_index`, etc.
   (unchanged from today). The returned vector ids are collected.
8. Server updates the row: `status="complete"`, `chunks_stored=<n>`,
   `vector_ids="<comma-separated ids>"`.
9. Done. Retrieval for this KB now includes this document's chunks, because the existing
   chat retrieval query only ever reads from the vector store directly - it does not care
   about `is_superseded` at all, since the vector store only contains active content
   (explained in section C below).

### B. Chat uses the document (nothing new here, unchanged)

10. When a user chats against this KB, the existing RAG flow
    (`app/services/rag.py`) queries the vector store for the KB's `session_id`/collection,
    gets back the top-k matching chunks (each tagged with `source=filename`), and builds the
    answer with citations pointing at `filename` (and page number, if present).
11. Nothing about versioning changes this step - the chat layer has no idea versions exist.
    It just always sees whatever is currently in the vector store, which by construction
    (step 19 below) is always the latest version's chunks only.

### C. Six months later: an updated "Q3_Pricing.pdf" is uploaded (same filename, changed content)

12. Same as steps 1-2: bytes read, `content_hash` computed - this time it differs from the
    hash stored on the existing active row.
13. Server queries for the active row, finds the old one
    (`version=1, is_superseded=false, content_hash=abc123`).
14. Hash differs -> this is a content change, not a duplicate. No confirmation is asked -
    the server proceeds automatically to create the next version.
15. **Single DB transaction**:
    - old row (`version=1`) -> `is_superseded = true` (its `vector_ids` are NOT deleted yet)
    - new row inserted: `version=2, is_superseded=false, content_hash=def456,
      previous_version_id=<old row id>, status="pending"`
17. New row goes through the exact same parse -> chunk -> embed pipeline as step 5-7 above.
    This is a **full fresh re-embed** - nothing from version 1's chunks/vectors is reused,
    patched, or diffed at the embedding level. The new PDF is treated exactly like a brand
    new file for embedding purposes.
18. On success: new row gets `status="complete"`, `chunks_stored`, `vector_ids` populated
    (same as step 8).
19. **Only now**, with the new version's vectors confirmed written and `status="complete"`,
    the server deletes the old row's vectors from the vector store using its stored
    `vector_ids` (the same delete mechanism already used for manual document deletion
    today). This is the moment the vector store stops containing version 1's content at
    all - there is a short window where both versions' vectors technically coexist in the
    store (between step 17 finishing and step 19 running), but retrieval ranking would
    naturally favor the new content anyway since it's the same filename/topic; this window
    is seconds long and single-document-shaped, not a broader data-integrity issue.
20. If step 17 fails (embedding error): new row -> `status="failed"`. Old row stays
    `is_superseded=true` but its vectors are **not deleted** (step 19 never runs), so chat
    retrieval keeps working off the old content until the user retries the upload. This is
    the one deliberately "slightly odd but safe" state in the design: `is_superseded=true`
    with live vectors, kept only as a fallback until a successful retry.

### D. Chat after the update (still nothing new here)

21. Exactly like steps 10-11 - chat just queries the vector store, which now only contains
    version 2's chunks (version 1's were deleted in step 19). The chat layer never
    references `version`, `is_superseded`, or any versioning concept. Versioning is
    entirely a DB/history-and-diff concern layered on top; it does not change how answers
    are generated or cited.

### E. "What changed" - version history and diff (the new user-facing feature)

22. User opens the document's "history" view -> calls
    `GET /kb/{kb_id}/documents/q3_pricing.pdf/versions`.
23. Server queries all rows (both `is_superseded` values) for that `(kb_id, filename)`,
    ordered by `version`, returns:
    ```json
    [
      {"version": 1, "uploaded_at": "2026-06-10T...", "content_hash": "abc123",
       "is_superseded": true},
      {"version": 2, "uploaded_at": "2026-10-20T...", "content_hash": "def456",
       "is_superseded": false}
    ]
    ```
24. User clicks "diff v1 vs v2" -> calls
    `GET /kb/documents/{v2_row_id}/diff?against={v1_row_id}`.
25. Server loads `extracted_text` from both rows (the plain text saved in step 5/17, not
    re-parsed from the PDF again, and not derived from embeddings).
26. Server normalizes both texts (collapse whitespace, strip repeated header/footer/page-
    number lines) - this step exists purely to stop PDF-extraction noise (re-wrapped lines,
    page breaks) from showing up as fake changes.
27. Server runs `difflib.unified_diff(normalized_v1_lines, normalized_v2_lines)`, returns
    the diff as structured JSON: list of `{type: "added"|"removed"|"unchanged", text: "..."}`
    lines, plus a summary count (`lines_added`, `lines_removed`).
28. Frontend renders this as a classic red/green diff view. This is purely informational -
    it never touches the vector store, never re-embeds anything, and has no effect on chat
    answers. It exists only so a human can see what changed between versions.

### F. Using item_id to handle a renamed file

29. User uploads "Pricing_2026.pdf" with `item_id="pricing-sheet"`. No existing row for
    `(kb_id, item_id="pricing-sheet")` -> inserted as `version=1`, `item_id="pricing-sheet"`,
    `filename="pricing_2026.pdf"`.
30. Months later, the same logical document is renamed and updated: user uploads
    "Pricing_Updated_Nov2026.pdf" with the same `item_id="pricing-sheet"`.
31. Server looks up by `item_id`, finds the version-1 row, compares hash - different ->
    version-bump proceeds exactly as in section C, except the new row's `filename` is
    `"pricing_updated_nov2026.pdf"`, different from version 1's filename. This is fine and
    expected: `item_id` is the identity key, filename is just descriptive metadata that is
    allowed to change per version.
32. `GET /kb/{kb_id}/items/pricing-sheet/versions` returns both versions correctly, including
    across the filename change.

### Summary answers to your specific questions

- **How it will load**: upload endpoint reads file bytes into memory once; parsing
  (`parse_file`) extracts text from those bytes; nothing is "loaded" from a previous
  version during upload - every upload is processed standalone.
- **How it will save**: one new `KnowledgeBaseDocument` row per version (never mutated in
  place for content - only `is_superseded` flips), plus one `extracted_text` text blob per
  row for diffing, plus the usual vector-store writes.
- **How chat will work**: completely unchanged. Chat only ever reads from the vector store,
  which is kept pruned to the current version's chunks only, via the delete-old-vectors step
  (C.19). Chat has zero awareness of versions.
- **How it will tell you what changed**: a separate, on-demand diff endpoint that compares
  two rows' stored plain text with `difflib`, after whitespace/header normalization. Not
  AI-generated, not embedding-based - a literal text diff.
- **Do we do fresh embedding**: yes, always, in full, every version. No partial re-embed, no
  patching, no chunk reuse across versions. Old vectors are deleted only after the new
  version's embedding succeeds.

## Phase 8 - Asking for the diff/change in chat itself

Goal: a user can type things like "what's the difference between abc.pdf and xyz.pdf",
"what changed in food_rights.pdf between v1 and v2", "what's new in the latest version of
the pricing sheet", or "when was it updated" - and get a plain-language answer ("previously
it said X, now it says Y") instead of having to open a separate history/diff UI. This must
work for (a) two arbitrary documents the user names, and (b) any two versions of a document
that has gone through the versioning flow in Phases 1-7.

This is new logic layered on the existing chat pipeline, not a replacement for it. Grounded
in how that pipeline actually works today:

- Every chat message goes through one entry point: `POST /chat/` (`app/routers/chat.py:42`)
  which always calls `answer_question()` in `app/services/rag.py:1494`. There is no
  per-intent dispatch in the router itself.
- `answer_question()` already has a pattern of early-exit checks before the main
  simple/advanced LangGraph pipelines run: `guard_query()` (rag.py:1419, blocks prompt
  injection) and `check_intent()` (rag.py:759, Thinking Mode only, routes small talk vs
  retrieval). A "compare documents" check fits in the same place, as a new early-exit.
- Filename resolution from free text already exists: `_resolve_target_sources()`
  (rag.py:565-706) matches names mentioned in a question against the KB's actual filenames
  (via `list_session_sources()`, rag.py:550), and it **already strips version-style suffixes
  like `-v2`** when matching - i.e. a lot of the "which file, which version" parsing this
  feature needs is already partly built, and should be extended rather than duplicated.
- Full document text for a given source can be pulled via `_fetch_broad_context()`
  (rag.py:718-754), though it's capped at `limit=40` chunks - for a true diff this plan
  reads from the stored `extracted_text` column (Phase 1-7) instead, which is the
  uncapped, pre-chunking full text saved specifically for this purpose.
- Thinking Mode already runs as a tool-calling agent (`_react_tools()`, rag.py:1187+, e.g.
  `vector_search`, `read_source`, `clarify`) - a new `compare_documents` tool fits there
  using the same `@tool` pattern.

### Step-by-step logic

1. **Detect compare intent.** Add a check in `answer_question()` right beside
   `guard_query`/`check_intent` (rag.py ~1519-1542): a question matches "compare intent" if
   it contains comparison/change language ("difference between", "what changed", "compare",
   "updated version", "what's new", "previous version") **and** mentions one or two
   document references (by filename, or a name close enough to one surfaced by
   `list_session_sources()`), optionally with version words ("v1", "v2", "latest",
   "previous", "original").
2. **Resolve the reference(s) to actual rows**, not just filenames - extend
   `_resolve_target_sources()`'s matching so it returns, for each matched document name:
   - if a version word is present ("v1", "version 2", "latest", "previous") -> resolve to
     the specific `KnowledgeBaseDocument` row at that version (via the `version`/`item_id`/
     `filename` columns from Phase 1).
   - if no version word is present but exactly one document name is mentioned and it has
     more than one version on file -> default to **latest vs. immediately previous**
     (`previous_version_id` chain).
   - if two distinct document names are mentioned (e.g. "abc.pdf" and "xyz.pdf", unrelated
     files, not versions of each other) -> resolve each to its current active row directly -
     this is the arbitrary-pair case, same resolution Phase 5's `/diff` endpoint uses.
3. **Ambiguous or unresolved cases** (zero matches, more than 2 candidates, or a single
   document with no version history) -> do not guess silently. Either fall back to the
   normal RAG answer pipeline (treat it as a regular question, since it clearly isn't
   resolvable as a comparison), or, in Thinking Mode, let the agent use the existing
   `clarify` tool to ask the user which two versions/files they mean.
4. Once exactly two rows are resolved, load their `extracted_text` (Phase 1's new column -
   not re-parsed from the file, not derived from embeddings) and run the same
   normalize-then-`difflib` step from Phase 5 (strip repeated headers/page numbers, collapse
   whitespace, unified diff).
5. **Do not return the raw unified diff as the chat answer.** Feed the two normalized texts
   (or the diff itself, if the documents are long) to the LLM via the existing
   `llm_invoke()` helper (rag.py:182) with a dedicated prompt instructing it to produce a
   plain-language summary in the "previously X, now Y" style the user asked for - e.g.:
   ```
   System: You are summarizing the difference between two versions of a document for a
   user. Be concrete: name what was removed, what was added, and what was reworded. Use the
   format "Previously: ... / Now: ..." for each change. Do not invent changes not present in
   the diff.
   User: <normalized old text> ||| <normalized new text> (or the unified diff, if shorter)
   ```
6. Include the factual metadata the user also asked about alongside the summary when
   relevant (upload timestamps, version numbers) - these come straight from the resolved
   rows' `uploaded_at`/`version` columns, not from the LLM, so they're always accurate (e.g.
   "Version 2 was uploaded on 2026-10-20, replacing the version from 2026-06-10.").
7. Return this as the normal chat answer (same `ChatResponse` shape used today), with
   citations pointing at both resolved documents/versions, following the existing
   `_build_citations()` pattern (rag.py:1037) so the UI doesn't need special-case rendering.

### Thinking Mode variant

8. Instead of (or in addition to) the early-exit detector in step 1, add a `compare_documents`
   tool to `_react_tools()` (rag.py:1187+, alongside `read_source`) that the agent can choose
   to call when its own reasoning decides the question is a comparison. The tool takes two
   resolved document/version identifiers (reusing the same resolver from step 2) and returns
   the diff for the agent to summarize in its final answer - this fits Thinking Mode's
   existing agentic pattern more naturally than a hardcoded regex early-exit, and the two
   approaches can coexist (regex early-exit for the simple/advanced pipeline, tool for
   Thinking Mode).

### Example

```
User: "What changed in food_rights.pdf between version 1 and version 2?"

Resolved: item_id/filename "food_rights.pdf", version 1 (uploaded 2026-06-10) vs
version 2 (uploaded 2026-10-20).

Answer:
"Version 2 was uploaded on 2026-10-20, replacing the version from 2026-06-10. Here's what
changed:
- Previously: the eligibility age was listed as 18. Now: it's listed as 16.
- Previously: no mention of remote applications. Now: a new section was added describing
  the remote application process.
Everything else (the introduction and the appeals section) is unchanged."
```

```
User: "What's the difference between abc.pdf and xyz.pdf?"

Resolved: two unrelated documents, no shared item_id/version history - each resolved to its
current active row directly.

Answer: same diff-then-summarize logic, applied to two arbitrary documents instead of two
versions of one document.
```

## Known limitations (explicit, accepted for this phase)

- Filename is the version-matching trigger **only when no `item_id` is supplied** on
  upload. In that fallback case, a renamed file (same content, new filename) is still
  treated as a brand-new, unrelated document. When the client supplies `item_id`, this is
  fully solved - version matching uses `item_id`, not filename, so renames are correctly
  recognized as updates. Automatic rename detection without an `item_id` (e.g. via the
  TF-IDF near-dup machinery already in `dedup.py`, extended to KBs) remains a possible
  future phase, not part of this plan.
- No chunk-level patching: every new version is fully re-parsed, re-chunked, and
  re-embedded, even if only a small part of the document changed.
- A failed re-embed during a version transition leaves the superseded row's vectors
  temporarily serving retrieval until the user retries (see Phase 3, step 14) - acceptable
  but worth confirming with you before implementation.
