import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException


API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_FILE_SIZE = 1_000_000


class GitHubConnector:
    """Read-only GitHub connector authenticated via a plain GITHUB_TOKEN env var.

    NOTE: CONNECTOR_POC's original implementation shelled out to
    `gh auth token` (the GitHub CLI) - that depends on the `gh` binary being
    installed AND already logged in on the host machine, which breaks in a
    deployed container and isn't tied to any particular user. This reads
    GITHUB_TOKEN instead (a personal access token with repo read scope).
    """

    def _get_token(self) -> str:
        token = os.getenv("GITHUB_TOKEN", "").strip()
        if not token:
            raise HTTPException(
                status_code=500,
                detail=(
                    "GITHUB_TOKEN environment variable not set. Add a GitHub "
                    "personal access token (repo read scope) to .env."
                ),
            )
        return token

    def _request(self, endpoint: str, params: dict | None = None):
        token = self._get_token()

        url = f"{API_BASE}{endpoint}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "aria-connectors",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise HTTPException(404, "GitHub resource not found or inaccessible")
            if exc.code == 401:
                raise HTTPException(401, "GitHub authentication failed - check GITHUB_TOKEN")
            if exc.code == 403:
                raise HTTPException(
                    403,
                    "GitHub access denied or API rate limit reached",
                )
            if exc.code == 429:
                raise HTTPException(429, "GitHub API rate limit reached")
            raise HTTPException(502, f"GitHub API returned HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError):
            raise HTTPException(502, "Unable to reach GitHub API")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(502, "Invalid response from GitHub API")

    @staticmethod
    def _validate_repo(owner: str, repo: str):
        pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
        if not pattern.fullmatch(owner) or not pattern.fullmatch(repo):
            raise HTTPException(400, "Invalid GitHub owner or repository name")

    def list_repositories(
        self,
        visibility: str = "all",
        page: int = 1,
        per_page: int = 30,
    ) -> dict:
        if visibility not in {"all", "public", "private"}:
            raise HTTPException(400, "visibility must be all, public, or private")
        if page < 1 or per_page < 1 or per_page > 100:
            raise HTTPException(400, "page must be positive and per_page must be 1-100")

        repositories = self._request(
            "/user/repos",
            {
                "visibility": visibility,
                "affiliation": "owner,collaborator,organization_member",
                "sort": "full_name",
                "per_page": per_page,
                "page": page,
            },
        )

        items = [
            {
                "name": item["name"],
                "full_name": item["full_name"],
                "owner": item["owner"]["login"],
                "private": item["private"],
                "visibility": item.get("visibility", "private" if item["private"] else "public"),
                "default_branch": item.get("default_branch"),
                "html_url": item["html_url"],
                "description": item.get("description"),
            }
            for item in repositories
        ]

        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total_items": len(items),
        }

    def list_contents(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str | None = None,
    ) -> dict:
        self._validate_repo(owner, repo)

        encoded_owner = urllib.parse.quote(owner, safe="")
        encoded_repo = urllib.parse.quote(repo, safe="")
        encoded_path = urllib.parse.quote(path.strip("/"), safe="/")
        endpoint = f"/repos/{encoded_owner}/{encoded_repo}/contents"
        if encoded_path:
            endpoint += f"/{encoded_path}"

        params = {"ref": ref} if ref else None
        result = self._request(endpoint, params)

        entries = result if isinstance(result, list) else [result]
        items = [
            {
                "name": item["name"],
                "path": item["path"],
                "type": item["type"],
                "size": item.get("size", 0),
                "sha": item.get("sha"),
                "html_url": item.get("html_url"),
            }
            for item in entries
        ]

        return {
            "repository": f"{owner}/{repo}",
            "path": path,
            "ref": ref,
            "items": items,
            "total_items": len(items),
        }

    def download_file(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str | None = None,
    ) -> tuple[bytes, str]:
        if not path or not path.strip("/"):
            raise HTTPException(400, "A file path is required")

        self._validate_repo(owner, repo)

        encoded_owner = urllib.parse.quote(owner, safe="")
        encoded_repo = urllib.parse.quote(repo, safe="")
        encoded_path = urllib.parse.quote(path.strip("/"), safe="/")
        endpoint = f"/repos/{encoded_owner}/{encoded_repo}/contents/{encoded_path}"

        params = {"ref": ref} if ref else None
        result = self._request(endpoint, params)

        if not isinstance(result, dict) or result.get("type") != "file":
            raise HTTPException(400, "The requested GitHub path is not a file")

        if result.get("size", 0) > MAX_FILE_SIZE:
            raise HTTPException(
                413,
                "File exceeds the 1 MB download limit",
            )

        if result.get("encoding") != "base64" or not result.get("content"):
            raise HTTPException(502, "GitHub did not return downloadable file content")

        try:
            content = base64.b64decode(result["content"], validate=False)
        except (ValueError, TypeError):
            raise HTTPException(502, "Unable to decode GitHub file content")

        return content, result["name"]


def get_github_connector() -> GitHubConnector:
    return GitHubConnector()
