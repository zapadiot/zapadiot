"""Tests for the Picklebet SGM repush workflow, modelled on TSD-388989 / TSD-389084."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from picklebet_missing_ids.datadog import parse_deliveries
from picklebet_missing_ids.ticket import parse_issue
from picklebet_missing_ids.workflow import (
    ADD_ID_AND_REPUBLISH,
    ASSIGNEE_ID,
    CLIENT_NOT_FOLLOWING,
    ID_MISMATCH,
    KICKED_OFF,
    REPUBLISH,
    TEAMS_MISMATCH,
    UNKNOWN_SOURCE,
    analyze_reopen_comment,
    already_reported,
    betradar_feed_id,
    report,
    run,
)

BEFORE_KICKOFF = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)
AFTER_KICKOFF = datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc)
SECRET_KEY = "11111111-2222-3333-4444-555555555555"

DESCRIPTION = """| |EXTERNAL| |

Sportcast Fixture ID: 551738
Client Fixture ID: sr:match:73220788
Fixture Info: Martinique v El Salvador
Sport Affected: Soccer
Products Affected: Betbuilder

Sportcast Fixture ID: 551736
Client Fixture ID: sr:match:73220786
Fixture Info: Guatemala v Suriname
Sport Affected: Soccer
Products Affected: Betbuilder
"""


def _issue(summary="Soccer SGM Request", status="Under investigation", comments=None, description=DESCRIPTION):
    return {
        "key": "TSD-388989",
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "description": description,
            "customfield_10651": {"value": "Sportcast", "child": {"value": "Repush SGM"}},
            "customfield_11360": [{"objectId": "46193"}],
            "customfield_10002": [{"name": "PickleBet"}],
            "comment": {"comments": comments or []},
        },
    }


def _definition(fixture_id, home, away, start, following=True, betradar=""):
    consumer = [{"sourceName": "BetGenius", "consumerFixtureId": "14234050"}]
    if betradar:
        consumer.append({"sourceName": "BetRadar", "consumerFixtureId": betradar})
    return {
        "fixtureId": fixture_id,
        "clientId": 1,
        "homeTeam": {"team": {"name": home}},
        "awayTeam": {"team": {"name": away}},
        "isFixtureInplay": False,
        "details": {
            "startTimeUTC": start,
            "consumerfixtureIdList": consumer,
            "betBuilderClients": [
                {"clientId": 244, "clientName": "Picklebet", "betBuilderFollowing": following,
                 "lastPublished": "2026-10-05T06:32:47Z", "clientApiKey": SECRET_KEY},
            ],
        },
    }


class FakeSportcast:
    def __init__(self, definitions, betradar):
        self.definitions = definitions
        self.betradar = dict(betradar)
        self.calls: list[tuple] = []

    def get_fixture(self, fixture_id):
        self.calls.append(("get", fixture_id))
        return self.definitions.get(fixture_id, {})

    def consumer_id(self, fixture_id, source="BetRadar"):
        return self.betradar.get(fixture_id, "")

    def add_consumer_id(self, fixture_id, feed_id, source="BetRadar"):
        self.calls.append(("add", fixture_id, feed_id))
        self.betradar[fixture_id] = feed_id
        return f"Added BetRadar Consumer Id {feed_id} for FixtureId {fixture_id}"

    def republish(self, definition):
        self.calls.append(("publish", definition["fixtureId"]))


def _tsd_388989_sportcast():
    return FakeSportcast(
        {
            551738: _definition(551738, "Martinique", "El Salvador", "2026-10-05T22:00:00Z"),
            551736: _definition(551736, "Guatemala", "Suriname", "2026-10-06T00:00:00Z",
                                betradar="sr:match:73220786"),
        },
        {551736: "sr:match:73220786"},
    )


class TicketParsingTest(unittest.TestCase):
    def test_reads_both_fixtures_from_description(self):
        ticket = parse_issue(_issue())
        self.assertEqual([f.fixture_id for f in ticket.fixtures], [551738, 551736])
        self.assertEqual(ticket.fixtures[0].client_fixture_id, "sr:match:73220788")
        self.assertEqual(ticket.fixtures[1].info, "Guatemala v Suriname")
        self.assertEqual(ticket.client_name, "Picklebet")
        self.assertEqual(ticket.category, "Sportcast > Repush SGM")
        self.assertTrue(ticket.is_repush)

    def test_reads_html_description_and_flat_payload(self):
        html = DESCRIPTION.replace("\n", "<br>")
        flat = {"key": "TSD-1", "summary": "x", "status": "Reopened", "description": f"<p>{html}</p>"}
        ticket = parse_issue(flat)
        self.assertEqual(len(ticket.fixtures), 2)
        self.assertEqual(ticket.status, "Reopened")

    def test_reads_atlassian_document_format(self):
        adf = {"type": "doc", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "Sportcast Fixture ID: 551738"}, {"type": "hardBreak"},
            {"type": "text", "text": "Client Fixture ID: sr:match:73220788"}]}]}
        ticket = parse_issue({"key": "TSD-2", "fields": {"description": adf, "status": {"name": "x"}}})
        self.assertEqual(ticket.fixtures[0].client_fixture_id, "sr:match:73220788")

    def test_clone_marked_do_not_touch_is_a_test(self):
        ticket = parse_issue(_issue(summary="CLONE - Soccer SGM Request for test purposes please do not touch"))
        self.assertTrue(ticket.is_test)

    def test_betradar_feed_id(self):
        self.assertEqual(betradar_feed_id("sr:match:73220788"), "sr:match:73220788")
        self.assertEqual(betradar_feed_id("73220788"), "sr:match:73220788")
        self.assertEqual(betradar_feed_id("0010c3cbb400000000000000"), "")


class SuccessfulTrajectoryTest(unittest.TestCase):
    def test_tsd_388989_adds_missing_id_republishes_verifies_and_resolves(self):
        sportcast = _tsd_388989_sportcast()
        verified = []

        def verify(fixture_id, client, since):
            verified.append((fixture_id, client, since))
            return ["2026-10-05T07:09:21Z"]

        result = run(parse_issue(_issue()), sportcast, apply=True, now=BEFORE_KICKOFF, verify=verify)

        self.assertEqual([f.verdict for f in result.findings], [ADD_ID_AND_REPUBLISH, REPUBLISH])
        self.assertIn(("add", 551738, "sr:match:73220788"), sportcast.calls)
        self.assertNotIn(("add", 551736, "sr:match:73220786"), sportcast.calls)
        self.assertEqual([c for c in sportcast.calls if c[0] == "publish"],
                         [("publish", 551738), ("publish", 551736)])
        self.assertEqual({v[1] for v in verified}, {"Picklebet"})
        self.assertTrue(result.applied)
        self.assertEqual(result.blockers, [])
        self.assertEqual(result.jira["transition"], "Resolved")
        self.assertEqual(result.jira["incident_resolution"], "Workaround Applied")
        self.assertIn("Martinique v El Salvador (Sportcast 551738 / sr:match:73220788)", result.jira["comment"])
        self.assertIn("Please reopen this ticket", result.jira["comment"])
        self.assertIn("Mapped the missing Betradar consumer id", result.jira["resolution_notes"])

    def test_dry_run_changes_nothing(self):
        sportcast = _tsd_388989_sportcast()
        result = run(parse_issue(_issue()), sportcast, apply=False, now=BEFORE_KICKOFF)
        self.assertFalse(result.applied)
        self.assertIsNone(result.jira)
        self.assertEqual({c[0] for c in sportcast.calls}, {"get"})

    def test_no_resolve_without_delivery(self):
        result = run(parse_issue(_issue()), _tsd_388989_sportcast(), apply=True, now=BEFORE_KICKOFF,
                     verify=lambda *a: [], verify_timeout=30, poll_every=15, sleep=lambda s: None)
        self.assertTrue(result.applied)
        self.assertIsNone(result.jira)
        self.assertIn("No production SinglesCreated delivery", " ".join(result.blockers))

    def test_no_resolve_without_datadog(self):
        result = run(parse_issue(_issue()), _tsd_388989_sportcast(), apply=True, now=BEFORE_KICKOFF)
        self.assertTrue(result.applied)
        self.assertIsNone(result.jira)


class SafetyGateTest(unittest.TestCase):
    def _run(self, issue, sportcast, now=BEFORE_KICKOFF):
        return run(parse_issue(issue), sportcast, apply=True, now=now, verify=lambda *a: ["t"])

    def test_tsd_389084_test_clone_is_read_only(self):
        sportcast = _tsd_388989_sportcast()
        result = self._run(_issue(summary="CLONE - Soccer SGM Request for test purposes please do not touch"),
                           sportcast, now=AFTER_KICKOFF)
        self.assertEqual({c[0] for c in sportcast.calls}, {"get"})
        self.assertEqual([f.verdict for f in result.findings], [KICKED_OFF, KICKED_OFF])
        self.assertIsNone(result.jira)
        self.assertIn("test", " ".join(result.blockers))

    def test_kicked_off_fixtures_are_not_republished(self):
        sportcast = _tsd_388989_sportcast()
        result = self._run(_issue(), sportcast, now=AFTER_KICKOFF)
        self.assertEqual({c[0] for c in sportcast.calls}, {"get"})
        self.assertFalse(result.applied)

    def test_team_mismatch_stops(self):
        sportcast = _tsd_388989_sportcast()
        sportcast.definitions[551738] = _definition(551738, "Haiti", "Cuba", "2026-10-05T22:00:00Z")
        result = self._run(_issue(), sportcast)
        self.assertEqual(result.findings[0].verdict, TEAMS_MISMATCH)
        self.assertFalse(result.applied)

    def test_existing_different_betradar_id_is_not_overwritten(self):
        sportcast = _tsd_388989_sportcast()
        sportcast.betradar[551738] = "sr:match:1"
        result = self._run(_issue(), sportcast)
        self.assertEqual(result.findings[0].verdict, ID_MISMATCH)
        self.assertNotIn("add", {c[0] for c in sportcast.calls})

    def test_client_not_following_stops(self):
        sportcast = _tsd_388989_sportcast()
        sportcast.definitions[551736] = _definition(551736, "Guatemala", "Suriname", "2026-10-06T00:00:00Z",
                                                    following=False)
        result = self._run(_issue(), sportcast)
        self.assertEqual(result.findings[1].verdict, CLIENT_NOT_FOLLOWING)
        self.assertFalse(result.applied)

    def test_non_betradar_client_id_stops(self):
        issue = _issue(description="Sportcast Fixture ID: 551738\nClient Fixture ID: abc-xyz\n")
        result = self._run(issue, _tsd_388989_sportcast())
        self.assertEqual(result.findings[0].verdict, UNKNOWN_SOURCE)

    def test_missing_fixture_ids_and_no_sportcast(self):
        result = run(parse_issue(_issue(description="please add SGM")), None, apply=True)
        self.assertIn("No 'Sportcast Fixture ID'", result.blockers[0])
        result = run(parse_issue(_issue()), None, apply=True)
        self.assertIn("Sportcast is not reachable", result.blockers[0])

    def test_other_status_is_left_alone(self):
        result = run(parse_issue(_issue(status="New")), None, apply=True)
        self.assertEqual(result.branch, "fallback")
        self.assertIsNone(result.jira)

    def test_report_never_contains_client_api_keys(self):
        result = self._run(_issue(), _tsd_388989_sportcast())
        self.assertNotIn(SECRET_KEY, report(result))
        self.assertNotIn(SECRET_KEY, json.dumps(result.jira))

    def test_repeat_run_is_detected(self):
        sportcast = _tsd_388989_sportcast()
        first = run(parse_issue(_issue()), sportcast, now=AFTER_KICKOFF)
        issue = _issue(comments=[{"author": {"displayName": "Cursor"}, "body": report(first)}])
        second = run(parse_issue(issue), sportcast, now=AFTER_KICKOFF)
        self.assertTrue(already_reported(second))


class ReopenedTest(unittest.TestCase):
    def _comment(self, body):
        return [{"author": {"displayName": "Mendel", "emailAddress": "mendel@picklebet.com"}, "body": body},
                {"author": {"displayName": "Support", "emailAddress": "a@openbet.com"}, "body": "Looking"}]

    def test_customer_confirms(self):
        result = run(parse_issue(_issue(status="Reopened", comments=self._comment("All good, thanks!"))), None)
        self.assertEqual(result.jira["decision"], "OK")
        self.assertEqual(result.jira["transition"], "Resolved")

    def test_customer_says_still_missing(self):
        result = run(parse_issue(_issue(status="Reopened", comments=self._comment("SGM still missing"))), None)
        self.assertEqual(result.jira["decision"], "NEEDS_REVIEW")
        self.assertEqual(result.jira["assignee_id"], ASSIGNEE_ID)

    def test_analyze_reopen_comment(self):
        self.assertEqual(analyze_reopen_comment(""), "NEEDS_REVIEW")
        self.assertEqual(analyze_reopen_comment("OK"), "OK")
        self.assertEqual(analyze_reopen_comment("It is still broken"), "NEEDS_REVIEW")


class DatadogParsingTest(unittest.TestCase):
    def test_only_production_deliveries_count(self):
        message = ('Successfully sent and delivered a SinglesCreated message to Client: "Picklebet" (244). '
                   'Details: Fixture: 551738,Url: "{url}",ClusterSource "Prematch", Response code: 200')
        events = [
            {"attributes": {"timestamp": "t1", "message": message.format(url="https://oddsfeeds.example.com/x")}},
            {"attributes": {"timestamp": "t2", "message": message.format(url="https://oddsfeeds.staging.example.io/x")}},
        ]
        deliveries = parse_deliveries(events)
        self.assertEqual([d.production for d in deliveries], [True, False])
        self.assertEqual(deliveries[0].fixture_id, 551738)


if __name__ == "__main__":
    unittest.main()
