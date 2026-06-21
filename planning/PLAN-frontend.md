# Frontend Plan — HTML/JS → React

---

## Stage A — Plain HTML + CSS + Vanilla JS ✅ Done

FastAPI serves the frontend directly from `app/static/`. No build tools, no npm.

### What's Built
```
app/static/
├── index.html    # Shell — loads CSS, JS, marked.js for markdown
├── style.css     # Full UI styling — chat bubbles, tabs, progress cards, toasts
└── chat.js       # All frontend logic
```

### Features
| Feature | Status |
|---|---|
| Tabs — Files / URL / API | ✅ Done |
| Drag & drop file upload | ✅ Done |
| File validation (type, size, count) | ✅ Done |
| SSE streaming progress cards with live timers | ✅ Done |
| Chat UI with markdown rendering (marked.js) | ✅ Done |
| Citations toggle — source, chunk, page number, confidence | ✅ Done |
| Copy answer button | ✅ Done |
| Toast notifications | ✅ Done |
| Session badge + reset button | ✅ Done |
| API headers panel (add/remove key-value rows) | ✅ Done |
| Auto-resize textarea | ✅ Done |
| Typing indicator | ✅ Done |

### How Citations Appear
Each answer shows expandable sources:
```
▶ report.pdf — Chunk #7 — Page 3 — Confidence 98.1% — "preview text..."
```

### Limits (controlled via .env — no code change needed)
```
MAX_FILES_PER_SESSION = 3
MAX_FILE_SIZE_MB      = 15
ALLOWED               = .pdf, .doc, .docx, .txt
```

---

## Phase 6 — Stage B: React + Tailwind + Vite 📋 Planned

**When:** After Stage A is stable and the UI needs to grow — components, routing, auth pages.

### Why React
- Reusable components (ChatBubble, FileUpload, CitationCard)
- Tailwind for fast styling
- Vite for fast builds
- Mobile responsive layouts

### Planned Folder Structure
```
frontend/
├── src/
│   ├── App.tsx
│   ├── components/
│   │   ├── ChatWindow.tsx
│   │   ├── MessageBubble.tsx
│   │   ├── CitationCard.tsx
│   │   └── FileUpload.tsx
│   └── api/
│       └── client.ts       # all fetch() calls to FastAPI
├── package.json
└── vite.config.ts
```

### Run Commands
| Mode | Command |
|---|---|
| Stage A (current) | `uv run uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload` |
| Stage B dev | `make dev` — starts FastAPI + Vite together |
| Stage B production | `npm run build` → outputs to `app/static/` → FastAPI serves it |

### New Features Planned for Stage B
- Mobile responsive layout
- Dark mode toggle
- Session history sidebar (past conversations)
- Multi-document comparison view
- Highlighted page citations (click citation → highlight page in PDF viewer)
