"""Shared helper for turning a failed connector API call's JSON error body
into a human-readable detail string, so a 401/403 can say *why* ("no mailbox
license", "insufficient permissions", "API not enabled") instead of just the
generic HTTP status - used by every connector client that talks to Google,
Microsoft Graph, or GitHub via urllib.
"""
import json
import urllib.error


def extract_http_error_detail(exc: urllib.error.HTTPError, max_len: int = 300) -> str:
    """Best-effort human-readable detail from a failed request's JSON error
    body - covers Google's {"error": {"message": ...}}, Microsoft Graph's
    {"error": {"code", "message"}}, and GitHub's {"message": ...} shapes.
    Returns "" if the body is empty or carries nothing useful, so callers can
    fall back to their own generic wording (Microsoft Graph in particular
    often returns a genuinely empty 401 body for mailbox-less accounts)."""
    try:
        raw = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:max_len]
    if not isinstance(body, dict):
        return raw[:max_len]
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error_description")
        if msg:
            return str(msg)[:max_len]
    elif isinstance(err, str) and err:
        return str(body.get("error_description") or err)[:max_len]
    msg = body.get("message")
    if msg:
        return str(msg)[:max_len]
    return raw[:max_len]
