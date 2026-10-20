import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from fastapi import HTTPException

logger = logging.getLogger(__name__)


class ServiceNowConnector:
    """ServiceNow connector using ServiceNow Table API and HTTP Basic Authentication."""

    def __init__(
        self,
        instance_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        raw_url = (instance_url or os.getenv("SERVICENOW_INSTANCE_URL") or "").strip().rstrip("/")
        if raw_url and not (raw_url.startswith("http://") or raw_url.startswith("https://")):
            raw_url = f"https://{raw_url}"
        self.instance_url = raw_url

        self.username = (username or os.getenv("SERVICENOW_USERNAME") or "").strip()
        self.password = (password or os.getenv("SERVICENOW_PASSWORD") or "").strip()

    @property
    def is_configured(self) -> bool:
        """Check if all required configuration credentials are set."""
        return bool(self.instance_url and self.username and self.password)

    @staticmethod
    def _extract_display_value(val: Any) -> str:
        """Extract a string representation from either a string or ServiceNow display dict."""
        if val is None:
            return ""
        if isinstance(val, dict):
            return str(val.get("display_value", val.get("value", "")))
        return str(val)

    def _api_request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Execute an authenticated HTTP GET request to the ServiceNow Table API using urllib.request.

        Endpoint can be a full URL, an absolute path (/api/now/table/{table}), or just a table name.
        """
        if not self.is_configured:
            raise HTTPException(
                status_code=500,
                detail="ServiceNow is not properly configured. Ensure SERVICENOW_INSTANCE_URL, SERVICENOW_USERNAME, and SERVICENOW_PASSWORD are set.",
            )

        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            url = endpoint
        elif endpoint.startswith("/"):
            url = f"{self.instance_url}{endpoint}"
        else:
            url = f"{self.instance_url}/api/now/table/{endpoint}"

        if params:
            clean_params = {k: str(v) for k, v in params.items() if v is not None and v != ""}
            if clean_params:
                url += "?" + urllib.parse.urlencode(clean_params, doseq=True)

        auth_str = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {auth_str}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "aria-connectors",
        }

        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp_text = resp.read().decode("utf-8")
                return json.loads(resp_text)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.error("ServiceNow API HTTP error %d: %s", exc.code, err_body)
            if exc.code == 401:
                raise HTTPException(status_code=401, detail=f"ServiceNow authentication failed: {err_body}")
            if exc.code == 403:
                raise HTTPException(status_code=403, detail=f"ServiceNow access forbidden: {err_body}")
            if exc.code == 404:
                raise HTTPException(status_code=404, detail=f"ServiceNow resource not found: {err_body}")
            raise HTTPException(status_code=exc.code, detail=f"ServiceNow API error: {err_body}")
        except urllib.error.URLError as exc:
            logger.error("ServiceNow connection error: %s", str(exc))
            raise HTTPException(status_code=502, detail=f"Failed to connect to ServiceNow instance: {str(exc.reason)}")
        except Exception as exc:
            logger.error("Unexpected ServiceNow error: %s", str(exc))
            raise HTTPException(status_code=500, detail=f"Unexpected ServiceNow error: {str(exc)}")

    def get_status(self) -> dict[str, Any]:
        """Check whether ServiceNow credentials are configured and instance is reachable."""
        if not self.is_configured:
            return {
                "configured": False,
                "reachable": False,
                "instance_url": self.instance_url or None,
                "message": "ServiceNow credentials not configured. Please set SERVICENOW_INSTANCE_URL, SERVICENOW_USERNAME, and SERVICENOW_PASSWORD in .env",
            }

        try:
            # Query a single record to test credentials and reachability
            self._api_request("/api/now/table/incident", params={"sysparm_limit": 1, "sysparm_fields": "sys_id"})
            return {
                "configured": True,
                "reachable": True,
                "instance_url": self.instance_url,
                "username": self.username,
                "message": "Connected to ServiceNow instance successfully",
            }
        except Exception as exc:
            return {
                "configured": True,
                "reachable": False,
                "instance_url": self.instance_url,
                "username": self.username,
                "message": f"ServiceNow instance unreachable or error: {str(exc)}",
            }

    def list_incidents(self, max_results: int = 10, query: str = "") -> dict[str, Any]:
        """List incidents from the incident table."""
        limit = min(max(int(max_results), 1), 100)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_display_value": "true",
            "sysparm_fields": "sys_id,number,short_description,state,priority,assigned_to,opened_at,description,urgency,impact,caller_id",
        }
        if query and query.strip():
            clean_q = query.strip()
            if "=" in clean_q or "^" in clean_q:
                params["sysparm_query"] = clean_q
            else:
                params["sysparm_query"] = f"short_descriptionLIKE{clean_q}^ORdescriptionLIKE{clean_q}^ORnumberLIKE{clean_q}"

        raw = self._api_request("/api/now/table/incident", params=params)
        raw_items = raw.get("result", [])
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []

        incidents = []
        for row in raw_items:
            incidents.append({
                "sys_id": self._extract_display_value(row.get("sys_id")),
                "number": self._extract_display_value(row.get("number")),
                "short_description": self._extract_display_value(row.get("short_description")),
                "state": self._extract_display_value(row.get("state")),
                "priority": self._extract_display_value(row.get("priority")),
                "assigned_to": self._extract_display_value(row.get("assigned_to")),
                "opened_at": self._extract_display_value(row.get("opened_at")),
                "description": self._extract_display_value(row.get("description")),
                "urgency": self._extract_display_value(row.get("urgency")),
                "impact": self._extract_display_value(row.get("impact")),
                "caller_id": self._extract_display_value(row.get("caller_id")),
            })

        return {
            "incidents": incidents,
            "count": len(incidents),
            "result": incidents,
        }

    def get_incident(self, sys_id: str) -> dict[str, Any]:
        """Get full details of a single incident by sys_id or incident number."""
        clean_id = (sys_id or "").strip()
        if not clean_id:
            raise HTTPException(status_code=400, detail="Incident sys_id is required")

        if clean_id.upper().startswith("INC") and len(clean_id) < 32:
            res = self.list_incidents(max_results=1, query=f"number={clean_id.upper()}")
            incidents = res.get("incidents", [])
            if not incidents:
                raise HTTPException(status_code=404, detail=f"Incident {clean_id} not found")
            clean_id = incidents[0]["sys_id"]

        raw = self._api_request(
            f"/api/now/table/incident/{clean_id}",
            params={"sysparm_display_value": "true"},
        )
        data = raw.get("result", raw)
        return data

    def list_requests(self, max_results: int = 10, query: str = "") -> dict[str, Any]:
        """List service requests from sc_request table."""
        limit = min(max(int(max_results), 1), 100)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_display_value": "true",
            "sysparm_fields": "sys_id,number,short_description,description,stage,request_state,opened_at,requested_for,price,priority",
        }
        if query and query.strip():
            clean_q = query.strip()
            if "=" in clean_q or "^" in clean_q:
                params["sysparm_query"] = clean_q
            else:
                params["sysparm_query"] = f"short_descriptionLIKE{clean_q}^ORdescriptionLIKE{clean_q}^ORnumberLIKE{clean_q}"

        raw = self._api_request("/api/now/table/sc_request", params=params)
        raw_items = raw.get("result", [])
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []

        requests_list = []
        for row in raw_items:
            requests_list.append({
                "sys_id": self._extract_display_value(row.get("sys_id")),
                "number": self._extract_display_value(row.get("number")),
                "short_description": self._extract_display_value(row.get("short_description")),
                "description": self._extract_display_value(row.get("description")),
                "stage": self._extract_display_value(row.get("stage")),
                "request_state": self._extract_display_value(row.get("request_state")),
                "opened_at": self._extract_display_value(row.get("opened_at")),
                "requested_for": self._extract_display_value(row.get("requested_for")),
                "priority": self._extract_display_value(row.get("priority")),
                "price": self._extract_display_value(row.get("price")),
            })

        return {
            "requests": requests_list,
            "count": len(requests_list),
            "result": requests_list,
        }

    def search_knowledge(self, query: str = "", max_results: int = 10) -> dict[str, Any]:
        """Search knowledge base articles from kb_knowledge table."""
        limit = min(max(int(max_results), 1), 100)
        params: dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_display_value": "true",
            "sysparm_fields": "sys_id,number,short_description,topic,article_type,published,workflow_state,text,author",
        }
        if query and query.strip():
            clean_q = query.strip()
            if "=" in clean_q or "^" in clean_q:
                params["sysparm_query"] = clean_q
            else:
                params["sysparm_query"] = f"short_descriptionLIKE{clean_q}^ORtextLIKE{clean_q}"

        raw = self._api_request("/api/now/table/kb_knowledge", params=params)
        raw_items = raw.get("result", [])
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []

        articles = []
        for row in raw_items:
            articles.append({
                "sys_id": self._extract_display_value(row.get("sys_id")),
                "number": self._extract_display_value(row.get("number")),
                "short_description": self._extract_display_value(row.get("short_description")),
                "topic": self._extract_display_value(row.get("topic")),
                "published": self._extract_display_value(row.get("published")),
                "workflow_state": self._extract_display_value(row.get("workflow_state")),
                "author": self._extract_display_value(row.get("author")),
                "snippet": (self._extract_display_value(row.get("text")) or "")[:300],
            })

        return {
            "articles": articles,
            "count": len(articles),
            "result": articles,
        }


def get_servicenow_connector() -> ServiceNowConnector:
    """Factory function returning a ServiceNowConnector instance."""
    return ServiceNowConnector()
