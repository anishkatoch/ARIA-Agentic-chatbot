# app/connectors/clients/sharepoint.py
from typing import Any
from urllib.parse import quote
from app.connectors.clients.microsoft_graph import (
    MicrosoftGraphClient,
    MicrosoftGraphError,
)


class SharePointConnector:
    """Read-only SharePoint operations using Microsoft Graph."""

    def __init__(self) -> None:
        self.graph = MicrosoftGraphClient()

    @staticmethod
    def _segment(value: str) -> str:
        if not value or not value.strip():
            raise ValueError("A non-empty ID is required.")
        return quote(value, safe="")

    def list_sites(self, search: str | None = None) -> list[dict[str, Any]]:
        params = {"$select": "id,name,displayName,webUrl,description"}
        if search and search.strip():
            params["search"] = search.strip()
            return self.graph.get_all("/sites", params=params)
        else:
            return self.graph.get_all("/sites?search=*", params=params)

    def list_drives(self, site_id: str) -> list[dict[str, Any]]:
        site = self._segment(site_id)
        return self.graph.get_all(
            f"/sites/{site}/drives",
            params={"$select": "id,name,description,driveType,webUrl"},
        )

    def list_items(
        self,
        drive_id: str,
        folder_id: str = "root",
    ) -> list[dict[str, Any]]:
        drive = self._segment(drive_id)
        if folder_id == "root":
            endpoint = f"/drives/{drive}/root/children"
        else:
            folder = self._segment(folder_id)
            endpoint = f"/drives/{drive}/items/{folder}/children"

        return self.graph.get_all(
            endpoint,
            params={
                "$select": "id,name,size,webUrl,file,folder,lastModifiedDateTime"
            },
        )

    def download_item(
        self,
        drive_id: str,
        item_id: str,
    ) -> tuple[bytes, str]:
        drive = self._segment(drive_id)
        item = self._segment(item_id)

        metadata = self.graph.get_json(
            f"/drives/{drive}/items/{item}",
            params={"$select": "id,name,file"},
        )
        if "file" not in metadata:
            raise ValueError("The requested SharePoint item is a folder, not a downloadable file.")

        filename = metadata.get("name", "sharepoint_download")
        content = self.graph.download_file_content(f"/drives/{drive}/items/{item}/content")
        return content, filename
