---
name: picklebet-missing-ids
description: >-
  Handle Picklebet "SGM Request" / Sportcast > Repush SGM tickets in the TSD
  Jira project: read the Sportcast fixture ids from the description, check
  each fixture in Sportcast, add a missing Betradar consumer id, republish,
  confirm SinglesCreated reached Picklebet production, and resolve the
  ticket. Use when Jira automation rule 019f45bf-fbf0-7bbf-b180-ae122b0714ac
  fires, or when asked to investigate, repush, or resolve one of these tickets.
---

# Picklebet SGM repush / missing consumer fixture ids

Everything runs through one command. Do not call Sportcast, Datadog, or Jira by hand, and do not build URLs.

```bash
python3 -m picklebet_missing_ids <ISSUE_KEY> --issue-json /tmp/issue.json [--apply]
```

## 1. Get the issue payload

- If `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` are set, omit `--issue-json`. The tool fetches the issue.
- Otherwise write the Jira trigger payload to `/tmp/issue.json`. Include `key`, `summary`, `status`, `description`, the Category, the Operator/s (TSD) field (`customfield_11360`), Organizations, and comments with author email. Either the Jira REST shape (`{"key", "fields": {...}}`) or a flat object works.

The fixtures come from the **description**, one block per line like this:

```text
Sportcast Fixture ID: 551738
Client Fixture ID: sr:match:73220788
Fixture Info: Martinique v El Salvador
```

These tickets have no `client_fixture_id`, `FixtureId`, `apiKey`, or `feedProviders` fields. Do not look for them.

## 2. Decide whether to pass `--apply`

- When triggered by the Jira rule above, pass `--apply`. The rule is the confirmation.
- When a person asks in chat, run without `--apply` first, show the output, and pass `--apply` only after a clear yes.

The tool still refuses to write, even with `--apply`, when:
- the summary says it is a test ("do not touch", "for test purposes"),
- any fixture has kicked off or is in play,
- the ticket's teams differ from Sportcast's,
- Sportcast already has a different Betradar id (adding one would remove it from another fixture),
- the client is not following Betbuilder on the fixture,
- the client fixture id is not a Betradar `sr:match:` id,
- the category is not a Repush SGM / SGM request.

## 3. What `--apply` does (only when every fixture passes)

1. Adds the Betradar consumer id (source 4) where it is missing, then reads it back.
2. Republishes each fixture (`isRepublish=true`).
3. Polls Datadog for up to 3 minutes for `SinglesCreated` with HTTP 200 to Picklebet production. Staging, dev, and t1 endpoints are ignored.
4. If every fixture was delivered: comments to the customer, sets Incident Resolution = Workaround Applied and Resolution Notes, and transitions the ticket to **Resolved**. Without Jira credentials it prints the plan instead.

If delivery is not seen, the ticket stays Under investigation and the output says which fixture is missing.

## 4. Reopened tickets

The tool reads the latest comment from a non-OpenBet author:
- If it confirms the fix, the ticket is resolved with comment `Resolving`.
- If it says the issue persists, or is unclear, the ticket goes back to Under investigation, assigned to OpenBet Support.

## 5. Reply

Post the command's stdout as the reply, unchanged. It carries a `[sgm-agent:...]` marker. If the output is `No change since the last run`, reply with only that line. Do not post the same findings again.

Never paste fixture JSON, cookies, or API keys. The fixture definition contains client API keys.

## Settings

Names and which ones are required are in `.env.example`. Copy it to `.env` and fill in the values. `.env` is gitignored. On a Cursor cloud agent, add the same names as secrets in the cloud environment instead of committing them. The tool loads `.env` from the repo root and does not override variables that are already set.

If a setting is missing, the output says which one. Report that and stop. Do not try another path.

## Checks

```bash
python3 -m unittest picklebet_missing_ids.test_workflow
```
