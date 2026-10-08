"""Read the parts of a TSD Jira issue that the SGM repush workflow needs.

Accepts the Jira REST shape ({"key", "fields": {...}}) and the flat shape the
Cursor Jira trigger sends ({"key", "summary", "status", "description", ...}).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

OPERATOR_FIELD = "customfield_11360"
CATEGORY_FIELD = "customfield_10651"
ORGANIZATIONS_FIELD = "customfield_10002"

# Assets object ids on Operator/s (TSD) mapped to the Sportcast client name.
OPERATOR_CLIENTS = {
    "46193": "Picklebet",
}

TEST_MARKERS = ("do not touch", "for test purposes", "test ticket")

_FIXTURE_ID = re.compile(r"Sportcast\s+Fixture\s+ID\s*[:#]?\s*(\d{4,9})", re.I)
_CLIENT_ID = re.compile(r"Client\s+Fixture\s+ID\s*[:#]?\s*([A-Za-z0-9:._-]+)", re.I)
_INFO = re.compile(r"Fixture\s+Info\s*[:#]?\s*([^\n|]+)", re.I)


@dataclass
class RequestedFixture:
    fixture_id: int
    client_fixture_id: str = ""
    info: str = ""


@dataclass
class Ticket:
    key: str
    summary: str
    status: str
    description: str
    category: str
    client_name: str
    fixtures: list[RequestedFixture]
    comments: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_test(self) -> bool:
        text = self.summary.lower()
        return any(marker in text for marker in TEST_MARKERS)

    @property
    def is_repush(self) -> bool:
        return "repush" in self.category.lower() or "sgm" in self.summary.lower()


def _text(value: Any) -> str:
    """Flatten wiki text, HTML, or Atlassian document format into plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = re.sub(r"<br\s*/?>|</p>|</tr>", "\n", value, flags=re.I)
        return re.sub(r"<[^>]+>", "", text)
    if isinstance(value, Mapping):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        if value.get("type") == "hardBreak":
            return "\n"
        inner = "".join(_text(child) for child in value.get("content", []) or [])
        if value.get("type") in {"paragraph", "heading", "listItem", "tableRow"}:
            inner += "\n"
        return inner
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    return str(value)


def _name(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("name", "value", "displayName"):
            if value.get(key):
                return str(value[key])
    return "" if value is None else str(value)


def parse_fixtures(description: str) -> list[RequestedFixture]:
    """Split the description into one block per 'Sportcast Fixture ID' line."""
    starts = [match.start() for match in _FIXTURE_ID.finditer(description)]
    fixtures: list[RequestedFixture] = []
    seen: set[int] = set()
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(description)
        block = description[start:end]
        fixture_id = int(_FIXTURE_ID.search(block).group(1))
        if fixture_id in seen:
            continue
        seen.add(fixture_id)
        client = _CLIENT_ID.search(block)
        info = _INFO.search(block)
        fixtures.append(
            RequestedFixture(
                fixture_id=fixture_id,
                client_fixture_id=client.group(1).strip() if client else "",
                info=info.group(1).strip() if info else "",
            )
        )
    return fixtures


def _category(fields: Mapping[str, Any]) -> str:
    raw = fields.get(CATEGORY_FIELD) or fields.get("Category (TSD)")
    if isinstance(raw, Mapping):
        parent = _name(raw)
        child = _name(raw.get("child"))
        return f"{parent} > {child}" if child else parent
    return _name(raw)


def _client_name(fields: Mapping[str, Any]) -> str:
    operators = fields.get(OPERATOR_FIELD) or fields.get("Operator/s (TSD)") or []
    for operator in operators if isinstance(operators, list) else [operators]:
        if isinstance(operator, Mapping):
            object_id = str(operator.get("objectId") or str(operator.get("id", "")).split(":")[-1])
            if object_id in OPERATOR_CLIENTS:
                return OPERATOR_CLIENTS[object_id]
            if operator.get("label"):
                return str(operator["label"])
    organizations = fields.get(ORGANIZATIONS_FIELD) or fields.get("Organizations") or []
    for organization in organizations if isinstance(organizations, list) else [organizations]:
        name = _name(organization)
        if name:
            return name
    return ""


def _comments(fields: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = fields.get("comment") or fields.get("comments") or []
    if isinstance(raw, Mapping):
        raw = raw.get("comments", [])
    comments = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        author = item.get("author") or {}
        comments.append(
            {
                "author": _name(author) or str(author.get("display_name", "")),
                "email": str(author.get("emailAddress") or author.get("email") or ""),
                "created": str(item.get("created", "")),
                "body": _text(item.get("body")),
            }
        )
    return comments


def _unwrap(value: Any) -> Any:
    if isinstance(value, Mapping) and set(value) == {"value"}:
        return value["value"]
    return value


def parse_issue(issue: Mapping[str, Any]) -> Ticket:
    fields: Mapping[str, Any] = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else issue
    fields = {name: _unwrap(value) for name, value in fields.items()}
    custom = fields.get("customFields")
    if isinstance(custom, Mapping):
        merged = dict(fields)
        for display, entry in custom.items():
            if isinstance(entry, Mapping) and "id" in entry:
                merged.setdefault(entry["id"], entry.get("value"))
                merged.setdefault(display, entry.get("value"))
        fields = merged
    description = _text(fields.get("description"))
    key = str(issue.get("key") or issue.get("issue_key") or "").strip()
    if not key:
        raise ValueError("issue key is missing from the payload")
    return Ticket(
        key=key,
        summary=_name(fields.get("summary")),
        status=_name(fields.get("status")),
        description=description,
        category=_category(fields),
        client_name=_client_name(fields),
        fixtures=parse_fixtures(description),
        comments=_comments(fields),
    )
