---
name: picklebet-missing-ids
description: >-
  Run the Picklebet missing consumer fixture ID workflow for a Jira issue
  that is Under investigation or Reopened. Use when automation rule
  019f45bf-fbf0-7bbf-b180-ae122b0714ac fires, or when asked to find a
  consumer fixture, update it in Sportcast, set match state, or resolve
  the Jira ticket.
---

# Picklebet Missing Consumer Fixture IDs

Triggered by the Jira automation rule `019f45bf-fbf0-7bbf-b180-ae122b0714ac`.

The tested helpers live in `picklebet_missing_ids.workflow`. Use them for search terms, request bodies, confirmation checks, and Jira plans. Do not hand-build Sportcast URLs.

## Confirmation

- That automation rule is confirmation to run the branch that matches the issue status.
- Any other request needs a clear yes before a Sportcast write or a Jira transition.
- Pass `confirm=True` only after that confirmation. Without it, mutating functions raise `ConfirmationRequired` and make no HTTP call.

## Runtime configuration

Read these from the environment or the Jira issue at runtime. Do not write them into the repo, logs, comments, or error output.

| Name | Role |
| --- | --- |
| `SPORTCAST_AU_BASE` | HTTPS base for the consumer-fixture update when the client region is `PROD_AU` |
| `SPORTCAST_EU_BASE` | HTTPS base for the consumer-fixture update when the client region is `PROD_EU` |
| `SPORTCAST_INTERNAL_BASE` | HTTPS base for `getclient` and match state |
| `SPORTCAST_ACCOUNT_KEY` | Outer match-state `Key`, when it differs from the issue |
| Issue field `apiKey` | Item `Key`, and the outer `Key` when the account env var is unset |
| `SPORTCAST_OPERATOR_KEYS` | JSON operator-to-API-key table, used when the issue has no `apiKey` |

If a base URL is unset, stop and say which variable is missing. Do not guess a host.

## Region

The consumer-fixture update host depends on the client's region, matching the TSD automation rule `019fa81d-054b-7a10-8437-d68eeb4e7d37`:

1. `client_region(apiKey)` GETs `/api/getclient?key=…&Connections=true` on `SPORTCAST_INTERNAL_BASE` and reads `MessagingRegion` (trimmed, upper-cased).
2. `region_base_url(region)` maps `PROD_AU` to `SPORTCAST_AU_BASE` and `PROD_EU` to `SPORTCAST_EU_BASE`.
3. Any other region, or a missing `MessagingRegion`, stops the workflow with `unsupported MessagingRegion …` or `MessagingRegion is missing`. Do not fall back to another region.

## Fields

From the Jira issue, via Atlassian `jira_get_issue` (authenticate the Atlassian MCP first if it is not already signed in):

- `client_fixture_id` — Picklebet fixture ID
- `FixtureId` — Sportcast fixture ID
- `apiKey` — Sportcast API key
- `feedProviders` — boolean

Pass the issue payload and, when the values sit on `customfield_*` ids, the id-to-name map into `extract_custom_fields`. Log `redacted_fields` only.

## Operator API key

When the issue has no `apiKey`, take the key from the operator instead, as the automation rule's custom field extractor agent does:

1. Read `Operator/s (TSD)` (`customfield_11360`). It holds an Assets object reference such as `{"objectId": "46193"}`.
2. Resolve the label with Atlassian `getAssetsObject` (numeric `objectId`). For example, `46193` is `Picklebet`.
3. Pass that label as `operator=` to `extract_custom_fields` or `run_issue`. `operator_api_key` matches it case-insensitively against `SPORTCAST_OPERATOR_KEYS`.

`SPORTCAST_OPERATOR_KEYS` accepts `{"Picklebet": "<key>"}`, `{"Picklebet": {"apikey": "<key>"}}`, or `[{"operator": "Picklebet", "apikey": "<key>"}]`. If it is unset or has no entry for the operator, stop and name the gap. Do not ask an AI step to recall a key.

## Branch: Under investigation

1. `extract_custom_fields`, with `operator=` when the issue has no `apiKey`.
2. `client_region(apiKey)`, then `region_base_url(region)`. Stop here on an unsupported region or a missing base URL.
3. `consumer_search_term(client_fixture_id, feedProviders)`.
   - `feedProviders` true: the client fixture id itself.
   - `feedProviders` false: `sr:match:{client_fixture_id}` unless it already has that prefix.
4. Search Sportcast with that term and take `resolvedFixtureId`. The sportcast MCP is the search path. On failure, widen the time window, drop filters, and try at most about three different searches. If search is still unavailable, stop. Do not invent a search URL.
5. Require both `FixtureId` and `resolvedFixtureId` before any write.
6. `update_consumer_fixture_id(FixtureId, resolvedFixtureId, region=region, confirm=True)`. This GETs `/api/UpdateConsumerFixtureid` on the region's base and expects HTTP 200.
7. `set_match_state(..., match_state=1, confirm=True)`.
8. `wait_seconds(3)`.
9. `set_match_state(..., match_state=2, confirm=True)`.
10. Apply `transition_jira_resolved(issue_key)` through Jira:
   - Incident Resolution = `Workaround Applied`
   - Resolving Team = `Sportsbook Support`
   - Comment: `Thanks for raising {issue_key}. Please check . If the issue persists please reopen the ticket.`
   - Transition to Resolved.

`run_issue(..., status="Under investigation", confirm=True, search_fn=...)` performs steps 1–10's plan, including the three-second wait between match states. Applying the returned `jira` plan still goes through the Jira MCP.

Network errors retry once. Any other failure is logged without secrets and stops the write path.

## Branch: Reopened

1. Read the latest customer comment.
2. Decide `OK` when the customer confirms it is resolved. Decide `NEEDS_REVIEW` when the issue persists or the comment is ambiguous. `analyze_reopen_comment` is the fallback and defaults to `NEEDS_REVIEW`.
3. `OK`: comment `Resolving`, transition to Resolved with resolution `Fixed`, and set Incident Resolution to `Workaround Applied`.
4. `NEEDS_REVIEW`: transition to Under investigation and assign `712020:e626a9d2-bf67-4741-a80b-3a018547cce4`.

`run_issue(..., status="Reopened", comment=..., confirm=True)` returns that plan. Apply it through Jira only after confirmation.

## Branch: Fallback

Any other status returns `{"action": "todo", "message": "todo"}`. Leave the issue for manual handling.

## Checks

```bash
python3 -m unittest picklebet_missing_ids.test_workflow
```
