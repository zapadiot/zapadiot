"""Validated requests for the Picklebet missing consumer fixture workflow.

Sportcast hosts and API keys are runtime inputs. This module does not embed
them, and it refuses mutating calls unless the caller passes confirm=True.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

ASSIGNEE_ID = "712020:e626a9d2-bf67-4741-a80b-3a018547cce4"
AUTOMATION_RULE_ID = "019f45bf-fbf0-7bbf-b180-ae122b0714ac"
INCIDENT_RESOLUTION = "Workaround Applied"
RESOLVING_TEAM = "Sportsbook Support"
RESOLVED_COMMENT = (
    "Thanks for raising {issue_key}. Please check . "
    "If the issue persists please reopen the ticket."
)
REOPEN_OK_COMMENT = "Resolving"

AU_BASE_ENV = "SPORTCAST_AU_BASE"
EU_BASE_ENV = "SPORTCAST_EU_BASE"
INTERNAL_BASE_ENV = "SPORTCAST_INTERNAL_BASE"
ACCOUNT_KEY_ENV = "SPORTCAST_ACCOUNT_KEY"

REGION_BASE_ENVS = {
    "PROD_AU": AU_BASE_ENV,
    "PROD_EU": EU_BASE_ENV,
}

UPDATE_PATH = "/api/UpdateConsumerFixtureid"
MATCH_STATE_PATH = "/api/AddOrUpdateFixtureMatchStateList"
GET_CLIENT_PATH = "/api/getclient"

_PERSIST_MARKERS = (
    "still",
    "persist",
    "persists",
    "persistent",
    "not fixed",
    "isn't fixed",
    "isnt fixed",
    "not working",
    "doesn't work",
    "does not work",
    "broken",
    "same issue",
    "reopen",
    "unable",
    "failed",
    "wrong",
)
_RESOLVED_MARKERS = (
    "resolved",
    "fixed",
    "working now",
    "works now",
    "all good",
    "sorted",
    "no longer",
    "confirmed",
)
_BARE_OK = {"ok", "okay", "resolved", "fixed", "all good", "sorted"}


class ConfirmationRequired(RuntimeError):
    """A mutating Sportcast or Jira action was requested without confirmation."""


class WorkflowError(RuntimeError):
    """The workflow cannot continue with the data it was given."""


class SportcastError(WorkflowError):
    """A Sportcast HTTP call failed."""


def redact(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    """Remove secret values from text that might be logged or raised."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def branch_for_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "under investigation":
        return "under_investigation"
    if normalized == "reopened":
        return "reopened"
    return "fallback"


