"""MCP server exposing a few Clockify time-entry tools over stdio."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import (
    FREE_PLAN_HOURLY_LIMIT,
    ClockifyClient,
    ClockifyError,
    hourly_limit_for,
    load_api_key,
    summarize,
)

mcp = MCPServer(
    "clockify",
    instructions=(
        "Tools for the user's own Clockify time entries. A workspace whose Clockify "
        "subscription is free (or not known yet) is treated as limited to "
        f"{FREE_PLAN_HOURLY_LIMIT} API requests per hour; other subscriptions are not. Every "
        "result reports api_usage; when hourly_limit is set, plan calls (each add, "
        "update or delete costs one or two requests) and check clockify_status before "
        "a larger batch. Use list_workspaces and select_workspace to work in another "
        "workspace. Dates are YYYY-MM-DD and times HH:MM in the user's Clockify time zone."
    ),
)

_client: ClockifyClient | None = None


def client() -> ClockifyClient:
    global _client
    if _client is None:
        _client = ClockifyClient(load_api_key())
    return _client


def _usage() -> dict[str, Any]:
    c = client()
    used = c.requests.count()
    limit = c.hourly_limit
    return {
        "requests_last_hour": used,
        "subscription": c.subscription,
        "hourly_limit": limit,  # None means no limit for this subscription
        "remaining": None if limit is None else max(0, limit - used),
    }


def _fail(exc: ClockifyError) -> ToolError:
    # A ToolError's message is passed on to the MCP client; other exceptions are masked.
    return ToolError(str(exc))


@mcp.tool()
def clockify_status() -> dict[str, Any]:
    """Show the Clockify user, workspace, time zone and API requests used in the last hour.

    Costs one request the first time it is called in a session, plus one if the
    workspace's subscription type is not known yet; none after that.
    """
    try:
        c = client()
        user = c.user()
        if c.subscription is None:
            c.workspaces()
        return {
            "user": user.get("name"),
            "workspace_id": c.workspace_id,
            "time_zone": str(c.tz),
            "api_usage": _usage(),
        }
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def list_workspaces(refresh: bool = False) -> dict[str, Any]:
    """List the user's Clockify workspaces and show which one the tools use.

    Each workspace shows its Clockify subscription type and the hourly request
    limit that follows from it (free: 30, others: none). The answer is saved, so
    the limit is known after a restart. Costs one request the first time (cached
    for the session; refresh to fetch again, for example after an upgrade).
    """
    try:
        c = client()
        current = c.workspace_id
        items = [
            {**w, "hourly_limit": hourly_limit_for(w["subscription"]), "in_use": w["id"] == current}
            for w in c.workspaces(refresh=refresh)
        ]
        return {"workspaces": items, "api_usage": _usage()}
    except ClockifyError as exc:
        raise _fail(exc)


@mcp.tool()
def select_workspace(workspace: str) -> dict[str, Any]:
    """Make the tools use another workspace, given by id or (part of) its name.

    The choice is saved and kept after a restart (unless CLOCKIFY_WORKSPACE_ID is
    set). Clockify's own active workspace is not changed. Costs one request if the workspace list is not cached.
    """
    try:
        chosen = client().select_workspace(workspace)
        return {"selected": chosen, "api_usage": _usage()}
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
def create_project(
    name: str,
    client_name: str = "",
    is_public: bool | None = None,
    billable: bool | None = None,
) -> dict[str, Any]:
    """Create a project in the workspace.

    Refuses a name that already exists (checked against the cached project list).
    client_name must match an existing client exactly; it is not created.
    Costs one request, plus one to look up the client if given, plus one to load
    the project list if it is not cached yet.
    """
    try:
        project = client().create_project(
            name, client=client_name, is_public=is_public, billable=billable
        )
        return {"created": project.as_dict(), "api_usage": _usage()}
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
