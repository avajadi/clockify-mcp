"""MCP server exposing a few Clockify time-entry tools over stdio."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import (
    FREE_PLAN_HOURLY_LIMIT,
    ClockifyClient,
    ClockifyError,
    load_api_key,
    summarize,
)

mcp = MCPServer(
    "clockify",
    instructions=(
        "Tools for the user's own Clockify time entries. The free plan can be limited "
        f"to {FREE_PLAN_HOURLY_LIMIT} API requests per hour for the whole workspace, so "
        "plan calls: each add, update or delete costs one or two requests. Check "
        "clockify_status before a larger batch. Dates are YYYY-MM-DD and times HH:MM "
        "in the user's Clockify time zone."
    ),
)

_client: ClockifyClient | None = None


def client() -> ClockifyClient:
    global _client
    if _client is None:
        _client = ClockifyClient(load_api_key())
    return _client


def _usage() -> dict[str, Any]:
    used = client().requests.count()
    return {
        "requests_last_hour": used,
        "free_plan_hourly_limit": FREE_PLAN_HOURLY_LIMIT,
        "remaining_if_limited": max(0, FREE_PLAN_HOURLY_LIMIT - used),
    }


def _fail(exc: ClockifyError) -> ToolError:
    # A ToolError's message is passed on to the MCP client; other exceptions are masked.
    return ToolError(str(exc))


@mcp.tool()
def clockify_status() -> dict[str, Any]:
    """Show the Clockify user, workspace, time zone and API requests used in the last hour.

    Costs one request the first time it is called in a session, none after that.
    """
    try:
        c = client()
        user = c.user()
        return {
            "user": user.get("name"),
            "workspace_id": c.workspace_id,
            "time_zone": str(c.tz),
            "api_usage": _usage(),
        }
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def list_projects(include_archived: bool = False, refresh: bool = False) -> dict[str, Any]:
    """List the projects in the workspace.

    The list is cached for the session; set refresh to fetch it again.
    """
    try:
        projects = client().projects(refresh=refresh)
        shown = [p.as_dict() for p in projects if include_archived or not p.archived]
        shown.sort(key=lambda p: p["name"].casefold())
        return {"projects": shown, "api_usage": _usage()}
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def list_time_entries(start_date: str, end_date: str, summary_only: bool = False) -> dict[str, Any]:
    """List the user's own time entries in an inclusive date range, with totals.

    Returns each entry (id, date, start, end, hours, project, description) and a
    summary with total hours, hours and share per project, and hours per day.
    Set summary_only to leave out the individual entries.
    """
    try:
        entries = client().time_entries(start_date, end_date)
        result: dict[str, Any] = {"summary": summarize(entries), "entry_count": len(entries)}
        if not summary_only:
            result["entries"] = entries
        result["api_usage"] = _usage()
        return result
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def add_time_entry(
    project: str,
    date: str,
    hours: float,
    start_time: str = "08:00",
    description: str = "",
    billable: bool | None = None,
) -> dict[str, Any]:
    """Create one time entry for the user.

    project is a project id, name, or unique part of a name. The entry runs from
    start_time for the given number of decimal hours on the given date. Entries
    may overlap; pick start_time yourself when adding several on the same day.
    """
    try:
        entry = client().add_entry(
            project=project,
            day=date,
            hours=hours,
            start_time=start_time,
            description=description,
            billable=billable,
        )
        return {"created": entry, "api_usage": _usage()}
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def update_time_entry(
    entry_id: str,
    project: str | None = None,
    date: str | None = None,
    hours: float | None = None,
    start_time: str | None = None,
    description: str | None = None,
    billable: bool | None = None,
) -> dict[str, Any]:
    """Change an existing time entry. Only the fields you give are changed.

    Costs two requests (read, then write).
    """
    try:
        entry = client().update_entry(
            entry_id,
            project=project,
            day=date,
            hours=hours,
            start_time=start_time,
            description=description,
            billable=billable,
        )
        return {"updated": entry, "api_usage": _usage()}
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def delete_time_entry(entry_id: str) -> dict[str, Any]:
    """Permanently delete one time entry. This cannot be undone."""
    try:
        client().delete_entry(entry_id)
        return {"deleted": entry_id, "api_usage": _usage()}
    except ClockifyError as exc:
        raise _fail(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
