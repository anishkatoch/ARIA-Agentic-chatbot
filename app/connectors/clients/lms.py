import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from fastapi import HTTPException

# app/connectors/clients/lms.py -> parents[3] is the repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_FILE = PROJECT_ROOT / "test-lms" / "lms_data.json"
FILES_ROOT = (PROJECT_ROOT / "test-lms" / "files").resolve()


class LMSConnector:
    """Local mock LMS adapter. No live LMS integration yet."""

    def _load_data(self) -> dict:
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise HTTPException(500, "LMS sample data file is missing")
        except (json.JSONDecodeError, OSError):
            raise HTTPException(500, "Unable to read LMS sample data")

        if not isinstance(data, dict):
            raise HTTPException(500, "Invalid LMS sample data")
        if not isinstance(data.get("courses"), list) or not isinstance(
            data.get("files"), list
        ):
            raise HTTPException(500, "Invalid LMS sample data structure")

        return data

    def list_courses(self) -> dict:
        data = self._load_data()
        courses = [
            {
                "id": course["id"],
                "name": course["name"],
                "description": course.get("description", ""),
                "module_count": len(course.get("modules", [])),
            }
            for course in data["courses"]
        ]
        return {"items": courses, "total_items": len(courses)}

    def list_modules(self, course_id: str) -> dict:
        data = self._load_data()
        course = next(
            (c for c in data["courses"] if c.get("id") == course_id), None
        )
        if course is None:
            raise HTTPException(404, "Course not found")

        modules = [
            {
                "id": m["id"],
                "name": m["name"],
                "file_count": len(m.get("files", [])),
                "files": m.get("files", []),
            }
            for m in course.get("modules", [])
        ]
        return {"course_id": course_id, "items": modules, "total_items": len(modules)}

    def _resolve_file_path(self, relative_path: str) -> Path:
        requested = Path(relative_path or ".")
        if requested.is_absolute():
            raise HTTPException(400, "Absolute paths are not allowed")
        if ".." in requested.parts:
            raise HTTPException(400, "Parent-directory traversal is not allowed")

        try:
            candidate = (FILES_ROOT / requested).resolve(strict=True)
        except FileNotFoundError:
            raise HTTPException(404, "File not found on disk")

        try:
            candidate.relative_to(FILES_ROOT)
        except ValueError:
            raise HTTPException(403, "File path is outside authorized root")

        if not candidate.is_file():
            raise HTTPException(400, "Requested path is not a file")

        return candidate

    def _get_file_info(self, file_entry: dict) -> dict:
        file_path = self._resolve_file_path(file_entry.get("path", ""))
        stat_res = file_path.stat()
        modified_at = datetime.fromtimestamp(stat_res.st_mtime, tz=timezone.utc).isoformat()
        mime_type = mimetypes.guess_type(file_path.name)[0] or "text/plain"

        return {
            "id": file_entry["id"],
            "course_id": file_entry.get("course_id"),
            "module_id": file_entry.get("module_id"),
            "name": file_entry.get("name"),
            "type": "file",
            "size": stat_res.st_size,
            "modified_at": modified_at,
            "mime_type": mime_type,
        }

    def list_course_files(self, course_id: str) -> dict:
        data = self._load_data()
        course = next(
            (c for c in data["courses"] if c.get("id") == course_id), None
        )
        if course is None:
            raise HTTPException(404, "Course not found")

        course_files = [
            f for f in data.get("files", []) if f.get("course_id") == course_id
        ]
        items = [self._get_file_info(f) for f in course_files]
        return {"course_id": course_id, "items": items, "total_items": len(items)}

    def get_file(self, file_id: str) -> Path:
        data = self._load_data()
        file_entry = next(
            (f for f in data.get("files", []) if f.get("id") == file_id), None
        )
        if file_entry is None:
            raise HTTPException(404, "File not found")

        return self._resolve_file_path(file_entry.get("path", ""))

    def get_file_metadata(self, file_id: str) -> dict:
        data = self._load_data()
        file_entry = next(
            (f for f in data.get("files", []) if f.get("id") == file_id), None
        )
        if file_entry is None:
            raise HTTPException(404, "File not found")

        return self._get_file_info(file_entry)


def get_lms_connector() -> LMSConnector:
    return LMSConnector()
