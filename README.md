# Jira Bug Monitor

A local app for the whole team that watches Jira and GitHub, sends Teams alerts when something changes, and shows your tickets, your team's work, time logged, and deployments in one dashboard. Anyone on the team can install it, and any Claude session, in Claude Code (CLI) or Claude Desktop, can query it and log work to the same record.

## What It Does

The dashboard has these tabs:

| Tab | What it shows |
|---|---|
| Overview | KPIs and charts for your bug scope: created vs. resolved, open bugs over time, by priority, status, assignee, and age |
| My Day | Your standup (yesterday, today, blockers), worklogs you may have missed, your watch list, and reviewers for your PRs |
| My Tickets | Everything assigned to you, any type, as a board, a table, or one tab per status, with time spent in every status |
| Team | Team board, active sprint with burnup, releases, review queue, QA handoff, stuck work, forecast, capacity, and release readiness |
| Time Log | Your hours per day from Jira worklogs, time tracked from Claude, and what you did on each ticket; the team's hours |
| Deployments | Every environment: what's running now with step progress, the latest deploy to each, who deployed, what shipped, rollbacks, and release notes drafted for each production deploy |
| Insights | Cycle time by stage, PR health, bug quality, estimate accuracy, delivery metrics, weekly, monthly, and sprint-review reports, and triage help |
| Bugs | Every tracked bug, open or resolved, with an urgency score, SLA timer, and linked PRs |
| Quality | Bug reports missing details, SLA breaches and compliance, and the code areas bugs come from |
| GitHub | Your PRs with reviewers, comments, checks, and merge state; your branches |
| Code Health | Builds on default branches (GitHub Actions and Jenkins), checks, Jenkins builds with stages, flaky tests, security alerts, and suggested reviewers |
| Activity | Alerts, work in progress, and time tracked from Claude |
| Settings (gear icon) | Notifications, quiet hours, summaries, standup, SLA, report checks, code health, channels, teammates, time off, and escalations |

Search everything from the box at the top (press `/`): tickets, descriptions, PRs, branches, deployments, worklog comments, work notes, and security alerts. Open any ticket for its SLA, report gaps, possible causes, similar finished tickets, and a **Start work** button with a branch name and git commands.

Click any ticket, PR, or deployment for its full details: timeline, time in each status, fix estimate, linked PRs and branches, Jira comments, and every environment it reached.

Jira tickets link to GitHub automatically when the key is in a branch name, PR title, or commit message, such as `humfe-4233-fix`.

## How It Fits Together

```
     Jira Cloud and GitHub (read-only, except opt-in worklogs)
                       │  every 5 min
                       ▼
   ┌────────────── bugmonitor run ────────────┐
   │  poller ─► SQLite (data/monitor.db) ◄─ dashboard (localhost:8765)
   │              ▲          │                 │
   └──────────────┼──────────┼─────────────────┘
                  │          └─► alerts: desktop · Teams (private + channels) · Slack · email
     MCP server (one per Claude session, same database)
        ▲                 ▲
  Claude Code CLI    Claude Desktop
```

## Install

