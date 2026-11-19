# clockify-mcp

A small [MCP](https://modelcontextprotocol.io) server for reading and writing your own
[Clockify](https://clockify.me) time entries. It talks to the Clockify REST API with a personal
API key and runs locally over stdio.

It is frugal with API requests, because a workspace on the Clockify free plan can be limited to
30 requests per hour. The server caches the user and the project list, and reports with every
tool result how many requests it has made in the last hour and, for a free workspace, how many
are left. Whether a workspace is free comes from the subscription type Clockify reports for it.

## Tools

| Tool | What it does | API requests |
|---|---|---|
| `clockify_status` | User, workspace, time zone and requests used in the last hour | 1 on first use (2 if the subscription is not known yet), then 0 |
| `list_workspaces` | Your workspaces, their Clockify subscription type and hourly limit, and which one the tools use | 1 on first use, then 0 |
| `select_workspace` | Make the tools use another workspace, by id or name | 0, or 1 if the workspaces are not listed yet |
| `list_projects` | Projects in the workspace (cached for the session) | 1 per 200 projects, then 0 |
| `create_project` | Create a project, optionally under an existing client; refuses duplicate names | 1, +1 if a client is given |
| `list_time_entries` | Your entries in a date range, with totals per project and per day | 1 per 200 entries |
| `add_time_entry` | Create one entry: project, date, hours, optional start time and description | 1 |
| `update_time_entry` | Change the given fields of an entry | 2 |
| `delete_time_entry` | Permanently delete an entry | 1 |

Dates are `YYYY-MM-DD` and times `HH:MM`, in the time zone set in your Clockify profile. A project
can be given as its id, its name, or a unique part of its name.

## Install

Requires Python 3.11 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## API key

Create an API key in Clockify under **Profile settings > Advanced**, then store it where only you
can read it:

```bash
mkdir -p ~/.config/clockify-mcp
install -m 600 /dev/null ~/.config/clockify-mcp/api-key
$EDITOR ~/.config/clockify-mcp/api-key    # paste the key, save
```

The key gives full access to your Clockify account. Do not commit it or paste it into a chat.

If you upgrade a workspace to a paid plan, create a new key: a key made before the upgrade may
still be held to the free plan's request limit.

## Configuration

### Workspace and request limit

The server keeps a small settings file, `~/.config/clockify-mcp/config.json`:

```json
{
  "workspace": "5c7d...",
  "workspaces": {
    "5c7d...": {"name": "Work", "subscription": "BASIC_2021"},
    "5ca3...": {"name": "Home", "subscription": "FREE_2026"}
  }
}
```

- `workspace` is the workspace the tools use, set by `select_workspace`. Without it, your
  active workspace in Clockify is used.
- `workspaces` is what Clockify's `/workspaces` endpoint last returned, saved by
  `list_workspaces`. A workspace whose subscription starts with `FREE` is limited to 30 requests
  per hour; any other subscription has no limit. Until the subscription is known, the server
  assumes the limit. After an upgrade, run `list_workspaces` with `refresh` to update it.

### Environment variables

All are optional.

| Variable | Default | Purpose |
|---|---|---|
| `CLOCKIFY_API_KEY` | not set | The API key. Takes precedence over the key file. |
| `CLOCKIFY_API_KEY_FILE` | `~/.config/clockify-mcp/api-key` | File holding the API key |
| `CLOCKIFY_API_URL` | `https://api.clockify.me/api/v1` | API base URL. Change it for a regional server, for example `https://euc1.clockify.me/api/v1`. |
| `CLOCKIFY_WORKSPACE_ID` | the workspace in the settings file, else your active workspace | Workspace to use; takes precedence over the settings file at start-up |

## Use with Claude Code

```bash
claude mcp add clockify -- /path/to/clockify-mcp/.venv/bin/clockify-mcp
```

## Tests

The tests run against a fake Clockify API. They need no network access and no API key.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## Limitations

- It only handles your own time entries and creating projects. It does not create clients, manage users or run reports.
- Tasks and tags cannot be set. An update keeps the task and tags an entry already has.
- The request counter only counts requests made by this server, not by other tools or other
  members of the workspace.
