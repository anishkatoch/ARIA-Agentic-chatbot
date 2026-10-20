import os
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from fastapi import HTTPException


class FileShareConnector:
    def __init__(self, root: str):
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as e:
            raise ValueError(f"FILE_SHARE_ROOT path does not exist: {e}")

        if not self.root.is_dir():
            raise ValueError("FILE_SHARE_ROOT must be a directory")

    def _resolve(self, relative_path: str = "") -> Path:
        # Only relative paths are accepted.
        requested = Path(relative_path or ".")
        if requested.is_absolute():
            raise HTTPException(400, "Absolute paths are not allowed")
        if ".." in requested.parts:
            raise HTTPException(400, "Parent-directory traversal is not allowed")

        try:
            candidate = (self.root / requested).resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(404, "Path not found")

        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise HTTPException(403, "Path is outside the configured root")

        return candidate

    def list_directory(self, relative_path: str = "") -> dict:
        target = self._resolve(relative_path)
        if not target.is_dir():
            raise HTTPException(400, "Requested path is not a directory")

        items = []
        try:
            entries = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for entry in entries:
                is_dir = entry.is_dir()
                entry_stat = entry.stat()
                modified_at = datetime.fromtimestamp(entry_stat.st_mtime, tz=timezone.utc).isoformat()
                mime_type = None if is_dir else (mimetypes.guess_type(entry.name)[0] or "application/octet-stream")

                items.append({
                    "name": entry.name,
                    "path": str(entry.relative_to(self.root)),
                    "type": "folder" if is_dir else "file",
                    "size": entry_stat.st_size if not is_dir else None,
                    "modified_at": modified_at,
                    "mime_type": mime_type,
                })
        except PermissionError:
            raise HTTPException(403, "Permission denied reading directory")

        relative_current = "" if target == self.root else str(target.relative_to(self.root))

        return {
            "root": str(self.root),
            "current_path": relative_current,
            "items": items,
            "total_items": len(items),
        }

    def get_file(self, relative_path: str) -> Path:
        candidate = self._resolve(relative_path)
        if not candidate.is_file():
            raise HTTPException(400, "Requested path is not a file")
        return candidate


def get_connector() -> FileShareConnector:
    root = os.getenv("FILE_SHARE_ROOT")
    if not root:
        raise HTTPException(500, "FILE_SHARE_ROOT environment variable not set")
    try:
        return FileShareConnector(root)
    except ValueError as e:
        raise HTTPException(500, str(e))
