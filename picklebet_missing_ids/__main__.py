"""Command line entry point for the cloud agent.

    python3 -m picklebet_missing_ids TSD-389084 --issue-json issue.json
    python3 -m picklebet_missing_ids TSD-389084 --apply

Prints the Jira comment text. Exit code 0 means the run finished (with or
without changes); 2 means it could not read the ticket or reach Sportcast.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from picklebet_missing_ids import datadog, jira, sportcast
from picklebet_missing_ids.ticket import parse_issue
from picklebet_missing_ids.workflow import already_reported, report, run


def _load_issue(key: str, path: str | None) -> dict:
    if path:
        with open(path, encoding="utf-8") as handle:
            issue = json.load(handle)
        issue.setdefault("key", key)
        return issue
    if jira.configured():
        return jira.get_issue(key)
    raise SystemExit(
        "No issue payload. Save the Jira trigger payload to a file and pass --issue-json, "
        f"or set {jira.BASE_ENV}, {jira.EMAIL_ENV} and {jira.TOKEN_ENV}."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="picklebet_missing_ids")
    parser.add_argument("issue_key")
    parser.add_argument("--issue-json", help="Jira issue payload saved from the trigger")
    parser.add_argument("--apply", action="store_true",
                        help="Add missing ids, republish, verify, and resolve when safe")
    parser.add_argument("--now", help="ISO time to treat as now (tests and replays)")
    parser.add_argument("--json", action="store_true", help="Print a JSON summary after the comment")
    args = parser.parse_args(argv)

    ticket = parse_issue(_load_issue(args.issue_key, args.issue_json))
    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)

    missing = sportcast.missing_credentials()
    if not datadog.configured():
        missing.append("DD_API_KEY/DD_APPLICATION_KEY")
    if args.apply and not jira.configured():
        missing.append(f"{jira.EMAIL_ENV}/{jira.TOKEN_ENV}")

    client = None
    if not sportcast.missing_credentials():
        try:
            client = sportcast.SportcastClient()
            client.login()
        except sportcast.SportcastError as exc:
            print(f"Sportcast login failed: {exc}", file=sys.stderr)
            client = None

    def verify(fixture_id: int, client_name: str, since: str) -> list[str]:
        return [d.timestamp for d in datadog.production_deliveries(fixture_id, client_name, since)]

    try:
        result = run(
            ticket,
            client,
            apply=args.apply,
            now=now,
            verify=verify if datadog.configured() else None,
        )
    except sportcast.SportcastError as exc:
        print(f"{ticket.key}: Sportcast call failed: {exc}")
        return 2

    jira_outcome = ""
    if result.jira and args.apply and not ticket.is_test:
        if jira.configured():
            try:
                jira_outcome = jira.apply_plan(result.jira)
            except jira.JiraError as exc:
                jira_outcome = f"failed: {exc}"
        else:
            jira_outcome = "not applied (no Jira credentials)"

    if already_reported(result) and not result.applied and not jira_outcome:
        print(f"No change since the last run. [{result.marker}]")
    else:
        print(report(result, missing_settings=missing, jira_outcome=jira_outcome))
    if args.json:
        print(json.dumps({
            "issue_key": ticket.key,
            "branch": result.branch,
            "applied": result.applied,
            "verdicts": {f.fixture_id: f.verdict for f in result.findings},
            "blockers": result.blockers,
            "jira": result.jira,
            "jira_outcome": jira_outcome,
            "marker": result.marker,
        }, indent=2, default=str))
    return 0 if client is not None or result.branch != "under_investigation" else 2


if __name__ == "__main__":
    sys.exit(main())
