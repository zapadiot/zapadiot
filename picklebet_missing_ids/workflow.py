"""Picklebet SGM repush / missing consumer fixture id workflow.

The successful path for these TSD tickets:
  1. read the Sportcast fixture ids from the description,
  2. check each fixture in Sportcast (teams, kickoff, Betradar consumer id,
     whether the client follows Betbuilder),
  3. add the Betradar consumer id when it is missing,
  4. republish the fixture,
  5. confirm SinglesCreated reached the client's production endpoint,
  6. tell the customer and resolve the ticket.

Sportcast writes and Jira transitions only happen with apply=True, never on
test tickets, and never when a fixture needs a human decision.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from picklebet_missing_ids.ticket import RequestedFixture, Ticket

ASSIGNEE_ID = "712020:e626a9d2-bf67-4741-a80b-3a018547cce4"
AUTOMATION_RULE_ID = "019f45bf-fbf0-7bbf-b180-ae122b0714ac"
INCIDENT_RESOLUTION = "Workaround Applied"
REOPEN_OK_COMMENT = "Resolving"
MARKER_PREFIX = "sgm-agent"

# Verdicts. The first two are the only ones the agent acts on by itself.
ADD_ID_AND_REPUBLISH = "add_id_and_republish"
REPUBLISH = "republish"
KICKED_OFF = "kicked_off"
IN_PLAY = "in_play"
ID_MISMATCH = "id_mismatch"
TEAMS_MISMATCH = "teams_mismatch"
CLIENT_NOT_FOLLOWING = "client_not_following"
NOT_FOUND = "not_found"
UNKNOWN_SOURCE = "unknown_source"
ACTIONABLE = {ADD_ID_AND_REPUBLISH, REPUBLISH}

_PERSIST_MARKERS = (
    "still", "persist", "persists", "persistent", "not fixed", "isn't fixed", "isnt fixed",
    "not working", "doesn't work", "does not work", "broken", "same issue", "reopen",
    "unable", "failed", "wrong", "missing", "not available", "not showing",
)
_RESOLVED_MARKERS = (
    "resolved", "fixed", "working now", "works now", "all good", "sorted", "no longer",
    "confirmed", "available now", "showing now",
)
_BARE_OK = {"ok", "okay", "resolved", "fixed", "all good", "sorted", "thanks", "thank you"}


class Sportcast(Protocol):
    def get_fixture(self, fixture_id: int) -> dict[str, Any]: ...
    def consumer_id(self, fixture_id: int, source: str = "BetRadar") -> str: ...
    def add_consumer_id(self, fixture_id: int, feed_id: str, source: str = "BetRadar") -> str: ...
    def republish(self, definition: dict[str, Any]) -> None: ...


@dataclass
class Finding:
    fixture_id: int
    requested_id: str
    info: str
    verdict: str = NOT_FOUND
    teams: str = ""
    kickoff: datetime | None = None
    betradar_id: str = ""
    client_following: bool | None = None
    client_last_published: str = ""
    notes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)


@dataclass
class Result:
    ticket: Ticket
    branch: str
    findings: list[Finding] = field(default_factory=list)
    applied: bool = False
    blockers: list[str] = field(default_factory=list)
    jira: dict[str, Any] | None = None

    @property
    def marker(self) -> str:
        state = "|".join(
            f"{f.fixture_id}:{f.verdict}:{f.betradar_id}:{len(f.delivered)}" for f in self.findings
        )
        digest = hashlib.sha1(f"{self.ticket.status}|{state}|{self.blockers}".encode()).hexdigest()[:10]
        return f"{MARKER_PREFIX}:{self.branch}:{digest}"


def branch_for_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "under investigation":
        return "under_investigation"
    if normalized == "reopened":
        return "reopened"
    return "fallback"


def betradar_feed_id(client_fixture_id: str) -> str:
    """Return the Betradar consumer id for a client fixture id, or '' if it is not Betradar."""
    value = (client_fixture_id or "").strip()
    if value.startswith("sr:match:") and value[9:].isdigit():
        return value
    if value.isdigit() and len(value) >= 7:
        return f"sr:match:{value}"
    return ""


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _teams_match(info: str, home: str, away: str) -> bool:
    if not info:
        return True
    wanted = _norm(info)
    return bool(home and away) and _norm(home) in wanted and _norm(away) in wanted


def diagnose(
    requested: RequestedFixture,
    definition: dict[str, Any] | None,
    betradar_id: str,
    client_name: str,
    now: datetime,
) -> Finding:
    finding = Finding(requested.fixture_id, requested.client_fixture_id, requested.info)
    if not definition or not definition.get("fixtureId"):
        finding.notes.append("Sportcast has no fixture with this id.")
        return finding

    home = (definition.get("homeTeam") or {}).get("team", {}).get("name", "")
    away = (definition.get("awayTeam") or {}).get("team", {}).get("name", "")
    details = definition.get("details") or {}
    finding.teams = f"{home} v {away}"
    finding.kickoff = _parse_time(details.get("startTimeUTC"))
    finding.betradar_id = betradar_id

    client = next(
        (c for c in details.get("betBuilderClients") or []
         if (c.get("clientName") or "").lower() == client_name.lower()),
        None,
    )
    if client is not None:
        finding.client_following = bool(client.get("betBuilderFollowing"))
        finding.client_last_published = str(client.get("lastPublished") or "")

    wanted = betradar_feed_id(requested.client_fixture_id)
    if requested.client_fixture_id and not wanted:
        finding.verdict = UNKNOWN_SOURCE
        finding.notes.append(f"Client fixture id {requested.client_fixture_id} is not a Betradar id.")
    elif not _teams_match(requested.info, home, away):
        finding.verdict = TEAMS_MISMATCH
        finding.notes.append(f"Ticket says '{requested.info}', Sportcast has '{finding.teams}'.")
    elif definition.get("isFixtureInplay"):
        finding.verdict = IN_PLAY
        finding.notes.append("The fixture is in play.")
    elif finding.kickoff is not None and finding.kickoff <= now:
        finding.verdict = KICKED_OFF
        finding.notes.append(f"Kickoff was {finding.kickoff:%Y-%m-%d %H:%M} UTC; there is nothing to repush.")
    elif client is None or not finding.client_following:
        finding.verdict = CLIENT_NOT_FOLLOWING
        finding.notes.append(f"{client_name or 'The client'} is not following Betbuilder on this fixture.")
    elif wanted and betradar_id and betradar_id != wanted:
        finding.verdict = ID_MISMATCH
        finding.notes.append(f"Sportcast already has Betradar id {betradar_id}; the ticket has {wanted}.")
    elif wanted and not betradar_id:
        finding.verdict = ADD_ID_AND_REPUBLISH
        finding.notes.append(f"Betradar consumer id is missing; {wanted} will be added.")
    else:
        finding.verdict = REPUBLISH
        finding.notes.append("Mapping is present; the fixture will be republished.")
    return finding


def analyze_reopen_comment(comment: str) -> str:
    """Return OK when the customer confirms the fix, otherwise NEEDS_REVIEW."""
    lowered = (comment or "").strip().lower().rstrip(".!")
    if not lowered:
        return "NEEDS_REVIEW"
    if lowered in _BARE_OK:
        return "OK"

    def has(marker: str) -> bool:
        return re.search(r"\b" + re.escape(marker).replace(r"\ ", r"\s+") + r"\b", lowered) is not None

    if any(has(m) for m in _RESOLVED_MARKERS) and not any(has(m) for m in _PERSIST_MARKERS):
        return "OK"
    return "NEEDS_REVIEW"


def latest_customer_comment(ticket: Ticket) -> str:
    for comment in reversed(ticket.comments):
        email = comment.get("email", "").lower()
        if email and not email.endswith("@openbet.com"):
            return comment.get("body", "")
    return ""


def customer_comment(findings: list[Finding]) -> str:
    lines = ["Hello,", "", "SGM for the requested fixtures is now available:", ""]
    for f in findings:
        requested = f" / {f.requested_id}" if f.requested_id else ""
        lines.append(f"- {f.teams} (Sportcast {f.fixture_id}{requested})")
    lines += ["", "Please reopen this ticket if any fixture is still missing.", "", "Regards"]
    return "\n".join(lines)


def resolution_notes(findings: list[Finding]) -> str:
    added = [f for f in findings if f.verdict == ADD_ID_AND_REPUBLISH]
    parts = []
    if added:
        parts.append(
            "Mapped the missing Betradar consumer id for "
            + ", ".join(f"{f.teams} ({f.fixture_id} / {betradar_feed_id(f.requested_id)})" for f in added)
        )
    parts.append(f"republished {len(findings)} requested fixture(s); SinglesCreated delivered to production.")
    text = " and ".join(parts)
    return text[0].upper() + text[1:]


def jira_resolve_plan(ticket: Ticket, findings: list[Finding]) -> dict[str, Any]:
    return {
        "issue_key": ticket.key,
        "transition": "Resolved",
        "comment": customer_comment(findings),
        "incident_resolution": INCIDENT_RESOLUTION,
        "resolution_notes": resolution_notes(findings),
    }


def jira_reopen_plan(ticket: Ticket, decision: str) -> dict[str, Any]:
    if decision == "OK":
        return {
            "issue_key": ticket.key,
            "transition": "Resolved",
            "comment": REOPEN_OK_COMMENT,
            "resolution": "Fixed",
            "incident_resolution": INCIDENT_RESOLUTION,
        }
    return {"issue_key": ticket.key, "transition": "Under investigation", "assignee_id": ASSIGNEE_ID}


def _betradar_from_definition(definition: dict[str, Any]) -> str:
    for item in (definition.get("details") or {}).get("consumerfixtureIdList") or []:
        if item.get("sourceName") == "BetRadar" and item.get("consumerFixtureId"):
            return str(item["consumerFixtureId"])
    return ""


def run(
    ticket: Ticket,
    sportcast: Sportcast | None,
    *,
    apply: bool = False,
    now: datetime | None = None,
    verify: Callable[[int, str, str], list[str]] | None = None,
    verify_timeout: float = 180,
    poll_every: float = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Diagnose the ticket and, when allowed, fix and verify it.

    verify(fixture_id, client_name, since_iso) returns production delivery
    timestamps, or raises when delivery cannot be checked.
    """
    now = now or datetime.now(timezone.utc)
    result = Result(ticket=ticket, branch=branch_for_status(ticket.status))

    if result.branch == "fallback":
        result.blockers.append(f"Status is '{ticket.status}'; only Under investigation and Reopened are handled.")
        return result

    if result.branch == "reopened":
        decision = analyze_reopen_comment(latest_customer_comment(ticket))
        result.jira = jira_reopen_plan(ticket, decision)
        result.jira["decision"] = decision
        return result

    if not ticket.fixtures:
        result.blockers.append("No 'Sportcast Fixture ID' found in the description.")
        return result
    if not ticket.client_name:
        result.blockers.append("Operator/s (TSD) and Organizations do not name a Sportcast client.")
        return result
    if sportcast is None:
        result.blockers.append("Sportcast is not reachable from this run (see missing settings).")
        return result

    definitions: dict[int, dict[str, Any]] = {}
    for requested in ticket.fixtures:
        definition = sportcast.get_fixture(requested.fixture_id)
        definitions[requested.fixture_id] = definition
        betradar = sportcast.consumer_id(requested.fixture_id) if definition.get("fixtureId") else ""
        betradar = betradar or _betradar_from_definition(definition)
        result.findings.append(diagnose(requested, definition, betradar, ticket.client_name, now))

    manual = [f for f in result.findings if f.verdict not in ACTIONABLE]
    if manual and all(f.verdict == KICKED_OFF for f in result.findings):
        result.blockers.append("All requested fixtures have kicked off; nothing to repush.")
    elif manual:
        result.blockers.append("Some fixtures need a human decision; nothing was changed.")
    if ticket.is_test:
        result.blockers.append("Ticket is marked as a test ('do not touch'); read-only run.")
    if not ticket.is_repush:
        result.blockers.append(f"Category is '{ticket.category}', not Sportcast > Repush SGM.")
    if result.blockers or not apply:
        return result

    started = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for finding in result.findings:
        if finding.verdict == ADD_ID_AND_REPUBLISH:
            feed_id = betradar_feed_id(finding.requested_id)
            message = sportcast.add_consumer_id(finding.fixture_id, feed_id)
            finding.actions.append(f"Added Betradar consumer id {feed_id}: {message}")
            if sportcast.consumer_id(finding.fixture_id) != feed_id:
                result.blockers.append(f"{finding.fixture_id}: consumer id did not stick ({message}).")
                return result
        sportcast.republish(sportcast.get_fixture(finding.fixture_id))
        finding.actions.append("Republished.")
    result.applied = True

    if verify is None:
        result.blockers.append("Datadog is not configured; delivery to production was not verified.")
        return result
    waited = 0.0
    while True:
        for finding in result.findings:
            if not finding.delivered:
                finding.delivered = verify(finding.fixture_id, ticket.client_name, started)
        if all(f.delivered for f in result.findings) or waited >= verify_timeout:
            break
        sleep(poll_every)
        waited += poll_every
    missing = [str(f.fixture_id) for f in result.findings if not f.delivered]
    if missing:
        result.blockers.append(f"No production SinglesCreated delivery seen yet for {', '.join(missing)}.")
        return result

    result.jira = jira_resolve_plan(ticket, result.findings)
    return result


