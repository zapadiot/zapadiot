---
name: picklebet-missing-ids
description: >-
  Run the Picklebet missing consumer fixture ID workflow for a Jira issue
  that is Under investigation or Reopened. Use when Jira automation rule
  019f45bf-fbf0-7bbf-b180-ae122b0714ac fires, or when asked to find a
  consumer fixture, update it in Sportcast, set match state, or resolve
  the Jira ticket. Pair with the picklebet-missing-ids agent.
---

# Picklebet Missing Consumer Fixture IDs

Triggered by Jira automation rule `019f45bf-fbf0-7bbf-b180-ae122b0714ac`:

https://openbet.atlassian.net/jira/servicedesk/projects/TSD/settings/automate#/rule/019f45bf-fbf0-7bbf-b180-ae122b0714ac

The agent instructions are `.cursor/agents/picklebet-missing-ids-agent.md`. The tested helpers live in `picklebet_missing_ids.workflow`. Use them for search terms, request bodies, confirmation checks, and Jira plans. Do not hand-build Sportcast URLs.

## Confirmation

- That automation rule is confirmation to run the branch that matches the issue status.
- Any other request needs a clear yes before a Sportcast write or a Jira transition.
- Pass `confirm=True` only after that confirmation. Without it, mutating functions raise `ConfirmationRequired` and make no HTTP call.

## Runtime configuration

Read keys from the environment or the Jira issue at runtime. Do not write API keys into the repo, logs, comments, or error output.

| Name | Role |
| --- | --- |
| `SPORTCAST_AU_BASE` | HTTPS base for the consumer-fixture update. Defaults to `https://clusterau.sportcastlive.com` |
| `SPORTCAST_INTERNAL_BASE` | HTTPS base for match state. Defaults to `https://clusterinternal.sportcastlive.com` |
| `SPORTCAST_ACCOUNT_KEY` | Outer match-state `Key`. Also the item `Key` when the issue has no `apiKey` |
| Issue field `apiKey` | Item `Key`, and the outer `Key` when the account env var is unset |

Those two hosts are the ones in the automation rule. An env var overrides the default. A value that is not HTTPS is rejected. Do not write API keys into logs, comments, or error output. If `apiKey` and `SPORTCAST_ACCOUNT_KEY` are both missing, stop before any Sportcast write and say that `apiKey` is required.

## Fields

From the Jira issue, via Atlassian `getJiraIssue` (authenticate the Atlassian MCP first if it is not already signed in):

- `client_fixture_id` — Picklebet fixture ID
- `FixtureId` — Sportcast fixture ID
- `apiKey` — Sportcast API key
- `feedProviders` — boolean

Pass the issue payload and, when the values sit on `customfield_*` ids, the id-to-name map into `issue_targets`. Log `redacted_fields` only.

TSD SGM tickets usually leave those custom fields empty and list one or more pairs in the description:

```
Sportcast Fixture ID: 551738
Client Fixture ID: sr:match:73220788
```

`issue_targets` uses the custom fields when both ids are present. Otherwise it uses every description pair. Absent `feedProviders` defaults to false. Process every pair before the Jira transition. Do not invent ids from fixture names.

## Branch: Under investigation

1. `issue_targets` (custom fields, otherwise every description pair).
2. `consumer_search_term(client_fixture_id, feedProviders)`.
   - `feedProviders` true: the client fixture id itself.
   - `feedProviders` false: `sr:match:{client_fixture_id}` unless it already has that prefix.
3. Search Sportcast with that term and take `resolvedFixtureId`. The sportcast MCP is the search path. On failure, widen the time window, drop filters, and try at most about three different searches. If search is still unavailable, stop. Do not invent a search URL.
4. Require both `FixtureId` and `resolvedFixtureId` before any write.
5. `update_consumer_fixture_id(FixtureId, resolvedFixtureId, confirm=True)`. This GETs `/api/UpdateConsumerFixtureid` and expects HTTP 200.
6. `set_match_state(..., match_state=1, confirm=True)`.
7. `wait_seconds(3)`.
8. `set_match_state(..., match_state=2, confirm=True)`.
9. Apply `transition_jira_resolved(issue_key)` through Jira:
   - Incident Resolution = `Workaround Applied`
   - Resolving Team = `Sportsbook Support`
   - Comment: `Thanks for raising {issue_key}. Please check . If the issue persists please reopen the ticket.`
   - Transition to Resolved.

`run_issue(..., status="Under investigation", confirm=True, search_fn=...)` performs steps 1–9 for every fixture, including the three-second wait between match states. Search every fixture before the first write. Applying the returned `jira` plan still goes through the Jira MCP, once, after every fixture succeeds.

Report this block. Mask `apiKey`.

```
## Picklebet Missing IDs Result
- Issue: <KEY>
- Client Fixture ID: <client_fixture_id>
- Sportcast Fixture ID: <FixtureId>
- API Key: masked
- Feed Providers: true/false
- Search Format: direct|sr:match:
- Resolved Fixture ID: <resolvedFixtureId>
- UpdateConsumerFixtureid: SUCCESS|FAILED (HTTP <code>)
- MatchState 1: SUCCESS|FAILED
- MatchState 2: SUCCESS|FAILED
- Jira Transitioned: yes/no
- Final Status: RESOLVED|REOPENED_HANDLED|FALLBACK
```

Repeat the fixture lines when the description has more than one pair.

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