def coerce_feed_providers(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    raise WorkflowError("feedProviders must be a boolean")


def _require_text(value: Any, name: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise WorkflowError(f"{name} is required")
    return text


def _coerce_fixture_id(value: Any) -> int | str:
    text = _require_text(value, "FixtureId")
    if text.isdigit():
        return int(text)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-")
    if any(character not in allowed for character in text):
        raise WorkflowError("FixtureId has an unexpected shape")
    return text


def _coerce_consumer_id(value: Any) -> str:
    text = _require_text(value, "consumer fixture id")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:._-")
    if any(character not in allowed for character in text):
        raise WorkflowError("consumer fixture id has an unexpected shape")
    return text


def consumer_search_term(client_fixture_id: Any, feed_providers: Any) -> str:
    """Return the Sportcast search value for a Picklebet fixture id."""
    client_id = _require_text(client_fixture_id, "client_fixture_id")
    if coerce_feed_providers(feed_providers):
        return client_id
    if client_id.startswith("sr:match:"):
        return client_id
    return f"sr:match:{client_id}"


def _field_map(issue: Mapping[str, Any], names: Mapping[str, str] | None) -> dict[str, Any]:
    if "fields" in issue and isinstance(issue["fields"], Mapping):
        raw = dict(issue["fields"])
    else:
        raw = dict(issue)
    if not names:
        return raw
    lifted = dict(raw)
    for field_id, display_name in names.items():
        if field_id in raw and display_name not in lifted:
            lifted[display_name] = raw[field_id]
    return lifted


def _value_of(field_value: Any) -> Any:
    if isinstance(field_value, Mapping):
        for key in ("value", "name", "id"):
            if key in field_value and field_value[key] not in (None, ""):
                return field_value[key]
    return field_value


def extract_custom_fields(
    issue: Mapping[str, Any],
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read the four workflow fields from a Jira issue payload.

    `names` maps Jira custom field ids (customfield_12345) to display names.
    """
    fields = _field_map(issue, names)
    client_fixture_id = _require_text(
        _value_of(fields.get("client_fixture_id")),
        "client_fixture_id",
    )
    fixture_id = _coerce_fixture_id(_value_of(fields.get("FixtureId")))
    api_key = _require_text(_value_of(fields.get("apiKey")), "apiKey")
    feed_providers = coerce_feed_providers(_value_of(fields.get("feedProviders")))
    return {
        "client_fixture_id": client_fixture_id,
        "FixtureId": fixture_id,
        "apiKey": api_key,
        "feedProviders": feed_providers,
    }


def redacted_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    hidden = redact(
        json.dumps({"apiKey": fields.get("apiKey", "")}),
        [str(fields.get("apiKey", ""))],
    )
    visible = {key: value for key, value in fields.items() if key != "apiKey"}
    visible["apiKey"] = json.loads(hidden)["apiKey"]
    return visible


def _base_url(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip().rstrip("/")
    if not value:
        raise WorkflowError(f"{env_name} is not set")
    if not value.startswith("https://"):
        raise WorkflowError(f"{env_name} must be an https URL")
    return value


def normalize_region(value: Any) -> str:
    text = "" if value is None else str(value).strip().upper()
    if not text:
        raise WorkflowError("MessagingRegion is missing")
    if not re.fullmatch(r"[A-Z0-9_]+", text):
        raise WorkflowError("MessagingRegion has an unexpected shape")
    return text


def region_base_url(region: Any) -> str:
    """Return the configured base URL for a client MessagingRegion."""
    normalized = normalize_region(region)
    env_name = REGION_BASE_ENVS.get(normalized)
    if env_name is None:
        supported = ", ".join(sorted(REGION_BASE_ENVS))
        raise WorkflowError(
            f"unsupported MessagingRegion {normalized}; expected one of {supported}"
        )
    return _base_url(env_name)


def get_client_url(api_key: str) -> str:
    key = _require_text(api_key, "apiKey")
    query = urlencode({"key": key, "Connections": "true"})
    return f"{_base_url(INTERNAL_BASE_ENV)}{GET_CLIENT_PATH}?{query}"


def update_consumer_fixture_url(
    fixture_id: Any,
    consumer_fixture_id: Any,
    *,
    region: Any,
) -> str:
    fixture = _coerce_fixture_id(fixture_id)
    consumer = _coerce_consumer_id(consumer_fixture_id)
    query = urlencode(
        {
            "fixtureId": fixture,
            "consumerfixtureid": consumer,
        }
    )
    return f"{region_base_url(region)}{UPDATE_PATH}?{query}"


def match_state_payload(
    fixture_id: Any,
    api_key: str,
    match_state: int,
    account_key: str | None = None,
) -> dict[str, Any]:
    if match_state not in (1, 2):
        raise WorkflowError("MatchState must be 1 (Live) or 2 (Settled)")
    item_key = _require_text(api_key, "apiKey")
    outer_key = _require_text(account_key or os.environ.get(ACCOUNT_KEY_ENV) or item_key, "apiKey")
    return {
        "Key": outer_key,
        "Items": [
            {
                "Key": item_key,
                "FixtureId": _coerce_fixture_id(fixture_id),
                "MatchState": match_state,
            }
        ],
    }


def _require_confirm(confirm: bool, action: str) -> None:
    if not confirm:
        raise ConfirmationRequired(f"{action} needs explicit confirmation")


class _Response:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body


def _default_request(method: str, url: str, payload: dict[str, Any] | None, secrets: list[str]) -> _Response:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return _Response(response.status, raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SportcastError(
            redact(f"Sportcast returned HTTP {exc.code}: {raw[:200]}", secrets)
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(redact(str(exc.reason if isinstance(exc, urllib.error.URLError) else exc), secrets)) from None


def _with_network_retry(operation: Callable[[], _Response]) -> _Response:
    try:
        return operation()
    except ConnectionError:
        return operation()


def _ensure_ok(response: _Response, secrets: list[str], action: str) -> _Response:
    if response.status != 200:
        detail = redact(response.body[:200], secrets)
        raise SportcastError(f"{action} returned HTTP {response.status}: {detail}")
    return response


def _as_response(result: Any) -> _Response:
    if isinstance(result, _Response):
        return result
    if isinstance(result, int):
        return _Response(result, "")
    status = int(getattr(result, "status", result))
    body = getattr(result, "body", "")
    if not isinstance(body, str):
        body = json.dumps(body)
    return _Response(status, body)


def client_region(
    api_key: str,
    *,
    http_get: Callable[[str], Any] | None = None,
) -> str:
    """Read the client's MessagingRegion from getclient. Read-only."""
    key = _require_text(api_key, "apiKey")
    url = get_client_url(key)
    secrets = [key]

    def once() -> _Response:
        if http_get is None:
            return _default_request("GET", url, None, secrets)
        return _as_response(http_get(url))

    try:
        response = _ensure_ok(_with_network_retry(once), secrets, "getclient")
    except ConnectionError as exc:
        raise SportcastError(redact(str(exc), secrets)) from None
    try:
        body = json.loads(response.body or "")
    except ValueError:
        raise SportcastError("getclient returned a body that is not JSON") from None
    if not isinstance(body, Mapping):
        raise SportcastError("getclient returned an unexpected body")
    return normalize_region(body.get("MessagingRegion"))


def update_consumer_fixture_id(
    fixture_id: Any,
    consumer_fixture_id: Any,
    *,
    region: Any,
    confirm: bool = False,
    http_get: Callable[[str], Any] | None = None,
) -> _Response:
    """GET the consumer-fixture update endpoint for a region. Requires confirm=True."""
    _require_confirm(confirm, "UpdateConsumerFixtureid")
    fixture = _coerce_fixture_id(fixture_id)
    consumer = _coerce_consumer_id(consumer_fixture_id)
    url = update_consumer_fixture_url(fixture, consumer, region=region)

    def once() -> _Response:
        if http_get is None:
            return _default_request("GET", url, None, [])
        return _as_response(http_get(url))

    return _ensure_ok(_with_network_retry(once), [], "UpdateConsumerFixtureid")


def set_match_state(
    fixture_id: Any,
    api_key: str,
    match_state: int,
    *,
    confirm: bool = False,
    account_key: str | None = None,
    http_post: Callable[[str, dict[str, Any]], Any] | None = None,
) -> _Response:
    """POST a Live (1) or Settled (2) match state. Requires confirm=True."""
    _require_confirm(confirm, "AddOrUpdateFixtureMatchStateList")
    payload = match_state_payload(fixture_id, api_key, match_state, account_key)
    secrets = [payload["Key"], payload["Items"][0]["Key"]]
    url = f"{_base_url(INTERNAL_BASE_ENV)}{MATCH_STATE_PATH}"

    def once() -> _Response:
        if http_post is None:
            return _default_request("POST", url, payload, secrets)
        return _as_response(http_post(url, payload))

    try:
        response = _ensure_ok(_with_network_retry(once), secrets, "AddOrUpdateFixtureMatchStateList")
    except SportcastError:
        raise
    except ConnectionError as exc:
        raise SportcastError(redact(str(exc), secrets)) from None
    return response


def wait_seconds(seconds: float, sleep: Callable[[float], None] | None = None) -> None:
    if seconds < 0:
        raise WorkflowError("wait seconds must be zero or greater")
    (sleep or time.sleep)(seconds)


def transition_jira_resolved(issue_key: str, comment: str | None = None) -> dict[str, Any]:
    """Plan a Resolved transition. Applying it is an MCP call, not this function."""
    key = _require_text(issue_key, "issue_key")
    return {
        "issue_key": key,
        "transition": "Resolved",
        "comment": comment if comment is not None else RESOLVED_COMMENT.format(issue_key=key),
        "fields": {
            "Incident Resolution": INCIDENT_RESOLUTION,
            "Resolving Team": RESOLVING_TEAM,
        },
    }


def transition_jira_under_investigation(issue_key: str) -> dict[str, Any]:
    key = _require_text(issue_key, "issue_key")
    return {
        "issue_key": key,
        "transition": "Under investigation",
    }


def assign_jira_issue(issue_key: str, assignee_id: str = ASSIGNEE_ID) -> dict[str, Any]:
    key = _require_text(issue_key, "issue_key")
    assignee = _require_text(assignee_id, "assignee_id")
    return {
        "issue_key": key,
        "assignee_id": assignee,
    }


def _has_marker(text: str, marker: str) -> bool:
    pattern = r"\b" + re.escape(marker).replace(r"\ ", r"\s+") + r"\b"
    return re.search(pattern, text) is not None


def analyze_reopen_comment(comment: str) -> str:
    """Return OK when the customer confirms the fix, otherwise NEEDS_REVIEW."""
    text = (comment or "").strip()
    if not text:
        return "NEEDS_REVIEW"
    lowered = text.lower().rstrip(".!")
    if lowered in _BARE_OK:
        return "OK"
    persist = any(_has_marker(lowered, marker) for marker in _PERSIST_MARKERS)
    resolved = any(_has_marker(lowered, marker) for marker in _RESOLVED_MARKERS)
    if resolved and not persist:
        return "OK"
    return "NEEDS_REVIEW"


def reopen_plan(issue_key: str, decision: str) -> dict[str, Any]:
    key = _require_text(issue_key, "issue_key")
    if decision == "OK":
        return {
            "action": "resolve",
            "issue_key": key,
            "comment": REOPEN_OK_COMMENT,
            "transition": "Resolved",
            "resolution": "Fixed",
            "fields": {
                "Incident Resolution": INCIDENT_RESOLUTION,
            },
        }
    if decision == "NEEDS_REVIEW":
        plan = transition_jira_under_investigation(key)
        plan["action"] = "investigate"
        plan["assignment"] = assign_jira_issue(key)
        return plan
    raise WorkflowError("reopen decision must be OK or NEEDS_REVIEW")


def _issue_key(issue: Mapping[str, Any]) -> str:
    key = issue.get("key") or issue.get("issue_key")
    return _require_text(key, "issue_key")


def _search_resolved_id(search_term: str, search_fn: Callable[[str], Any]) -> str:
    found = search_fn(search_term)
    if isinstance(found, Mapping):
        found = found.get("resolvedFixtureId") or found.get("id")
    return _coerce_consumer_id(found)


def run_under_investigation(
    issue: Mapping[str, Any],
    *,
    confirm: bool,
    search_fn: Callable[[str], Any],
    names: Mapping[str, str] | None = None,
    http_get: Callable[[str], Any] | None = None,
    http_post: Callable[[str, dict[str, Any]], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    account_key: str | None = None,
) -> dict[str, Any]:
    """Resolve the region, search, update, set Live then Settled, and plan Jira."""
    _require_confirm(confirm, "Under investigation workflow")
    fields = extract_custom_fields(issue, names)
    region = client_region(fields["apiKey"], http_get=http_get)
    # Stop on an unsupported region or a missing base URL before searching.
    region_base_url(region)
    search_term = consumer_search_term(fields["client_fixture_id"], fields["feedProviders"])
    resolved = _search_resolved_id(search_term, search_fn)
    update = update_consumer_fixture_id(
        fields["FixtureId"],
        resolved,
        region=region,
        confirm=True,
        http_get=http_get,
    )
    set_match_state(
        fields["FixtureId"],
        fields["apiKey"],
        1,
        confirm=True,
        account_key=account_key,
        http_post=http_post,
    )
    wait_seconds(3, sleep=sleep)
    set_match_state(
        fields["FixtureId"],
        fields["apiKey"],
        2,
        confirm=True,
        account_key=account_key,
        http_post=http_post,
    )
    issue_key = _issue_key(issue)
    return {
        "action": "resolved",
        "issue_key": issue_key,
        "region": region,
        "searchTerm": search_term,
        "resolvedFixtureId": resolved,
        "updateStatus": update.status,
        "matchStates": [1, 2],
        "jira": transition_jira_resolved(issue_key),
        "fields": redacted_fields(fields),
    }


def run_reopened(
    issue: Mapping[str, Any],
    comment: str,
    *,
    confirm: bool,
    analyze_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    _require_confirm(confirm, "Reopened workflow")
    decision = (analyze_fn or analyze_reopen_comment)(comment)
    if decision not in {"OK", "NEEDS_REVIEW"}:
        decision = "NEEDS_REVIEW"
    plan = reopen_plan(_issue_key(issue), decision)
    plan["decision"] = decision
    return plan


def run_issue(
    issue: Mapping[str, Any],
    *,
    status: str,
    confirm: bool = False,
    comment: str = "",
    search_fn: Callable[[str], Any] | None = None,
    names: Mapping[str, str] | None = None,
    http_get: Callable[[str], Any] | None = None,
    http_post: Callable[[str, dict[str, Any]], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    analyze_fn: Callable[[str], str] | None = None,
    account_key: str | None = None,
) -> dict[str, Any]:
    """Dispatch on Jira status. Unknown statuses return the manual fallback."""
    branch = branch_for_status(status)
    if branch == "fallback":
        return {
            "action": "todo",
            "issue_key": issue.get("key") or issue.get("issue_key"),
            "message": "todo",
        }
    if branch == "reopened":
        return run_reopened(issue, comment, confirm=confirm, analyze_fn=analyze_fn)
    if search_fn is None:
        raise WorkflowError("search_fn is required for Under investigation")
    return run_under_investigation(
        issue,
        confirm=confirm,
        search_fn=search_fn,
        names=names,
        http_get=http_get,
        http_post=http_post,
        sleep=sleep,
        account_key=account_key,
    )
