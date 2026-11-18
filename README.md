# clockify-mcp

A small [MCP](https://modelcontextprotocol.io) server for reading and writing your own
[Clockify](https://clockify.me) time entries. It talks to the Clockify REST API with a personal
API key and runs locally over stdio.

It is built for the Clockify free plan, where the API can be limited to 30 requests per hour for
the whole workspace. The server caches the user and the project list, and reports how many
requests it has made in the last hour with every tool result.

## Tools

| Tool | What it does | API requests |
|---|---|---|
| `clockify_status` | User, workspace, time zone and requests used in the last hour | 1 on first use, then 0 |
| `list_projects` | Projects in the workspace (cached for the session) | 1 per 200 projects, then 0 |
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

## Configuration

All settings are optional environment variables.

| Variable | Default | Purpose |
|---|---|---|
| `CLOCKIFY_API_KEY` | not set | The API key. Takes precedence over the key file. |
| `CLOCKIFY_API_KEY_FILE` | `~/.config/clockify-mcp/api-key` | File holding the API key |
| `CLOCKIFY_API_URL` | `https://api.clockify.me/api/v1` | API base URL. Change it for a regional server, for example `https://euc1.clockify.me/api/v1`. |
| `CLOCKIFY_WORKSPACE_ID` | your active workspace | Workspace to use |

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

- It only handles your own time entries. It does not create projects, manage users or run reports.
- Tasks and tags cannot be set. An update keeps the task and tags an entry already has.
- The request counter only counts requests made by this server, not by other tools or other
  members of the workspace.
