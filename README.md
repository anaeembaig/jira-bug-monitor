# Jira Bug Monitor

A local app that watches Jira for bugs, alerts you when something changes, tracks every status change, and estimates fix times. Any Claude session — Claude Code (CLI) or Claude Desktop — can query it and log work to the same shared record.

## What It Does

- **New bug alerts** on the dashboard, desktop pop-ups, Slack, Teams, and email
- **Live status board** with every open bug, sorted by an urgency score from 0 to 100
- **Full timeline** for each ticket, built from the Jira changelog plus work you log from Claude
- **Time in status** for each ticket, such as 3 hours in To Do and 6 hours in In Progress
- **Fix estimates** based on how long similar resolved bugs took
- **Work records** from any Claude session, stored in one shared database

## How It Fits Together

```
            Jira Cloud (REST API, read-only)
                       │  every 5 min
                       ▼
   ┌──────── python -m bugmonitor run ────────┐
   │  poller ─► SQLite (data/monitor.db) ◄─ dashboard (localhost:8765)
   │              ▲          │                 │
   └──────────────┼──────────┼─────────────────┘
                  │          └─► alerts: desktop · Slack · Teams · email
     MCP server (one per Claude session, same database)
        ▲                 ▲
  Claude Code CLI    Claude Desktop
```

## Setup

You need Python 3.11 or newer.

1. Unzip this folder into Documents. The result should be `Documents/jira-bug-monitor`.
2. Run setup:
   - **Windows:** double-click `setup.bat`
   - **Mac:** open Terminal in the folder and run `./setup.sh`

   This creates a virtual environment, installs dependencies, creates `config.toml`, and connects the monitor to Claude Desktop and Claude Code.
3. Create a Jira API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.
4. Open `config.toml` and set `base_url`, `email`, and `api_token`. The token can also go in the `JIRA_API_TOKEN` environment variable.
5. Test the connection:
   - **Windows:** `.venv\Scripts\python -m bugmonitor check`
   - **Mac:** `.venv/bin/python -m bugmonitor check`
6. Start the monitor with `start.bat` or `./start.sh`. The dashboard opens at <http://127.0.0.1:8765>.
7. Restart Claude Desktop. In Claude Code, run `/mcp` and confirm `jira-bug-monitor` is listed.

The first sync loads every open bug, plus bugs resolved in the last 90 days, so estimates have history. It doesn't alert on existing bugs. Alerts start with the next poll.

## Alert Channels

Set channels in the `[alerts]` section of `config.toml`. Then run `python -m bugmonitor test-alert` to test them.

| Channel | Setup |
|---|---|
| Dashboard | Always on |
| Desktop | `desktop = true`. On a Mac, allow notifications for **Script Editor** under **System Settings**, then **Notifications** |
| Slack | Create an [Incoming Webhook](https://api.slack.com/messaging/webhooks) and paste the URL into `slack_webhook` |
| Teams (private) | In Teams, open **Apps**, then **Workflows**. Choose **Post to a chat when a webhook request is received**, pick **Chat with Flow bot**, and set yourself as the recipient. Paste the URL into `teams_webhook`. Alerts arrive as a private 1:1 chat that only you see |
| Email | Set `email_enabled = true` and fill in the SMTP settings |

Alerts fire for these events:

- A new bug appears
- A bug's priority goes up
- A resolved bug is reopened

Slack, Teams, and email only fire at or above `external_min_priority`, which defaults to High. That keeps busy channels quiet.

## Using It From Claude

Ask in plain language from any session:

- "What new bugs came in today?"
- "Show me the open bugs, highest urgency first."
- "Give me the full history of HUM-412."
- "How long will HUM-409 take to fix?"
- "I'm starting on HUM-412." Then later: "Done with HUM-412. Fixed the null check."
- "What did I work on this week?"

### Available Tools

| Tool | Purpose |
|---|---|
| `bug_summary` | Shows counts by priority, status, and project, plus sync health |
| `list_open_bugs` | Lists open bugs by urgency, with optional filters |
| `list_new_bugs` | Lists bugs created in the last N hours |
| `get_ticket` | Shows fields, time in status, timeline, alerts, and an estimate |
| `estimate_fix` | Estimates fix time from similar resolved bugs |
| `log_work` | Records `start`, `stop`, `note`, `blocked`, or `done` |
| `active_work` | Shows tickets someone is working on now, from any session |
| `work_report` | Shows time logged per ticket |
| `list_alerts` and `acknowledge_alerts` | Shows the alert inbox and marks alerts as read |
| `sync_now` | Pulls from Jira right away |

Each entry records whether it came from `claude-cli` or `claude-desktop`.

## How Urgency and Estimates Work

**Urgency score (0–100)** adds these factors:

| Factor | Points |
|---|---|
| Jira priority | 55 |
| Age, capped at 30 days | 10 |
| Unassigned | 10 |
| Urgent words such as *customer*, *production*, *regression*, *outage*, or *payroll* | 15 |
| Reopens | 10 |
| No update in 7 or more days | 5 |

To change the weights, edit `urgency_score` in `bugmonitor/analytics.py`.

**Fix estimate** compares the ticket with resolved bugs. It uses the closest group that has at least 5 bugs:

1. Same project, component, and priority
2. Same project and priority
3. Same project
4. All resolved bugs

It measures hands-on time, meaning the time spent in statuses Jira marks as in progress. If that history is thin, it uses open-to-resolved time instead. The estimate includes:

- The median time
- The time 80% of similar bugs finish within
- The time already spent on this ticket
- A confidence level

If the ticket has a Jira original estimate, that appears too. Estimates use calendar hours and get more accurate as history builds.

## Run It at Login

- **Windows:** press Win+R, type `shell:startup`, and add a shortcut to `start.bat` with the target `start.bat --no-browser`
- **Mac:** open **System Settings**, go to **General**, then **Login Items**, and add `start.sh`. You can also wrap it in a launchd agent.

The Claude tools still work when the poller is off. Data refreshes when you ask Claude to sync.

## Commands

```
python -m bugmonitor run          # poll Jira and serve the dashboard (leave running)
python -m bugmonitor sync         # sync once
python -m bugmonitor check        # test Jira credentials
python -m bugmonitor dashboard    # dashboard only
python -m bugmonitor test-alert   # send a test alert
python -m bugmonitor install-claude  # re-register with Claude Desktop and Claude Code
python -m bugmonitor doctor       # show version, running copies, and database locks
python -m pytest -q               # run tests
```

## Notes

- Access to Jira is read-only. The app never changes tickets.
- Everything stays on your computer. The dashboard only listens on `127.0.0.1`.
- `config.toml` holds your token. Keep it out of Git. It's already in `.gitignore`.
- The default scope is every bug your Jira account can see. To narrow it, change `jql`, for example `issuetype = Bug AND project in (HUM, TCP)`.
