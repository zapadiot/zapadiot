# Agent instructions

This repo is used by the Cursor cloud agent that Jira starts for TSD tickets.

## Picklebet SGM / Repush SGM tickets

When the ticket is a Picklebet "SGM Request" or the Category is Sportcast > Repush SGM, follow `.cursor/skills/picklebet-missing-ids/SKILL.md`. In short:

1. Save the Jira issue payload you were given to `/tmp/issue.json`. Skip this when `JIRA_API_TOKEN` is set.
2. Run:
   ```bash
   python3 -m picklebet_missing_ids <ISSUE_KEY> --issue-json /tmp/issue.json --apply
   ```
   Pass `--apply` only when Jira automation rule `019f45bf-fbf0-7bbf-b180-ae122b0714ac` started the run. Otherwise run without it and ask first.
3. Reply with the command's stdout, unchanged.

Rules:
- Do not make Sportcast, Datadog, or Jira calls outside the command, and do not invent URLs or endpoints.
- Do not edit code in this repo during a ticket run, and do not open pull requests.
- If the command reports a missing setting or a failed login, reply with that and stop. Do not retry another way.
- Never print fixture JSON, cookies, passwords, or API keys.

## Other tickets

Reply with a short summary of the ticket and `No automated workflow for this ticket type.` Make no changes.
