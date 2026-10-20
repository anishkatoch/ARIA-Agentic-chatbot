import base64
import os
from typing import Any
from urllib.parse import quote, urlparse
import requests


class AzureDevOpsError(Exception):
    """Raised when an Azure DevOps API request fails."""


class AzureDevOpsConnector:
    """Read-only Azure DevOps Repos connector."""

    API_VERSION = "7.1"

    def __init__(self) -> None:
        org_url = os.getenv("AZURE_DEVOPS_ORG_URL", "").strip().rstrip("/")
        self.pat = os.getenv("AZURE_DEVOPS_PAT", "").strip()

        if not org_url:
            raise AzureDevOpsError(
                "Missing AZURE_DEVOPS_ORG_URL environment variable."
            )
        if not self.pat:
            raise AzureDevOpsError(
                "Missing AZURE_DEVOPS_PAT environment variable."
            )

        parsed = urlparse(org_url)
        if parsed.scheme != "https" or parsed.hostname != "dev.azure.com":
            raise AzureDevOpsError(
                "AZURE_DEVOPS_ORG_URL must use an HTTPS dev.azure.com URL."
            )

        self.org_url = org_url
        self.session = requests.Session()
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        self.session.headers.update(
            {
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "User-Agent": "aria-connectors",
            }
        )

    @staticmethod
    def _segment(value: str) -> str:
        if not value or not value.strip():
            raise ValueError("A non-empty ID/name is required.")
        return quote(value.strip(), safe="")

    def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        is_stream: bool = False,
    ) -> requests.Response:
        url = f"{self.org_url}/{endpoint.lstrip('/')}"
        query_params = {"api-version": self.API_VERSION}
        if params:
            query_params.update(params)

        try:
            resp = self.session.get(url, params=query_params, timeout=25, stream=is_stream)
        except requests.RequestException as exc:
            raise AzureDevOpsError(f"Unable to reach Azure DevOps: {str(exc)}") from exc

        if resp.status_code == 401:
            raise AzureDevOpsError("Azure DevOps authentication failed. Check your PAT token.")
        if resp.status_code == 403:
            raise AzureDevOpsError("Azure DevOps access denied or insufficient permissions.")
        if resp.status_code == 404:
            raise AzureDevOpsError("Azure DevOps resource not found.")
        if not resp.ok:
            raise AzureDevOpsError(
                f"Azure DevOps API returned HTTP {resp.status_code}: {resp.text}"
            )

        return resp

    def list_projects(self) -> list[dict[str, Any]]:
        resp = self._request("_apis/projects")
        data = resp.json()
        return data.get("value", [])

    def list_repositories(self, project: str | None = None) -> list[dict[str, Any]]:
        if project:
            proj = self._segment(project)
            endpoint = f"{proj}/_apis/git/repositories"
        else:
            endpoint = "_apis/git/repositories"

        resp = self._request(endpoint)
        data = resp.json()
        return data.get("value", [])

    def list_items(
        self,
        project: str,
        repository_id: str,
        path: str = "/",
        recursion_level: str = "OneLevel",
        scope_path: str | None = None,
    ) -> list[dict[str, Any]]:
        target_path = scope_path if scope_path is not None else path
        proj = self._segment(project)
        repo = self._segment(repository_id)
        params = {
            "scopePath": target_path or "/",
            "recursionLevel": recursion_level,
        }
        resp = self._request(f"{proj}/_apis/git/repositories/{repo}/items", params=params)
        data = resp.json()
        return data.get("value", [])

    def get_file(
        self,
        project: str,
        repository_id: str,
        path: str,
    ) -> bytes:
        if not path or not path.strip("/"):
            raise ValueError("A file path is required.")

        proj = self._segment(project)
        repo = self._segment(repository_id)

        params = {
            "path": path,
            "$format": "octetStream",
        }
        resp = self._request(
            f"{proj}/_apis/git/repositories/{repo}/items",
            params=params,
        )
        return resp.content
