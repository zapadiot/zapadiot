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
    EU_BASE_ENV,
    INTERNAL_BASE_ENV,
    ConfirmationRequired,
    SportcastError,
    WorkflowError,
    analyze_reopen_comment,
    branch_for_status,
    client_region,
    consumer_search_term,
    extract_custom_fields,
    get_client_url,
    match_state_payload,
    redact,
    region_base_url,
    run_issue,
    set_match_state,
    transition_jira_resolved,
    update_consumer_fixture_id,
    update_consumer_fixture_url,
)

AU = "https://au.example.test"
EU = "https://eu.example.test"
INTERNAL = "https://internal.example.test"
API_KEY = "test-api-key-value"
ACCOUNT_KEY = "test-account-key-value"


class _Reply:
    def __init__(self, status: int, body: object = "") -> None:
        self.status = status
        self.body = body


def _client_reply(region: object) -> _Reply:
    return _Reply(200, json.dumps({"ClientId": 7, "MessagingRegion": region}))


class _SportcastEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {
            name: os.environ.get(name)
            for name in (AU_BASE_ENV, EU_BASE_ENV, INTERNAL_BASE_ENV)
        }
        os.environ[AU_BASE_ENV] = AU
        os.environ[EU_BASE_ENV] = EU
        os.environ[INTERNAL_BASE_ENV] = INTERNAL

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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


class RegionTest(_SportcastEnv):
    def test_prod_regions_map_to_their_base_urls(self) -> None:
        self.assertEqual(region_base_url("PROD_AU"), AU)
        self.assertEqual(region_base_url("PROD_EU"), EU)
        self.assertEqual(region_base_url("  prod_eu "), EU)

    def test_unsupported_region_is_rejected(self) -> None:
        for region in ("PROD_US", "UAT_AU", "PRODAU"):
            with self.assertRaises(WorkflowError) as caught:
                region_base_url(region)
            self.assertIn("unsupported MessagingRegion", str(caught.exception))
            self.assertIn(region, str(caught.exception))

    def test_missing_or_odd_region_is_rejected(self) -> None:
        for region in (None, "", "   "):
            with self.assertRaises(WorkflowError) as caught:
                region_base_url(region)
            self.assertIn("MessagingRegion is missing", str(caught.exception))
        with self.assertRaises(WorkflowError):
            region_base_url("PROD_AU/../x")

    def test_region_base_url_names_missing_env_var(self) -> None:
        os.environ.pop(EU_BASE_ENV)
        with self.assertRaises(WorkflowError) as caught:
            region_base_url("PROD_EU")
        self.assertIn(EU_BASE_ENV, str(caught.exception))

    def test_get_client_url_uses_internal_base(self) -> None:
        parts = urlsplit(get_client_url(API_KEY))
        self.assertEqual(f"{parts.scheme}://{parts.netloc}", INTERNAL)
        self.assertEqual(parts.path, "/api/getclient")
        self.assertEqual(parse_qs(parts.query), {"key": [API_KEY], "Connections": ["true"]})

    def test_client_region_reads_messaging_region(self) -> None:
        seen = []

        def http_get(url: str) -> _Reply:
            seen.append(url)
            return _client_reply("prod_au")

        self.assertEqual(client_region(API_KEY, http_get=http_get), "PROD_AU")
        self.assertEqual(urlsplit(seen[0]).path, "/api/getclient")

    def test_client_region_accepts_parsed_body(self) -> None:
        reply = _Reply(200, {"MessagingRegion": "PROD_EU"})
        self.assertEqual(client_region(API_KEY, http_get=lambda url: reply), "PROD_EU")

    def test_client_region_errors_are_redacted(self) -> None:
        def not_found(url: str) -> _Reply:
            return _Reply(404, f"no client for {API_KEY}")

        with self.assertRaises(SportcastError) as caught:
            client_region(API_KEY, http_get=not_found)
        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

        def offline(url: str) -> _Reply:
            raise ConnectionError(f"cannot reach {url}")

        with self.assertRaises(SportcastError) as caught:
            client_region(API_KEY, http_get=offline)
        self.assertNotIn(API_KEY, str(caught.exception))

    def test_client_region_rejects_bad_bodies(self) -> None:
        for body in ("not json", "[]", json.dumps({"ClientId": 7})):
            with self.assertRaises(WorkflowError):
                client_region(API_KEY, http_get=lambda url, body=body: _Reply(200, body))


