# Industrial Operations Coworker

A small scheduled bot for the Industrial Operations team. It reads Linear and posts to Slack as its own user, **Industrial Operations Coworker**.

| When (Lisbon) | What |
|---|---|
| Weekdays 09:00 | One DM per person: blocked, late, due today, gone quiet (no update in 3+ working days), due this week, in flight without a due date, and PRs waiting on their review. Nobody is DMed on a day they have nothing to act on. |
| Weekdays 09:00, lead only | Also: `needs-decision` issues assigned to you, unread Linear mentions, Slack messages you haven't answered in 24h+, and a one-line team total. |
| Fridays 16:00 | A summary DM to Joaquim and João, using the same rules as the board's Week tab: shipped against a usual week, what slipped, late and blocked now, cycle forecast, decisions needed, projects at risk, and who is over capacity next week. No per-person ranking, nothing posted to channels. |

The team board: https://claude.ai/artifact/CdeQXEm882JJMMXQf2huVh (weekly view: add `#week`)

## Layout

```
.github/workflows/coworker.yml   # schedule + manual run
coworker-bot/
  config.yaml                    # people, Slack IDs, links, rules: edit this, not the code
  coworker/                      # clients.py (APIs), logic.py (rules + messages), main.py (jobs)
  tests/                         # pytest, no network
  slack-app-manifest.yaml        # paste into "Create app → From manifest"
```

## Setup, once

1. **Slack app.** api.slack.com/apps → Create New App → From an app manifest → pick the Procimo workspace → paste `slack-app-manifest.yaml` → Create → Install to Workspace.
   * Copy the **Bot User OAuth Token** (`xoxb-…`).
   * Optional, for "Slack messages you haven't answered": install as yourself and copy the **User OAuth Token** (`xoxp-…`). It can only read what you can read, and is used only for your own DM.
2. **Linear API key.** Linear → Settings → Security & access → Personal API keys → new key, read access is enough. A service account works the same way if you'd rather not use yours.
3. **GitHub token (optional).** A fine-grained token with read access to Procimo's repositories (pull requests: read) enables "PRs waiting on your review" in each DM.
4. **Repository secrets** (Settings → Secrets and variables → Actions):
   * `COWORKER_LINEAR_API_KEY`
   * `COWORKER_SLACK_BOT_TOKEN`
   * `COWORKER_SLACK_LEAD_USER_TOKEN` (optional)
   * `COWORKER_GITHUB_TOKEN` (optional)

   Until the two required secrets exist, scheduled runs log "Secrets not set yet" and exit without failing.
5. **`config.yaml` is filled in.** Slack user IDs (including João for the Friday DM) were looked up already, so the bot doesn't depend on emails matching in Slack. Linear emails were checked against Linear's user list.

## First run: preview everything in your own DMs

Actions → Industrial Operations Coworker → Run workflow:

* mode `daily`, flags `--force --only-to-lead`: every person's DM arrives in **your** DMs, each labelled with who it was for. Nothing goes to anyone else.
* mode `weekly`, flags `--force --dry-run`: prints the Friday summary in the job log without sending it.

When the previews look right, the schedule takes over; nothing else to switch on.

Locally:

```bash
cd coworker-bot && pip install -r requirements.txt pytest && python -m pytest -q
LINEAR_API_KEY=… SLACK_BOT_TOKEN=… python -m coworker.main daily --force --dry-run
```

## Notes

* GitHub runs scheduled jobs late at busy times, often by 5–15 minutes.
* Each job runs at two UTC hours and only acts in the right Lisbon hour, so it survives the summer-time change.
* If a Linear query is rejected after a Linear API change, the error names the field; the queries are at the top of `main.py`.
