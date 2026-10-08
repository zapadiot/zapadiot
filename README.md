# zapadiot

Cursor cloud agent workflow for Picklebet SGM / Repush SGM tickets in Jira (TSD).

- `AGENTS.md`: what the cloud agent does when Jira starts it.
- `.cursor/skills/picklebet-missing-ids/SKILL.md`: the full workflow, its safety gates, and required secrets.
- `picklebet_missing_ids/`: the code. Run `python3 -m picklebet_missing_ids <ISSUE_KEY> --issue-json issue.json` for a read-only check, and add `--apply` to fix it.
- `.env.example`: the settings the cloud agent needs. Copy it to `.env` locally, or add the same names as Cursor cloud secrets. Do not commit the values.

```bash
python3 -m unittest picklebet_missing_ids.test_workflow
```