class MutationGuardTest(_SportcastEnv):
    def test_update_url_uses_region_base_and_query(self) -> None:
        for region, base in (("PROD_AU", AU), ("PROD_EU", EU)):
            url = update_consumer_fixture_url(445566, "sr:match:998877", region=region)
            parts = urlsplit(url)
            self.assertEqual(f"{parts.scheme}://{parts.netloc}", base)
            self.assertEqual(parts.path, "/api/UpdateConsumerFixtureid")
            self.assertEqual(
                parse_qs(parts.query),
                {"fixtureId": ["445566"], "consumerfixtureid": ["sr:match:998877"]},
            )
            self.assertNotIn(API_KEY, url)

    def test_missing_base_url_names_the_env_var_only(self) -> None:
        os.environ.pop(AU_BASE_ENV)

        with self.assertRaises(WorkflowError) as caught:
            update_consumer_fixture_url(1, "abc", region="PROD_AU")
        self.assertIn(AU_BASE_ENV, str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_mutations_require_confirmation(self) -> None:
        with self.assertRaises(ConfirmationRequired):
            update_consumer_fixture_id(
                1, "abc", region="PROD_AU", confirm=False, http_get=lambda url: 200
            )
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
            update_consumer_fixture_id(
                1, "abc", region="PROD_AU", confirm=True, http_get=lambda url: 500
            )

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


def _routing_get(region: object, events: list | None = None):
    def http_get(url: str):
        if events is not None:
            events.append(("get", url))
        if urlsplit(url).path == "/api/getclient":
            return _client_reply(region)
        return 200

    return http_get


class EndToEndTest(_SportcastEnv):
    def test_under_investigation_updates_then_settles(self) -> None:
        events: list[tuple] = []

        def search(term: str) -> str:
            events.append(("search", term))
            return "sr:match:998877"

        http_get = _routing_get("PROD_AU", events)

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
        self.assertEqual(result["region"], "PROD_AU")
        self.assertEqual(result["searchTerm"], "sr:match:998877")
        self.assertEqual(result["resolvedFixtureId"], "sr:match:998877")
        self.assertEqual(result["matchStates"], [1, 2])
        self.assertEqual(result["jira"]["transition"], "Resolved")
        self.assertEqual(result["jira"]["fields"]["Incident Resolution"], "Workaround Applied")
        self.assertEqual(result["jira"]["fields"]["Resolving Team"], "Sportsbook Support")
        self.assertIn("PB-42", result["jira"]["comment"])
        self.assertEqual(result["fields"]["apiKey"], "[redacted]")
        self.assertEqual(
            [event[0] for event in events],
            ["get", "search", "get", "post", "sleep", "post"],
        )
        self.assertTrue(events[0][1].startswith(f"{INTERNAL}/api/getclient?"))
        self.assertTrue(events[2][1].startswith(f"{AU}/api/UpdateConsumerFixtureid?"))
        self.assertEqual(events[4], ("sleep", 3))
        self.assertEqual(events[3][1], 1)
        self.assertEqual(events[5][1], 2)
        self.assertEqual(events[3][2]["Key"], ACCOUNT_KEY)
        self.assertNotIn(API_KEY, json.dumps(result))

    def test_eu_client_updates_on_eu_base(self) -> None:
        events: list[tuple] = []
        result = run_issue(
            _issue(),
            status="Under investigation",
            confirm=True,
            search_fn=lambda term: "sr:match:998877",
            http_get=_routing_get("PROD_EU", events),
            http_post=lambda url, body: 200,
            sleep=lambda seconds: None,
        )
        self.assertEqual(result["region"], "PROD_EU")
        self.assertTrue(events[1][1].startswith(f"{EU}/api/UpdateConsumerFixtureid?"))

    def test_unsupported_region_stops_before_search_or_writes(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("workflow continued past an unsupported region")

        with self.assertRaises(WorkflowError) as caught:
            run_issue(
                _issue(),
                status="Under investigation",
                confirm=True,
                search_fn=boom,
                http_get=_routing_get("PROD_US"),
                http_post=boom,
                sleep=boom,
            )
        self.assertIn("unsupported MessagingRegion PROD_US", str(caught.exception))

    def test_missing_region_base_stops_before_search(self) -> None:
        os.environ.pop(EU_BASE_ENV)

        def boom(*_args, **_kwargs):
            raise AssertionError("workflow continued without a region base URL")

        with self.assertRaises(WorkflowError) as caught:
            run_issue(
                _issue(),
                status="Under investigation",
                confirm=True,
                search_fn=boom,
                http_get=_routing_get("PROD_EU"),
                http_post=boom,
            )
        self.assertIn(EU_BASE_ENV, str(caught.exception))

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
            http_get=_routing_get("PROD_AU"),
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
    def test_skill_and_library_do_not_embed_url_hosts(self) -> None:
        root = Path(__file__).resolve().parent
        skill = root.parent / ".cursor" / "skills" / "picklebet-missing-ids" / "SKILL.md"
        for path in (root / "workflow.py", root / "__init__.py", skill):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "https://" in line or "http://" in line:
                    self.assertIn("startswith", line, path.name)


class RedactTest(unittest.TestCase):
    def test_redact_replaces_secret(self) -> None:
        self.assertEqual(redact(f"key={API_KEY}", [API_KEY]), "key=[redacted]")


if __name__ == "__main__":
    unittest.main()