You need Python 3.11 or newer and [pipx](https://pipx.pypa.io) (`brew install pipx` on a Mac).

```
pipx install git+https://github.com/anaeembaig/jira-bug-monitor.git
bugmonitor init
bugmonitor run
```

`bugmonitor init` walks you through everything:

- **Jira:** your site, email, and API token, tested before it's saved
- **Projects:** pick from the projects your account can see, plus how many days of history to keep
- **Teams:** your private chat webhook and the lowest priority that should reach you
- **GitHub:** a token and your organizations, also tested
- **Team views:** optional team board, sprint, review queue, and insights
- **Claude:** connects the monitor to Claude Desktop and Claude Code

Tokens go into your Mac Keychain (or Windows Credential Locker), not the settings file. Run `bugmonitor init` again any time to change an answer; your current values are the defaults.

The dashboard opens at <http://127.0.0.1:8765>. To update later, run `pipx upgrade jira-bug-monitor`.

### Where Settings Live

| Install | Settings file |
|---|---|
| pipx on a Mac | `~/Library/Application Support/bugmonitor/config.toml` |
| pipx on Windows | `%APPDATA%\bugmonitor\config.toml` |
| pipx on Linux | `~/.config/bugmonitor/config.toml` |
| Git checkout | `config.toml` in the project folder |

Set `BUGMON_CONFIG` to use a different file. `bugmonitor secrets` shows where each token comes from, and `bugmonitor secrets --move` moves tokens out of the settings file into your credential store.

### Working on the Code

```
git clone https://github.com/anaeembaig/jira-bug-monitor.git
cd jira-bug-monitor
./setup.sh                       # or setup.bat on Windows
.venv/bin/python -m pytest -q
```

Tests run automatically on GitHub for every push, on macOS, Windows, and Linux.

## Notifications

Set everything up in the dashboard under **Settings** (the gear icon), or in the settings file. Changes made in Settings apply right away.

### Where They Go

| Destination | What reaches it |
|---|---|
| Dashboard and desktop pop-ups | Every alert |
| Your private Teams chat | Your alerts. In Teams: Workflows, then **Post to a chat when a webhook request is received**, **Chat with Flow bot**, you as the recipient |
| Team Teams channels | Team events for the projects you choose, with the assignee @mentioned. In the channel: Workflows, then **Post to a channel when a webhook request is received** |
| Slack and email | Your alerts, if configured |

### Events

| Event | Private chat | Team channels |
|---|---|---|
| New bug, priority raised, bug reopened | At or above your lowest priority | At or above the channel's lowest priority |
| A ticket is assigned to you, or yours changes status | Always | No |
| You're @mentioned in a Jira comment | Always | No |
| PR: review requested, comment, approved, changes requested, checks failing, conflicts, merged | Always | No |
| Your tickets get deployed; a deploy with your tickets fails; rollback | Always | Production deploys, failures, and rollbacks |
| Stuck work (too long in a status, or a PR waiting on review) | Yours, once | Once per item |
| Escalation rules, e.g. Critical bug unassigned for 2 hours | If no channel is set | The rule's channel |
| End-of-day time log reminder | When you're under your daily target | No |
| A ticket on your watch list changes or gets a comment | Always | No |
| SLA due soon (your tickets) or breached | Yours | Breaches, to the `[sla] route` channel and channels with the `sla` event |
| A default-branch build breaks | If your commit broke it | Channels with the `ci` event |
| New security alert at or above your severity | Always | Channels with the `security` event |
| Bug Monitor can't sync (3 failures in a row) | Always | No |

Snooze a ticket from its details to keep its alerts on the dashboard only for a few hours or days. Team channels aren't affected.

### Timing

- **Quiet hours and weekends:** non-urgent messages wait, then arrive as one summary. Urgent ones (Blocker, Critical, failed deploys, escalations) still come through unless you turn that off.
- **Daily summary:** your tickets by status, stuck work, PRs waiting on you, new bugs, and yesterday's hours. The team summary goes to channels marked for it.
- **Weekly report:** resolved tickets, merged PRs, deploys, and hours, posted on the day you pick.

Teams messages are color coded (red for failures, amber for warnings, green for approvals, merges, and deploys) and have buttons to open the ticket, PR, or dashboard. Both Jira priority schemes are supported (Highest to Lowest, and Blocker to Trivial); a priority the monitor doesn't recognize is always sent.

```
bugmonitor notifications             # see what's on and where it goes
bugmonitor notifications --activate  # turn on the recommended set
bugmonitor notifications --preview   # send a sample message to Teams
```

## Time Log

The **Time Log** tab shows each day, newest first:

- **Hours logged in Jira** per ticket, with worklog comments, against your daily target
- **Time tracked from Claude** ("I'm starting on HUM-12", "done with HUM-12")
- **What you did**: status changes, Jira comments, PRs opened, merged, or reviewed
- **Worked on but not logged**: tickets with activity and no Jira time

Switch to **Team** for everyone's hours per day. To add worklogs from the dashboard or from Claude, set `allow_jira_write = true` under `[timelog]`. It's off by default, and each use is recorded in the audit log.

Bug Monitor also suggests worklogs you probably forgot: time tracked from Claude that isn't in Jira, and tickets you worked on during days that are short of your target. Accept them on **My Day**. Export your timesheet, or the team's, as a CSV file for Excel from the Time Log tab.

## Daily Workflow

- **Standup:** set `standup_time` in Settings and your note arrives in your Teams chat on workdays. Copy it or post it from **My Day**. Turn on `standup_team` to post everyone's to digest channels.
- **Start work:** in Claude, "start work on HUM-412" returns the description, gaps in the report, similar finished tickets and how they were fixed, possible causes, the SLA, a branch name that follows your naming, and git commands, and starts the timer.
- **Watch list:** follow tickets you don't own and get alerts for status, assignee, priority, and comments.
- **Ticket actions:** with `allow_actions = true` under `[jira]`, change status, assign to yourself, and comment from the dashboard or Claude. It's off by default, and each change is recorded in the audit log.

## Quality and Planning

- **Bug report checks** flag open bugs with no steps to reproduce, no expected result, or no environment, and draft a note to the reporter.
- **SLA timers** track first response and fix time by priority. Turn them on in Settings and set the hours under `[sla]`.
- **Possible causes** lists PRs merged or deployed in the two weeks before a bug was reported that touch the same area.
- **Hotspots** shows the files and folders bug fixes change most.
- **Forecast** replays the last 30 workdays of finished work 2,000 times to give the chance of finishing the sprint or release on time.
- **Capacity** compares each person's focus hours, minus time off from Settings, with their remaining work.
- **Release readiness** is a go/no-go checklist: tickets done, no high-priority bugs, PRs merged, QA finished, and deployed to a test environment.

## Jenkins

Run `bugmonitor init` and answer yes to Jenkins. You'll need the Jenkins address, your Jenkins user ID, and an API token (in Jenkins: your name at the top right, then **Security**, then **Add new token**). The token goes into your credential store. If Jenkins uses a company certificate, set `ca_bundle` under `[jenkins]` to the certificate file from IT.

Bug Monitor reads every job built in the last two weeks, including folders and multibranch projects, or only the jobs you list under `jobs`. For each build it shows:

- **Result and stages**, with live progress for running builds
- **Who started it**, the commits it built, and the Jira tickets they mention
- **Test results** from the JUnit report, used for flaky-test detection
- **Deploy jobs** (names with "deploy" or "release", or your `deploy_jobs` patterns) on the Deployments tab, with the environment taken from a build parameter such as `ENVIRONMENT`

When a default-branch job breaks, you hear about it if your commit broke it, and channels with the `ci` event hear about it too. Build parameters that look like passwords, secrets, tokens, or keys are never stored.

## Flaky Tests

The **Tests** view in Code Health reads Jenkins test reports and JUnit XML files that GitHub Actions uploads as artifacts (artifact names containing "test", "junit", "report", or "results"). A test counts as flaky when it failed and passed on the same commit, or keeps flipping between pass and fail. Tests failing in the last two builds of a pipeline are listed separately as failing now.

## Reports

On the Insights tab:

- **Monthly:** bugs reported and fixed, time to fix, reopens, SLA, finished work, production deploys with delivery metrics, builds and flaky tests, where bugs come from, and work per person
- **Sprint review:** committed vs. completed, work added after the start, what carried over, plus the same bug, delivery, and build numbers for the sprint

Both have **Copy as text** and **Send to my Teams chat**. They also go to your private chat automatically: last month's report on the first workday of the month, and a sprint review when a sprint closes. Turn either off in Settings under Reports.

## Deployments

Bug Monitor reads GitHub deployments for every environment in your repos. GitHub Actions jobs that use `environment:` create these automatically. For each deploy it shows:

- **Who and when:** who deployed, when it started, how long it took, and every status change (queued, in progress, succeeded, failed)
- **What shipped:** the commits since the previous deploy to that environment, their PRs, and the Jira tickets they mention
- **Rollbacks:** a deploy of an older version than the one before it, with the tickets it took back out of that environment
- **Live progress:** deploy pipelines in GitHub Actions, step by step (workflows whose names include "deploy" or "release")
- **Links:** logs and the environment's URL

When a production deploy succeeds, from GitHub or Jenkins, release notes are drafted from the tickets it shipped and kept on the Deployments tab, ready to copy.

Each ticket's details list every environment it reached. A rollback removes the ticket from that environment again, and the QA handoff view uses this to show what's actually testable. Add more repos under `[github] repos`, and rename environments under `[deployments.aliases]`.

## Connect GitHub

1. Create a classic token at <https://github.com/settings/tokens/new> with the **repo** scope. Add **security_events** for security alerts.
2. If `tcp-software` uses single sign-on, open the token, choose **Configure SSO**, and authorize it for the organization.
3. In `config.toml`, add the following:

   ```toml
   [github]
   enabled = true
   token = ""   # blank = credential store (bugmonitor init puts it there)
   orgs = ["tcp-software"]
   jira_prefixes = ["HUMFE", "HUM"]
   ```

4. Run `bugmonitor check`. It should print `GitHub: connected as <you>`.
5. Restart `bugmonitor run`.

`bugmonitor init` does all of this for you.

The monitor finds PRs you authored, were asked to review, reviewed, or are assigned to, from the last 60 days. It also finds your branches in the repos you've opened PRs in. To scan more repos, add them to `repos`. Access is read-only.

## Using It From Claude

Ask in plain language from any session:

- "What new bugs came in today?"
- "Show me the open bugs, highest urgency first."
- "Give me the full history of HUM-412."
- "How long will HUM-409 take to fix?"
- "I'm starting on HUM-412." Then later: "Done with HUM-412. Fixed the null check."
- "What did I work on this week?"
- "Which of my tickets are in QA, and how long have they been there?"
- "Which of my PRs are failing checks?" or "What's waiting on my review?"
- "Has web-schedule#2520 been dev reviewed? Show me the unresolved comments."
- "How did bug intake compare to fixes over the last 60 days?"
- "Where has HUM-412 been deployed, and who deployed it?"
- "What did I work on yesterday, and what haven't I logged?"
- "What's stuck on the team?" or "How's the sprint going?"
- "Write my weekly report." or "Draft release notes for HUM 2026.11."
- "What's my standup?" or "Start work on HUM-412."
- "Watch HUM-500." or "Snooze HUM-388 for a day."
- "Which bug reports are missing details?" or "What might have caused HUM-412?"
- "Are we going to make the sprint?" or "Is 2026.11 ready to release?"
- "Is main green?" or "Who should review my PR?"
- "What time haven't I logged this week?" or "Export my timesheet for the last two weeks."
- "Which tests are flaky?" or "Is the Jenkins build for HUM-412 green?"
- "Give me the release notes for today's production deploy."
- "Write the sprint review for Sprint 41." or "Give me the monthly report for September."

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
| `my_tickets` | Lists your tickets by status, with time in each status and the total |
| `bug_trends` | Shows 60-day trends, breakdowns, and KPIs |
| `my_pull_requests` | Lists your PRs with status, checks, review state, and linked Jira keys |
| `get_pull_request` | Shows one PR in full: reviewers, comments, review threads, checks, and linked Jira tickets |
| `my_branches` | Lists your recent branches with CI status and linked PR |
| `github_summary` | Shows open PRs by status, reviews waiting on you, and failing checks |
| `team_board`, `sprint_status`, `release_status` | Team work by person and status, active sprint, unreleased versions |
| `review_queue`, `qa_handoff`, `stuck_work` | PRs waiting on review, tickets in QA and whether they're testable, stuck items |
| `my_timesheet`, `team_timesheet` | Hours per day and ticket, with what was done |
| `log_time_to_jira` | Adds a Jira worklog (only with `allow_jira_write = true`) |
| `deployments`, `ticket_deployments` | Deploys across environments, and where a ticket has been deployed |
| `delivery_metrics`, `engineering_insights` | Delivery (DORA) metrics, cycle time, PR health, bug quality, estimate accuracy |
| `weekly_report`, `draft_release_notes`, `triage_help` | Ready-to-share report, release notes, duplicate and assignment suggestions |
| `my_standup`, `team_standup` | Yesterday, today, and blockers, for you or per teammate |
| `start_work` | A brief to start a ticket, with branch name and git commands; starts the timer |
| `watch_ticket`, `snooze_ticket` | Follow a ticket, or mute its alerts for a while |
| `search_everything` | Searches tickets, PRs, branches, deployments, worklogs, notes, and security alerts |
| `ticket_action` | Changes status, assigns, or comments (only with `allow_actions = true`) |
| `bug_report_quality`, `similar_tickets`, `regression_suspects` | Report gaps, similar finished tickets, and possible causes |
| `sla_status`, `bug_hotspots` | SLA timers and compliance, and where bugs come from |
| `delivery_forecast`, `team_capacity`, `release_readiness` | Sprint and release forecast, capacity, and go/no-go checks |
| `ci_health`, `suggest_reviewers`, `security_alerts` | Builds, reviewer suggestions, and security alerts |
| `suggested_worklogs`, `export_timesheet` | Worklogs you may have missed, and a CSV timesheet |
| `jenkins_builds`, `flaky_tests` | Jenkins builds by job or ticket, and flaky or failing tests |
| `deploy_release_notes` | Release notes drafted for recent production deploys |
| `monthly_report`, `sprint_review` | Shareable monthly and sprint-review reports |
| `sync_now` | Pulls from Jira and GitHub right away |

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
bugmonitor init              # guided setup (run again to change answers)
bugmonitor run               # sync Jira and GitHub and serve the dashboard (leave running)
bugmonitor sync              # sync once
bugmonitor check             # test Jira and GitHub logins
bugmonitor dashboard         # dashboard only
bugmonitor notifications     # see notifications; --activate, --preview
bugmonitor test-alert        # send a test alert
bugmonitor secrets           # where each token comes from; --move to use the credential store
bugmonitor audit             # changes made from the dashboard or by Claude
bugmonitor doctor            # version, running copies, database locks, exposure checks
bugmonitor install-claude    # connect to Claude Desktop and Claude Code again
```

In a git checkout, use `.venv/bin/python -m bugmonitor` in place of `bugmonitor`.

## Security

Read-only access to Jira and GitHub (except opt-in worklogs and ticket actions), tokens in your credential store, a dashboard that only listens on this computer, and automatic cleanup of old data. See [SECURITY.md](SECURITY.md).
