"""Optional Jira Cloud REST calls. Used only when JIRA_* credentials are set."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any

BASE_ENV = "JIRA_BASE_URL"
EMAIL_ENV = "JIRA_EMAIL"
TOKEN_ENV = "JIRA_API_TOKEN"

INCIDENT_RESOLUTION_FIELD = "customfield_10688"
RESOLUTION_NOTES_FIELD = "customfield_10518"


class JiraError(RuntimeError):
    pass


def configured() -> bool:
    return all(os.environ.get(name) for name in (BASE_ENV, EMAIL_ENV, TOKEN_ENV))


def _request(method: str, path: str, body: Any = None) -> Any:
    base = os.environ[BASE_ENV].rstrip("/")
    auth = base64.b64encode(f"{os.environ[EMAIL_ENV]}:{os.environ[TOKEN_ENV]}".encode()).decode()
    request = urllib.request.Request(
        f"{base}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Basic {auth}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise JiraError(f"{method} {path} returned HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JiraError(f"{method} {path} failed: {getattr(exc, 'reason', exc)}") from None


def get_issue(key: str) -> dict[str, Any]:
    fields = "summary,status,description,comment,issuelinks,customfield_11360,customfield_10651,customfield_10002"
    return _request("GET", f"/rest/api/2/issue/{key}?fields={fields}")


def apply_plan(plan: dict[str, Any]) -> str:
    """Apply a resolve or investigate plan from workflow.jira_plan()."""
    key = plan["issue_key"]
    transitions = _request("GET", f"/rest/api/2/issue/{key}/transitions").get("transitions", [])
    wanted = plan["transition"].lower()
    match = next((t for t in transitions if t.get("name", "").lower() == wanted
                  or t.get("to", {}).get("name", "").lower() == wanted), None)
    if match is None:
        names = ", ".join(t.get("name", "") for t in transitions)
        raise JiraError(f"no '{plan['transition']}' transition on {key}; available: {names}")
    body: dict[str, Any] = {"transition": {"id": match["id"]}}
    fields: dict[str, Any] = {}
    if plan.get("incident_resolution"):
        fields[INCIDENT_RESOLUTION_FIELD] = {"value": plan["incident_resolution"]}
    if plan.get("resolution_notes"):
        fields[RESOLUTION_NOTES_FIELD] = plan["resolution_notes"]
    if plan.get("resolution"):
        fields["resolution"] = {"name": plan["resolution"]}
    if fields:
        body["fields"] = fields
    if plan.get("comment"):
        body["update"] = {"comment": [{"add": {"body": plan["comment"]}}]}
    _request("POST", f"/rest/api/2/issue/{key}/transitions", body)
    if plan.get("assignee_id"):
        _request("PUT", f"/rest/api/2/issue/{key}/assignee", {"accountId": plan["assignee_id"]})
    return f"{key} moved to {plan['transition']}"
