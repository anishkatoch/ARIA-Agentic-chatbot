import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from fastapi import HTTPException


class JiraConnector:
    """Read-only Jira Cloud connector using REST API v3 and Basic Authentication."""

    def __init__(self, base_url: str | None = None, email: str | None = None, api_token: str | None = None):
        self.base_url = (base_url or os.getenv("JIRA_BASE_URL") or "").strip().rstrip("/")
        self.email = (email or os.getenv("JIRA_EMAIL") or "").strip()
        self.api_token = (api_token or os.getenv("JIRA_API_TOKEN") or "").strip()

        if self.base_url and not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            self.base_url = f"https://{self.base_url}"

        missing = []
        if not self.base_url:
            missing.append("JIRA_BASE_URL")
        if not self.email:
            missing.append("JIRA_EMAIL")
        if not self.api_token:
            missing.append("JIRA_API_TOKEN")

        self.missing_vars = missing
        self.is_configured = len(missing) == 0

        if self.is_configured:
            auth_str = f"{self.email}:{self.api_token}"
            self._auth_header = "Basic " + base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        else:
            self._auth_header = None

    def _validate(self):
        if not self.is_configured:
            raise HTTPException(
                status_code=500,
                detail=f"Jira connector not properly configured in .env. Missing: {', '.join(self.missing_vars)}",
            )

    @staticmethod
    def _extract_text(data: Any) -> str:
        """Recursively extract plain text from Atlassian Document Format (ADF) or strings."""
        if not data:
            return ""
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, (int, float)):
            return str(data)

        parts: list[str] = []

        def _traverse(node: Any):
            if not node:
                return
            if isinstance(node, str):
                parts.append(node)
                return
            if isinstance(node, list):
                for item in node:
                    _traverse(item)
                return
            if isinstance(node, dict):
                node_type = node.get("type")
                if node_type == "text":
                    text = node.get("text", "")
                    if text:
                        parts.append(text)
                elif node_type == "hardBreak":
                    parts.append("\n")
                elif node_type == "mention":
                    mention_text = node.get("attrs", {}).get("text", "")
                    if mention_text:
                        parts.append(mention_text)

                if "content" in node and isinstance(node["content"], list):
                    for child in node["content"]:
                        _traverse(child)
                    if node_type in (
                        "paragraph",
                        "heading",
                        "bulletList",
                        "orderedList",
                        "listItem",
                        "blockquote",
                        "codeBlock",
                    ):
                        parts.append("\n")

        _traverse(data)
        result = "".join(parts).strip()
        return re.sub(r"\n{3,}", "\n\n", result)

    def _api_request(self, endpoint: str, params: dict | None = None) -> Any:
        self._validate()

        ep = endpoint.strip()
        if not ep.startswith("/"):
            ep = f"/{ep}"
        if not ep.startswith("/rest/api/"):
            ep = f"/rest/api/3{ep}"

        url = f"{self.base_url}{ep}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                url += "?" + urllib.parse.urlencode(clean_params, doseq=True)

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": self._auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "aria-connectors",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_bytes = resp.read()
                if not raw_bytes:
                    return {}
                return json.loads(raw_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            err_msg = ""
            try:
                err_json = json.loads(err_body)
                if isinstance(err_json, dict):
                    if "errorMessages" in err_json and err_json["errorMessages"]:
                        err_msg = "; ".join(err_json["errorMessages"])
                    elif "message" in err_json:
                        err_msg = str(err_json["message"])
                    elif "errors" in err_json and isinstance(err_json["errors"], dict):
                        err_msg = "; ".join(f"{k}: {v}" for k, v in err_json["errors"].items())
            except Exception:
                err_msg = err_body[:200]

            if exc.code == 401:
                detail = f"Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN. {err_msg}".strip()
                raise HTTPException(status_code=401, detail=detail)
            if exc.code == 403:
                detail = f"Jira API access denied or insufficient permissions. {err_msg}".strip()
                raise HTTPException(status_code=403, detail=detail)
            if exc.code == 404:
                detail = f"Jira resource not found: {endpoint}. {err_msg}".strip()
                raise HTTPException(status_code=404, detail=detail)
            if exc.code == 400:
                detail = f"Jira bad request: {err_msg}".strip()
                raise HTTPException(status_code=400, detail=detail)

            raise HTTPException(
                status_code=502,
                detail=f"Jira API returned HTTP {exc.code}: {err_msg or exc.reason}",
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Unable to reach Jira API at {self.base_url}: {str(exc)}",
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Invalid JSON response from Jira API: {str(exc)}",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Unexpected error communicating with Jira: {str(exc)}",
            )

    def get_status(self) -> dict:
        """Check Jira connection status and return server info."""
        if not self.is_configured:
            return {
                "configured": False,
                "authenticated": False,
                "message": f"Jira is not configured. Missing environment variables: {', '.join(self.missing_vars)}",
            }

        try:
            server_info = self._api_request("/serverInfo")
            user_info = None
            try:
                user_info = self._api_request("/myself")
            except Exception:
                pass

            return {
                "configured": True,
                "authenticated": True,
                "base_url": self.base_url,
                "server_info": server_info,
                "user": user_info.get("displayName") if user_info else self.email,
                "email": user_info.get("emailAddress") if user_info else self.email,
                "version": server_info.get("version"),
                "deployment_type": server_info.get("deploymentType", "Cloud"),
            }
        except HTTPException as exc:
            return {
                "configured": True,
                "authenticated": False,
                "base_url": self.base_url,
                "error": exc.detail,
            }
        except Exception as exc:
            return {
                "configured": True,
                "authenticated": False,
                "base_url": self.base_url,
                "error": str(exc),
            }

    def search_issues(self, jql: str = "", max_results: int = 10) -> dict:
        """Search issues using JQL."""
        params: dict[str, Any] = {
            "maxResults": min(max(max_results, 1), 50),
            "fields": "summary,status,assignee,priority,created,updated,issuetype,project,description",
        }
        if jql and jql.strip():
            clean_jql = jql.strip()
            if "order by" not in clean_jql.lower():
                clean_jql += " order by created DESC"
            params["jql"] = clean_jql
        else:
            params["jql"] = "created is not EMPTY order by created DESC"

        raw = self._api_request("/search/jql", params=params)
        raw_issues = raw.get("issues", []) if isinstance(raw, dict) else []

        items = []
        for issue in raw_issues:
            key = issue.get("key", "")
            fields = issue.get("fields", {}) or {}

            status_val = ""
            if isinstance(fields.get("status"), dict):
                status_val = fields["status"].get("name", "")
            elif fields.get("status"):
                status_val = str(fields["status"])

            assignee_val = "Unassigned"
            if isinstance(fields.get("assignee"), dict):
                assignee_val = (
                    fields["assignee"].get("displayName")
                    or fields["assignee"].get("emailAddress")
                    or "Unassigned"
                )
            elif fields.get("assignee"):
                assignee_val = str(fields["assignee"])

            priority_val = ""
            if isinstance(fields.get("priority"), dict):
                priority_val = fields["priority"].get("name", "")
            elif fields.get("priority"):
                priority_val = str(fields["priority"])

            type_val = ""
            if isinstance(fields.get("issuetype"), dict):
                type_val = fields["issuetype"].get("name", "")
            elif fields.get("issuetype"):
                type_val = str(fields["issuetype"])

            project_val = ""
            project_key = ""
            if isinstance(fields.get("project"), dict):
                project_val = fields["project"].get("name") or fields["project"].get("key", "")
                project_key = fields["project"].get("key", "")
            elif fields.get("project"):
                project_val = str(fields["project"])
                project_key = project_val

            items.append({
                "id": issue.get("id", ""),
                "key": key,
                "summary": fields.get("summary", ""),
                "status": status_val,
                "assignee": assignee_val,
                "priority": priority_val,
                "created": fields.get("created", ""),
                "updated": fields.get("updated", ""),
                "type": type_val,
                "project": project_val,
                "project_key": project_key,
                "url": f"{self.base_url}/browse/{key}" if key else "",
                "description": self._extract_text(fields.get("description")),
            })

        return {
            "issues": items,
            "total": raw.get("total", len(items)) if isinstance(raw, dict) else len(items),
            "max_results": raw.get("maxResults", max_results) if isinstance(raw, dict) else max_results,
            "start_at": raw.get("startAt", 0) if isinstance(raw, dict) else 0,
        }

    def get_issue(self, issue_key: str) -> dict:
        """Get full issue details including description and comments."""
        clean_key = (issue_key or "").strip()
        if not clean_key:
            raise HTTPException(400, "Issue key must not be empty")

        raw = self._api_request(f"/issue/{urllib.parse.quote(clean_key)}")
        fields = raw.get("fields", {}) or {}

        status_val = ""
        if isinstance(fields.get("status"), dict):
            status_val = fields["status"].get("name", "")
        elif fields.get("status"):
            status_val = str(fields["status"])

        assignee_val = "Unassigned"
        if isinstance(fields.get("assignee"), dict):
            assignee_val = (
                fields["assignee"].get("displayName")
                or fields["assignee"].get("emailAddress")
                or "Unassigned"
            )
        elif fields.get("assignee"):
            assignee_val = str(fields["assignee"])

        reporter_val = ""
        if isinstance(fields.get("reporter"), dict):
            reporter_val = (
                fields["reporter"].get("displayName")
                or fields["reporter"].get("emailAddress")
                or ""
            )
        elif fields.get("reporter"):
            reporter_val = str(fields["reporter"])

        priority_val = ""
        if isinstance(fields.get("priority"), dict):
            priority_val = fields["priority"].get("name", "")
        elif fields.get("priority"):
            priority_val = str(fields["priority"])

        type_val = ""
        if isinstance(fields.get("issuetype"), dict):
            type_val = fields["issuetype"].get("name", "")
        elif fields.get("issuetype"):
            type_val = str(fields["issuetype"])

        project_val = ""
        project_key = ""
        if isinstance(fields.get("project"), dict):
            project_val = fields["project"].get("name") or fields["project"].get("key", "")
            project_key = fields["project"].get("key", "")
        elif fields.get("project"):
            project_val = str(fields["project"])
            project_key = project_val

        # Extract comments
        comment_data = fields.get("comment", {})
        raw_comments = (
            comment_data.get("comments", []) if isinstance(comment_data, dict) else []
        )
        comments = []
        for c in raw_comments:
            author_info = c.get("author", {}) if isinstance(c.get("author"), dict) else {}
            comments.append({
                "id": c.get("id", ""),
                "author": author_info.get("displayName") or author_info.get("emailAddress", "Unknown"),
                "created": c.get("created", ""),
                "updated": c.get("updated", ""),
                "body": self._extract_text(c.get("body")),
            })

        actual_key = raw.get("key", clean_key)
        return {
            "id": raw.get("id", ""),
            "key": actual_key,
            "summary": fields.get("summary", ""),
            "description": self._extract_text(fields.get("description")),
            "status": status_val,
            "assignee": assignee_val,
            "reporter": reporter_val,
            "priority": priority_val,
            "type": type_val,
            "project": project_val,
            "project_key": project_key,
            "labels": fields.get("labels", []) if isinstance(fields.get("labels"), list) else [],
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "url": f"{self.base_url}/browse/{actual_key}" if actual_key else "",
            "comments": comments,
            "comments_total": len(comments),
        }

    def list_projects(self) -> dict:
        """List Jira projects."""
        raw = self._api_request("/project")
        if isinstance(raw, list):
            project_list = raw
        elif isinstance(raw, dict) and "values" in raw:
            project_list = raw["values"]
        elif isinstance(raw, dict) and "projects" in raw:
            project_list = raw["projects"]
        else:
            project_list = []

        items = []
        for p in project_list:
            lead_info = p.get("lead", {}) if isinstance(p.get("lead"), dict) else {}
            items.append({
                "id": p.get("id", ""),
                "key": p.get("key", ""),
                "name": p.get("name", ""),
                "project_type": p.get("projectTypeKey", ""),
                "lead": lead_info.get("displayName", "") or lead_info.get("name", ""),
                "url": f"{self.base_url}/browse/{p.get('key')}" if p.get("key") else "",
            })

        return {
            "projects": items,
            "total": len(items),
        }


def get_jira_connector(user_id: str | None = None) -> JiraConnector:
    """Uses this user's own credentials (from connector_credentials) if
    they've set them, otherwise falls back to the shared JIRA_* env vars."""
    creds = {}
    if user_id:
        from app.connectors import credential_store
        creds = credential_store.get_credentials(user_id, "jira") or {}
    return JiraConnector(
        base_url=creds.get("base_url"),
        email=creds.get("email"),
        api_token=creds.get("api_token"),
    )