def report(result: Result, *, missing_settings: list[str] | None = None,
           jira_outcome: str = "") -> str:
    """Plain-text summary for the Jira comment the cloud agent posts."""
    t = result.ticket
    lines = [f"{t.key} ({t.status}) — {t.client_name or 'unknown client'}, {t.category or 'no category'}"]
    if result.branch == "fallback":
        lines.append("No action: " + "; ".join(result.blockers))
    for f in result.findings:
        kickoff = f"{f.kickoff:%Y-%m-%d %H:%M} UTC" if f.kickoff else "unknown kickoff"
        following = {True: "following", False: "not following", None: "not listed"}[f.client_following]
        lines.append("")
        lines.append(f"Fixture {f.fixture_id} — {f.teams or f.info or 'not found'} — {kickoff}")
        lines.append(f"  Betradar id: {f.betradar_id or 'missing'} (ticket: {f.requested_id or 'none'})")
        lines.append(f"  {t.client_name} Betbuilder: {following}, last published {f.client_last_published or 'never'}")
        lines.append(f"  Verdict: {f.verdict.replace('_', ' ')}")
        for note in f.notes + f.actions:
            lines.append(f"  - {note}")
        if f.delivered:
            lines.append(f"  - SinglesCreated delivered to production at {f.delivered[0]}")
    if result.jira:
        lines.append("")
        lines.append(f"Jira: {jira_outcome or 'planned'} -> {result.jira['transition']}")
    if missing_settings:
        lines.append("")
        lines.append("Missing settings for this run: " + ", ".join(missing_settings))
    if result.blockers and result.branch != "fallback":
        lines.append("")
        lines.append("Not done: " + " ".join(result.blockers))
    lines.append("")
    lines.append(f"[{result.marker}]")
    return "\n".join(lines)


def already_reported(result: Result) -> bool:
    """True when the last comment already carries this exact run marker."""
    if not result.ticket.comments:
        return False
    return f"[{result.marker}]" in result.ticket.comments[-1].get("body", "")
