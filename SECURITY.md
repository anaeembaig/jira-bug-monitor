# Security and Data

Bug Monitor runs on your own computer. Nothing is sent anywhere except the services you connect (Jira, GitHub, and the Teams, Slack, or email destinations you set up).

## What It Reads and Writes

| Service | Access | Notes |
|---|---|---|
| Jira | Read-only | Two opt-in exceptions, both off by default and recorded in the audit log: **Log time** adds a worklog (`[timelog] allow_jira_write = true`), and ticket actions change status, assign, or comment (`[jira] allow_actions = true`) |
| GitHub | Read-only | Pull requests, reviews, comments, checks, files changed, branches, deployments, Actions runs, and security alerts |
| Jenkins | Read-only | Jobs, builds, stages, change sets, and test reports. Build parameters that look like passwords, secrets, tokens, or keys are never stored |
| Teams, Slack, email | Send only | Messages go only to the webhooks and addresses you configure |

## Where Data Lives

| Item | Location | Protection |
|---|---|---|
| Settings | `config.toml` (see "Where Settings Live" in the README) | Readable only by you (mode 600 on macOS and Linux) |
| Tokens and webhook URLs | Your OS credential store: macOS Keychain, Windows Credential Locker, or Secret Service on Linux | Never written to the settings file when a credential store is available. Run `bugmonitor secrets` to see where each one comes from |
| Synced data | `data/monitor.db` next to the settings file | Readable only by you (mode 600, folder 700) |

## Tokens

- **Jira:** a classic API token from your Atlassian account. It has the same access as you; Bug Monitor only uses it to read, except for opt-in worklogs and ticket actions.
- **GitHub:** a classic token with the `repo` scope (read-only use), plus `security_events` for security alerts. Set an expiry. If your organization uses single sign-on, authorize the token for it.
- **Jenkins:** a personal API token, kept in the credential store. Test artifacts are parsed with a hardened XML parser and size limits.
- **Revoking:** delete the token at id.atlassian.com or github.com/settings/tokens, then run `bugmonitor init` to add a new one.

## The Dashboard

- Listens only on `127.0.0.1` (this computer). It refuses other addresses unless you set `[dashboard] allow_network = true`. It has no login, so don't do that on a shared network.
- Rejects requests with an unexpected `Host` header (blocks DNS rebinding), and every change (POST) needs a header that other websites can't send.
- Sends `Content-Security-Policy`, `X-Frame-Options: DENY`, and `nosniff` headers.

## Retention

Once a day Bug Monitor deletes read alerts after 90 days, audit entries after 365 days, local work logs after 365 days, and data for tickets that left your filters. Change these under `[storage]` (`keep_alerts_days`, `keep_audit_days`, `keep_work_log_days`).

## Audit Log

Changes made from the dashboard or by Claude (settings, worklogs written to Jira) are recorded. View them with `bugmonitor audit`.

## Reporting a Problem

Email the maintainer instead of opening a public issue.
