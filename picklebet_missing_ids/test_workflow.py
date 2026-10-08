"""Unit tests for the Picklebet missing consumer fixture workflow."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from picklebet_missing_ids.workflow import (
    ASSIGNEE_ID,
    AU_BASE_ENV,
    DEFAULT_AU_BASE,
    DEFAULT_INTERNAL_BASE,
    INTERNAL_BASE_ENV,
    ConfirmationRequired,
    SportcastError,
    WorkflowError,
    analyze_reopen_comment,
    branch_for_status,
    consumer_search_term,
    description_fixture_pairs,
    extract_custom_fields,
    issue_targets,
    match_state_payload,
    redact,
    run_issue,
    set_match_state,
    transition_jira_resolved,
    update_consumer_fixture_id,
)

AU = "https://au.example.test"
INTERNAL = "https://internal.example.test"
API_KEY = "test-api-key-value"
ACCOUNT_KEY = "test-account-key-value"


def _issue(**overrides):
    fields = {
        "client_fixture_id": "998877",
        "FixtureId": "445566",
        "apiKey": API_KEY,
        "feedProviders": False,
    }
    fields.update(overrides)
    return {"key": "PB-42", "fields": fields}


class SearchAndFieldsTest(unittest.TestCase):
    def test_feed_provider_search_uses_client_id(self) -> None:
        self.assertEqual(consumer_search_term("998877", True), "998877")
        self.assertEqual(consumer_search_term("998877", "true"), "998877")

    def test_non_feed_provider_search_uses_sr_match(self) -> None:
        self.assertEqual(consumer_search_term("998877", False), "sr:match:998877")
        self.assertEqual(consumer_search_term("sr:match:998877", "no"), "sr:match:998877")

    def test_missing_client_id_is_rejected(self) -> None:
        with self.assertRaises(WorkflowError):
            consumer_search_term("  ", False)

    def test_extracts_named_custom_fields(self) -> None:
        issue = {
            "key": "PB-7",
            "fields": {
                "customfield_1": "111",
                "customfield_2": "222",
                "customfield_3": API_KEY,
                "customfield_4": {"value": "true"},
            },
        }
        names = {
            "customfield_1": "client_fixture_id",
            "customfield_2": "FixtureId",
            "customfield_3": "apiKey",
            "customfield_4": "feedProviders",
        }
        fields = extract_custom_fields(issue, names)
        self.assertEqual(fields["client_fixture_id"], "111")
        self.assertEqual(fields["FixtureId"], 222)
        self.assertTrue(fields["feedProviders"])
        self.assertNotIn(API_KEY, json.dumps({k: v for k, v in fields.items() if k != "apiKey"}))

    def test_status_branches(self) -> None:
        self.assertEqual(branch_for_status("Under investigation"), "under_investigation")
        self.assertEqual(branch_for_status("Reopened"), "reopened")
        self.assertEqual(branch_for_status("In Progress"), "fallback")

    def test_description_pairs_from_sgm_html(self) -> None:
        description = (
            "<p>Sportcast Fixture ID: 551738 <br>Client Fixture ID: sr:match:73220788<br>"
            "Fixture Info: Martinique v El Salvador</p>"
            "<p>Sportcast Fixture ID: 551736<br>Client Fixture ID: sr:match:73220786<br>"
            "Fixture Info: Guatemala v Suriname</p>"
        )
        pairs = description_fixture_pairs(description)
        self.assertEqual(
            pairs,
            [
                {"FixtureId": 551738, "client_fixture_id": "sr:match:73220788"},
                {"FixtureId": 551736, "client_fixture_id": "sr:match:73220786"},
            ],
        )

    def test_description_targets_default_feed_providers_false(self) -> None:
        previous = os.environ.get("SPORTCAST_ACCOUNT_KEY")
        os.environ["SPORTCAST_ACCOUNT_KEY"] = ACCOUNT_KEY
        try:
            targets = issue_targets(
                {
                    "key": "TSD-389084",
                    "fields": {
                        "description": (
                            "Sportcast Fixture ID: 551738 Client Fixture ID: sr:match:73220788"
                        )
                    },
                }
            )
        finally:
            if previous is None:
                os.environ.pop("SPORTCAST_ACCOUNT_KEY", None)
            else:
                os.environ["SPORTCAST_ACCOUNT_KEY"] = previous
        self.assertEqual(len(targets), 1)
        self.assertFalse(targets[0]["feedProviders"])
        self.assertEqual(targets[0]["apiKey"], ACCOUNT_KEY)
        self.assertEqual(consumer_search_term(targets[0]["client_fixture_id"], False), "sr:match:73220788")

    def test_description_without_api_key_stops(self) -> None:
        previous = os.environ.get("SPORTCAST_ACCOUNT_KEY")
        os.environ.pop("SPORTCAST_ACCOUNT_KEY", None)
        try:
            with self.assertRaises(WorkflowError) as caught:
                issue_targets(
                    {
                        "key": "TSD-389084",
                        "fields": {
                            "description": "Sportcast Fixture ID: 551738 Client Fixture ID: sr:match:73220788"
                        },
                    }
                )
        finally:
            if previous is None:
                os.environ.pop("SPORTCAST_ACCOUNT_KEY", None)
            else:
                os.environ["SPORTCAST_ACCOUNT_KEY"] = previous
        self.assertIn("apiKey", str(caught.exception))


class MutationGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {
            AU_BASE_ENV: os.environ.get(AU_BASE_ENV),
            INTERNAL_BASE_ENV: os.environ.get(INTERNAL_BASE_ENV),
        }
        os.environ[AU_BASE_ENV] = AU
        os.environ[INTERNAL_BASE_ENV] = INTERNAL

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_update_url_uses_configured_base_and_query(self) -> None:
        from picklebet_missing_ids.workflow import update_consumer_fixture_url

        url = update_consumer_fixture_url(445566, "sr:match:998877")
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}", AU)
        self.assertEqual(parts.path, "/api/UpdateConsumerFixtureid")
        self.assertEqual(
            parse_qs(parts.query),
            {"fixtureId": ["445566"], "consumerfixtureid": ["sr:match:998877"]},
        )
        self.assertNotIn(API_KEY, url)

    def test_missing_base_url_uses_automation_host(self) -> None:
        os.environ.pop(AU_BASE_ENV)
        from picklebet_missing_ids.workflow import update_consumer_fixture_url

        url = update_consumer_fixture_url(1, "abc")
        self.assertTrue(url.startswith(DEFAULT_AU_BASE + "/api/UpdateConsumerFixtureid?"))
        self.assertNotIn(API_KEY, url)

    def test_non_https_base_is_rejected(self) -> None:
        os.environ[AU_BASE_ENV] = "http://clusterau.sportcastlive.com"
        from picklebet_missing_ids.workflow import update_consumer_fixture_url

        with self.assertRaises(WorkflowError) as caught:
            update_consumer_fixture_url(1, "abc")
        self.assertIn(AU_BASE_ENV, str(caught.exception))
        self.assertNotIn("http://", str(caught.exception))

    def test_mutations_require_confirmation(self) -> None:
        with self.assertRaises(ConfirmationRequired):
            update_consumer_fixture_id(1, "abc", confirm=False, http_get=lambda url: 200)
        with self.assertRaises(ConfirmationRequired):
            set_match_state(1, API_KEY, 1, confirm=False, http_post=lambda url, body: 200)

    def test_match_state_payload_redacts_in_errors(self) -> None:
        def fail(url, body):
            raise ConnectionError(json.dumps(body))

        with self.assertRaises(SportcastError) as caught:
            set_match_state(
                445566,
                API_KEY,
                1,
                confirm=True,
                account_key=ACCOUNT_KEY,
                http_post=fail,
            )
        message = str(caught.exception)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn(ACCOUNT_KEY, message)
        self.assertIn("[redacted]", message)

    def test_non_200_is_an_error(self) -> None:
        with self.assertRaises(SportcastError):
            update_consumer_fixture_id(1, "abc", confirm=True, http_get=lambda url: 500)

    def test_network_error_is_retried_once(self) -> None:
        calls = {"count": 0}

        def flaky(url, body):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ConnectionError("temporary")
            return 200

        response = set_match_state(10, API_KEY, 2, confirm=True, http_post=flaky)
        self.assertEqual(response.status, 200)
        self.assertEqual(calls["count"], 2)

    def test_match_state_rejects_other_states(self) -> None:
        with self.assertRaises(WorkflowError):
            match_state_payload(1, API_KEY, 9)


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {
            AU_BASE_ENV: os.environ.get(AU_BASE_ENV),
            INTERNAL_BASE_ENV: os.environ.get(INTERNAL_BASE_ENV),
        }
        os.environ[AU_BASE_ENV] = AU
        os.environ[INTERNAL_BASE_ENV] = INTERNAL

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_under_investigation_updates_then_settles(self) -> None:
        events: list[tuple] = []

        def search(term: str) -> str:
            events.append(("search", term))
            return "sr:match:998877"

        def http_get(url: str) -> int:
            events.append(("get", url))
            return 200

        def http_post(url: str, body: dict) -> int:
            events.append(("post", body["Items"][0]["MatchState"], body))
            return 200

        def sleep(seconds: float) -> None:
            events.append(("sleep", seconds))

        result = run_issue(
            _issue(),
            status="Under investigation",
            confirm=True,
            search_fn=search,
            http_get=http_get,
            http_post=http_post,
            sleep=sleep,
            account_key=ACCOUNT_KEY,
        )

        self.assertEqual(result["action"], "resolved")
        self.assertEqual(result["searchTerm"], "sr:match:998877")
        self.assertEqual(result["resolvedFixtureId"], "sr:match:998877")
        self.assertEqual(result["matchStates"], [1, 2])
        self.assertEqual(result["jira"]["transition"], "Resolved")
        self.assertEqual(result["jira"]["fields"]["Incident Resolution"], "Workaround Applied")
        self.assertEqual(result["jira"]["fields"]["Resolving Team"], "Sportsbook Support")
        self.assertIn("PB-42", result["jira"]["comment"])
        self.assertEqual(result["fields"]["apiKey"], "[redacted]")
        self.assertEqual([event[0] for event in events], ["search", "get", "post", "sleep", "post"])
        self.assertEqual(len(result["fixtures"]), 1)
        self.assertEqual(events[3], ("sleep", 3))
        self.assertEqual(events[2][1], 1)
        self.assertEqual(events[4][1], 2)
        self.assertEqual(events[2][2]["Key"], ACCOUNT_KEY)
        self.assertNotIn(API_KEY, json.dumps(result))

    def test_two_description_fixtures_search_before_writes(self) -> None:
        previous_account = os.environ.get("SPORTCAST_ACCOUNT_KEY")
        os.environ["SPORTCAST_ACCOUNT_KEY"] = ACCOUNT_KEY
        os.environ.pop(AU_BASE_ENV, None)
        os.environ.pop(INTERNAL_BASE_ENV, None)
        events: list[tuple] = []

        def search(term: str) -> str:
            events.append(("search", term))
            return term

        def http_get(url: str) -> int:
            events.append(("get", url))
            return 200

        def http_post(url: str, body: dict) -> int:
            events.append(("post", body["Items"][0]["MatchState"]))
            return 200

        issue = {
            "key": "TSD-389084",
            "fields": {
                "description": (
                    "Sportcast Fixture ID: 551738 Client Fixture ID: sr:match:73220788 "
                    "Sportcast Fixture ID: 551736 Client Fixture ID: sr:match:73220786"
                )
            },
        }
        try:
            result = run_issue(
                issue,
                status="Under investigation",
                confirm=True,
                search_fn=search,
                http_get=http_get,
                http_post=http_post,
                sleep=lambda seconds: events.append(("sleep", seconds)),
            )
        finally:
            if previous_account is None:
                os.environ.pop("SPORTCAST_ACCOUNT_KEY", None)
            else:
                os.environ["SPORTCAST_ACCOUNT_KEY"] = previous_account

        self.assertEqual(result["action"], "resolved")
        self.assertEqual(
            [event[0] for event in events],
            ["search", "search", "get", "post", "sleep", "post", "get", "post", "sleep", "post"],
        )
        self.assertEqual(
            [item["FixtureId"] for item in result["fixtures"]],
            [551738, 551736],
        )
        self.assertNotIn(ACCOUNT_KEY, json.dumps(result))
        self.assertTrue(events[2][1].startswith(DEFAULT_AU_BASE))
        self.assertTrue(DEFAULT_INTERNAL_BASE.startswith("https://"))

    def test_feed_provider_true_searches_raw_client_id(self) -> None:
        seen = {}

        def search(term: str) -> str:
            seen["term"] = term
            return "client-fixture"

        run_issue(
            _issue(feedProviders=True, client_fixture_id="client-fixture"),
            status="Under investigation",
            confirm=True,
            search_fn=search,
            http_get=lambda url: 200,
            http_post=lambda url, body: 200,
            sleep=lambda seconds: None,
        )
        self.assertEqual(seen["term"], "client-fixture")

    def test_unconfirmed_run_does_not_call_sportcast(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("Sportcast was called without confirmation")

        with self.assertRaises(ConfirmationRequired):
            run_issue(
                _issue(),
                status="Under investigation",
                confirm=False,
                search_fn=boom,
                http_get=boom,
                http_post=boom,
            )

    def test_reopen_ok_resolves(self) -> None:
        plan = run_issue(
            _issue(),
            status="Reopened",
            confirm=True,
            comment="Thanks, this is resolved now.",
        )
        self.assertEqual(plan["decision"], "OK")
        self.assertEqual(plan["transition"], "Resolved")
        self.assertEqual(plan["resolution"], "Fixed")
        self.assertEqual(plan["comment"], "Resolving")
        self.assertEqual(plan["fields"]["Incident Resolution"], "Workaround Applied")

    def test_reopen_ambiguous_assigns_investigation(self) -> None:
        plan = run_issue(
            _issue(),
            status="Reopened",
            confirm=True,
            comment="Can someone look at this?",
        )
        self.assertEqual(plan["decision"], "NEEDS_REVIEW")
        self.assertEqual(plan["transition"], "Under investigation")
        self.assertEqual(plan["assignment"]["assignee_id"], ASSIGNEE_ID)

    def test_reopen_persist_language_needs_review(self) -> None:
        self.assertEqual(analyze_reopen_comment("It is still broken"), "NEEDS_REVIEW")
        self.assertEqual(analyze_reopen_comment("This remains unconfirmed"), "NEEDS_REVIEW")
        self.assertEqual(analyze_reopen_comment("OK"), "OK")
        self.assertEqual(analyze_reopen_comment(""), "NEEDS_REVIEW")

    def test_fallback_logs_todo(self) -> None:
        result = run_issue(_issue(), status="Waiting for customer", confirm=True)
        self.assertEqual(result, {"action": "todo", "issue_key": "PB-42", "message": "todo"})

    def test_resolved_comment_template(self) -> None:
        plan = transition_jira_resolved("PB-42")
        self.assertEqual(
            plan["comment"],
            "Thanks for raising PB-42. Please check . If the issue persists please reopen the ticket.",
        )


class SourceSafetyTest(unittest.TestCase):
    def test_skill_and_library_only_embed_automation_hosts(self) -> None:
        root = Path(__file__).resolve().parent
        skill = root.parent / ".cursor" / "skills" / "picklebet-missing-ids" / "SKILL.md"
        agent = root.parent / ".cursor" / "agents" / "picklebet-missing-ids-agent.md"
        allowed = (
            "startswith",
            "clusterau.sportcastlive.com",
            "clusterinternal.sportcastlive.com",
            "openbet.atlassian.net/jira/servicedesk/projects/TSD/settings/automate",
        )
        for path in (root / "workflow.py", root / "__init__.py", skill, agent):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "https://" in line or "http://" in line:
                    self.assertTrue(any(marker in line for marker in allowed), path.name + ": " + line)


class RedactTest(unittest.TestCase):
    def test_redact_replaces_secret(self) -> None:
        self.assertEqual(redact(f"key={API_KEY}", [API_KEY]), "key=[redacted]")


if __name__ == "__main__":
    unittest.main()
