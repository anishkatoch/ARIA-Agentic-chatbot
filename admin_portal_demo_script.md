# ARIA Admin Portal Demo Script — Knowledge Base CRUD

URL: `http://localhost:8001/admin.html`
Login: ADMIN_EMAIL / ADMIN_PASSWORD from `.env`

Record at normal click speed; pause ~1s after each action before the next narration line starts, so captions don't get cut off.

| # | Time | On-screen action | Narration (voiceover) | Subtitle (on-screen caption) |
|---|------|-------------------|------------------------|-------------------------------|
| 1 | 0:00–0:05 | Open `admin.html`, show the "Admin sign in" screen | "This is the ARIA admin portal — the control center for managing knowledge bases and users." | Welcome to the ARIA Admin Portal |
| 2 | 0:05–0:12 | Type admin email + password into the two fields, click "Sign In" | "Admins sign in with a single fixed account, separate from regular user logins." | Signing in as admin |
| 3 | 0:12–0:18 | Dashboard loads on the Knowledge Bases tab, stats row visible (Total bases / Active / Total files / Roles covered) | "Right away we see an overview — how many knowledge bases exist, how many are active, and how many files they hold." | Dashboard overview: KB stats at a glance |
| 4 | 0:18–0:24 | Hover/point at the existing KB cards in the grid | "Each card is a knowledge base — a scoped set of documents a group of users can search." | Existing knowledge bases |
| 5 | 0:24–0:30 | Click "+ New Knowledge Base" | "Let's create a new one." | Creating a new Knowledge Base |
| 6 | 0:30–0:40 | Fill in Name ("e.g. HR Policies"), Description, leave roles empty | "We give it a name, a short description, and choose which roles can see it — leaving this empty makes it visible to every signed-in user." | Name, description, and role access |
| 7 | 0:40–0:45 | Click "Create" | "One click and it's live." | Knowledge base created |
| 8 | 0:45–0:52 | New KB card appears in the grid, stats row updates | "It immediately appears in the grid, and our totals update automatically." | Instantly reflected in the dashboard |
| 9 | 0:52–0:58 | Click into the new KB card to open its files modal | "Opening the knowledge base, we can now add documents to it." | Adding documents |
| 10 | 0:58–1:08 | Upload a file (drag-and-drop or file picker), show progress/ingestion | "We upload a file and ARIA ingests it — parsing, chunking, and embedding it for retrieval, all in the background." | Uploading and ingesting a document |
| 11 | 1:08–1:15 | File appears in the files list for that KB | "The document now shows up in this knowledge base's file list, ready to be searched in chat." | Document ready for search |
| 12 | 1:15–1:22 | Click the file to view its version history (if re-uploaded) or close modal | "Every file keeps its version history, so admins can track changes over time." | Version history tracked per file |
| 13 | 1:22–1:28 | Toggle the KB's active/inactive switch | "Knowledge bases can be paused without deleting them — handy for maintenance." | Toggle active status |
| 14 | 1:28–1:33 | Close modal, return to grid view | "And that's the full lifecycle — create, populate, and manage a knowledge base end to end." | Full KB lifecycle, start to finish |
| 15 | 1:33–1:38 | Fade out / end card | "That's the ARIA admin portal." | Thanks for watching |

## Notes for recording
- If the login screen flashes an error, double check `.env` admin credentials haven't changed since this script was written.
- Keep each subtitle on screen for the full narration line (don't let it outrun the voiceover).
- If you also want a Users-tab segment later, say so and I'll extend this with the "+ Create User" / role assignment flow (`openCreateUserModal` → `handleCreateUser`).
