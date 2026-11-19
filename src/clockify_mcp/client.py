"""Thin Clockify REST client, built to be frugal with API requests.

A workspace on the free plan can be limited to 30 requests per hour, so the
client caches the user and the project list, and keeps a persistent count of
the requests it has made during the last hour. The hourly limit follows the
subscription type Clockify reports for the workspace (see hourly_limit_for).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

DEFAULT_API_URL = "https://api.clockify.me/api/v1"
DEFAULT_KEY_FILE = Path.home() / ".config" / "clockify-mcp" / "api-key"
DEFAULT_USAGE_FILE = Path.home() / ".cache" / "clockify-mcp" / "requests.json"
DEFAULT_CONFIG_FILE = Path.home() / ".config" / "clockify-mcp" / "config.json"
FREE_PLAN_HOURLY_LIMIT = 30
PAGE_SIZE = 200


class ClockifyError(Exception):
    """An error that is safe and useful to show to the MCP client."""


def load_api_key() -> str:
    """Read the API key from CLOCKIFY_API_KEY or the key file."""
    key = os.environ.get("CLOCKIFY_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(os.environ.get("CLOCKIFY_API_KEY_FILE", DEFAULT_KEY_FILE))
    if key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise ClockifyError(
        "No Clockify API key found. Set CLOCKIFY_API_KEY, or put the key in "
        f"{key_file} (chmod 600). Create the key in Clockify under "
        "Profile settings > Advanced."
    )


class Settings:
    """Small JSON settings file kept by the MCP.

    {
      "workspace": "<id>",                    # workspace the tools use
      "workspaces": {"<id>": {"name": "RISE", "subscription": "BASIC_2021"}}
    }

    The workspaces part is a copy of what Clockify's /workspaces endpoint
    returned the last time it was asked; it is not meant to be edited.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, Any] = {}
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ClockifyError(f"Could not read the settings file {path}: {exc}") from exc

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as exc:
            raise ClockifyError(f"Could not write the settings file {self.path}: {exc}") from exc

    def subscription_for(self, workspace_id: str | None) -> str | None:
        return (self.data.get("workspaces") or {}).get(workspace_id or "", {}).get("subscription")

    def record_workspaces(self, workspaces: list[dict[str, Any]]) -> None:
        self.data["workspaces"] = {
            w["id"]: {"name": w["name"], "subscription": w["subscription"]} for w in workspaces
        }
        self.save()

    def select(self, workspace_id: str) -> None:
        self.data["workspace"] = workspace_id
        self.save()


def hourly_limit_for(subscription: str | None) -> int | None:
    """Requests per hour: limited for a free subscription, and also while the
    subscription is not known yet; no limit for any other subscription."""
    if subscription is None or subscription.upper().startswith("FREE"):
        return FREE_PLAN_HOURLY_LIMIT
    return None


class RequestLog:
    """Persistent record of request timestamps for the last hour."""

    def __init__(self, path: Path | None):
        self.path = path
        self._stamps: list[float] = []
        if path and path.is_file():
            try:
                self._stamps = [float(x) for x in json.loads(path.read_text())]
            except (ValueError, OSError):
                self._stamps = []

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        self._stamps = [s for s in self._stamps if s > cutoff]

    def record(self) -> None:
        self._prune()
        self._stamps.append(time.time())
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._stamps))
            except OSError:
                pass  # counting is best effort

    def count(self) -> int:
        self._prune()
        return len(self._stamps)


def to_utc_iso(moment: datetime) -> str:
    """Format an aware datetime the way Clockify expects (UTC, Z suffix)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClockifyError(f"Invalid date '{value}'; use YYYY-MM-DD.") from exc


def parse_time(value: str) -> dtime:
    try:
        return dtime.fromisoformat(value)
    except ValueError as exc:
        raise ClockifyError(f"Invalid time '{value}'; use HH:MM.") from exc


@dataclass
class Project:
    id: str
    name: str
    client: str
    archived: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "client": self.client,
            "archived": self.archived,
        }


class ClockifyClient:
    def __init__(
        self,
        api_key: str,
        *,
        api_url: str | None = None,
        workspace_id: str | None = None,
        usage_file: Path | None = DEFAULT_USAGE_FILE,
        config_file: Path | None = DEFAULT_CONFIG_FILE,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=(api_url or os.environ.get("CLOCKIFY_API_URL") or DEFAULT_API_URL).rstrip("/"),
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self.settings = Settings(config_file)
        self._workspace_override = (
            workspace_id or os.environ.get("CLOCKIFY_WORKSPACE_ID") or self.settings.data.get("workspace")
        )
        self.requests = RequestLog(usage_file)
        self._user: dict[str, Any] | None = None
        self._projects: list[Project] | None = None
        self._workspaces: list[dict[str, Any]] | None = None

    # -- low level -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.requests.record()
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ClockifyError(f"Could not reach Clockify: {exc}") from exc
        if response.status_code == 429:
            raise ClockifyError(
                "Clockify rate limit reached (HTTP 429). The workspace subscription is "
                f"'{self.subscription or 'not known yet'}'; a free workspace can be limited to "
                f"{FREE_PLAN_HOURLY_LIMIT} requests per hour. "
                f"This server has made {self.requests.count()} in the last hour. Wait and try "
                "again. If the workspace was upgraded, an API key made before the upgrade may "
                "still be held to the free limit; create a new key."
            )
        if response.status_code == 401:
            raise ClockifyError("Clockify rejected the API key (HTTP 401).")
        if response.status_code >= 400:
            detail = response.text[:300]
            try:
                detail = response.json().get("message", detail)
            except (ValueError, AttributeError):
                pass
            raise ClockifyError(
                f"Clockify returned HTTP {response.status_code} for {method} {path}: {detail}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- user and workspace --------------------------------------------------

    def user(self) -> dict[str, Any]:
        if self._user is None:
            self._user = self._request("GET", "/user")
        return self._user

    @property
    def user_id(self) -> str:
        return self.user()["id"]

    @property
    def workspace_id(self) -> str:
        if self._workspace_override:
            return self._workspace_override
        user = self.user()
        workspace = user.get("activeWorkspace") or user.get("defaultWorkspace")
        if not workspace:
            raise ClockifyError("Could not determine the Clockify workspace; set CLOCKIFY_WORKSPACE_ID.")
        return workspace

    def known_workspace_id(self) -> str | None:
        """The workspace id if it is known without an API request."""
        if self._workspace_override:
            return self._workspace_override
        if self._user:
            return self._user.get("activeWorkspace") or self._user.get("defaultWorkspace")
        return None

    @property
    def subscription(self) -> str | None:
        """The workspace's subscription type as Clockify last reported it."""
        return self.settings.subscription_for(self.known_workspace_id())

    @property
    def hourly_limit(self) -> int | None:
        return hourly_limit_for(self.subscription)

    def workspaces(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The user's workspaces (one request, then cached). Their names and
        subscription types are saved in the settings file."""
        if self._workspaces is None or refresh:
            items = self._request("GET", "/workspaces") or []
            self._workspaces = [
                {
                    "id": w["id"],
                    "name": w.get("name", ""),
                    "subscription": w.get("featureSubscriptionType"),
                }
                for w in items
            ]
            self.settings.record_workspaces(self._workspaces)
        return self._workspaces

    def select_workspace(self, workspace: str) -> dict[str, Any]:
        """Use another workspace from now on, by id or (part of) its name.

        The choice is saved in the settings file. Clockify's own active
        workspace is not changed.
        """
        wanted = workspace.strip().casefold()
        if not wanted:
            raise ClockifyError("No workspace given.")
        options = self.workspaces()
        match = [w for w in options if w["id"] == workspace.strip()]
        match = match or [w for w in options if w["name"].casefold() == wanted]
        match = match or [w for w in options if wanted in w["name"].casefold()]
        if len(match) != 1:
            names = ", ".join(f"'{w['name']}'" for w in (match or options)[:10])
            what = "several" if match else "no"
            raise ClockifyError(f"'{workspace}' matches {what} workspaces. Choose one of: {names}.")
        chosen = match[0]
        if chosen["id"] != self._workspace_override:
            self._projects = None  # projects belong to a workspace
        self._workspace_override = chosen["id"]
        self.settings.select(chosen["id"])
        return {**chosen, "hourly_limit": self.hourly_limit}

    @property
    def tz(self) -> ZoneInfo:
        name = (self.user().get("settings") or {}).get("timeZone") or "UTC"
        try:
            return ZoneInfo(name)
        except Exception:  # unknown zone name
            return ZoneInfo("UTC")

    # -- projects ------------------------------------------------------------

    def projects(self, *, refresh: bool = False) -> list[Project]:
        """All projects in the workspace (cached for the life of the process)."""
        if self._projects is None or refresh:
            found: list[Project] = []
            page = 1
            while True:
                batch = self._request(
                    "GET",
                    f"/workspaces/{self.workspace_id}/projects",
                    params={"page": page, "page-size": PAGE_SIZE},
                )
                for item in batch or []:
                    found.append(
                        Project(
                            id=item["id"],
                            name=item.get("name", ""),
                            client=item.get("clientName") or "",
                            archived=bool(item.get("archived")),
                        )
                    )
                if not batch or len(batch) < PAGE_SIZE:
                    break
                page += 1
            self._projects = found
        return self._projects

    def resolve_project(self, project: str) -> Project:
        """Find a project by id, exact name, or unique part of the name."""
        wanted = project.strip()
        if not wanted:
            raise ClockifyError("No project given.")
        candidates = self.projects()
        for p in candidates:
            if p.id == wanted:
                return p
        active = [p for p in candidates if not p.archived]
        lowered = wanted.casefold()
        exact = [p for p in active if p.name.casefold() == lowered]
        if len(exact) == 1:
            return exact[0]
        partial = exact or [p for p in active if lowered in p.name.casefold()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise ClockifyError(
                f"No active project matches '{project}'. Use list_projects to see the names."
            )
        names = ", ".join(f"'{p.name}'" for p in partial[:10])
        raise ClockifyError(f"'{project}' matches several projects: {names}. Be more specific or use the id.")

    def find_client_id(self, name: str) -> str:
        """Id of the existing client with exactly this name (one request)."""
        wanted = name.strip().casefold()
        batch = self._request(
            "GET",
            f"/workspaces/{self.workspace_id}/clients",
            params={"name": name.strip(), "page-size": PAGE_SIZE},
        )
        matches = [c for c in batch or [] if (c.get("name") or "").strip().casefold() == wanted]
        if len(matches) != 1:
            found = ", ".join(f"'{c.get('name')}'" for c in (batch or [])[:10]) or "none"
            raise ClockifyError(
                f"Expected one client named '{name}', found {len(matches)} (similar: {found}). "
                "Create the client in Clockify first, or leave the client out."
            )
        return matches[0]["id"]

    def create_project(
        self,
        name: str,
        *,
        client: str = "",
        is_public: bool | None = None,
        billable: bool | None = None,
    ) -> Project:
        """Create a project. Refuses a name that already exists, archived or not."""
        name = name.strip()
        if not name:
            raise ClockifyError("No project name given.")
        clash = [p for p in self.projects() if p.name.casefold() == name.casefold()]
        if clash:
            state = "archived" if clash[0].archived else "active"
            raise ClockifyError(
                f"A project named '{clash[0].name}' already exists ({state}, id {clash[0].id})."
            )
        body: dict[str, Any] = {"name": name}
        if client.strip():
            body["clientId"] = self.find_client_id(client)
        if is_public is not None:
            body["isPublic"] = is_public
        if billable is not None:
            body["billable"] = billable
        item = self._request("POST", f"/workspaces/{self.workspace_id}/projects", json=body)
        project = Project(
            id=item["id"],
            name=item.get("name", name),
            client=item.get("clientName") or client.strip(),
            archived=bool(item.get("archived")),
        )
        if self._projects is not None:
            self._projects.append(project)
        return project

    def project_name(self, project_id: str | None) -> str:
        if not project_id:
            return "(no project)"
        for p in self.projects():
            if p.id == project_id:
                return p.name
        return project_id

    # -- time entries --------------------------------------------------------

    def local_range(self, start_date: str, end_date: str) -> tuple[datetime, datetime]:
        """Local-midnight bounds for an inclusive date range."""
        first, last = parse_date(start_date), parse_date(end_date)
        if last < first:
            raise ClockifyError("end_date is before start_date.")
        start = datetime.combine(first, dtime.min, tzinfo=self.tz)
        end = datetime.combine(last + timedelta(days=1), dtime.min, tzinfo=self.tz)
        return start, end

    def time_entries(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """The user's own entries in an inclusive local date range."""
        start, end = self.local_range(start_date, end_date)
        raw: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"/workspaces/{self.workspace_id}/user/{self.user_id}/time-entries",
                params={
                    "start": to_utc_iso(start),
                    "end": to_utc_iso(end),
                    "page": page,
                    "page-size": PAGE_SIZE,
                },
            )
            raw.extend(batch or [])
            if not batch or len(batch) < PAGE_SIZE:
                break
            page += 1
        entries = [self.describe_entry(item) for item in raw]
        entries.sort(key=lambda e: e["start"])
        return entries

    def describe_entry(self, item: dict[str, Any]) -> dict[str, Any]:
        """Flatten a Clockify entry into local times and decimal hours."""
        interval = item.get("timeInterval") or {}
        start = parse_iso(interval["start"]).astimezone(self.tz)
        end_raw = interval.get("end")
        end = parse_iso(end_raw).astimezone(self.tz) if end_raw else None
        hours = round((end - start).total_seconds() / 3600, 2) if end else None
        return {
            "id": item["id"],
            "date": start.date().isoformat(),
            "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M") if end else None,
            "hours": hours,
            "running": end is None,
            "project_id": item.get("projectId"),
            "project": self.project_name(item.get("projectId")),
            "description": item.get("description") or "",
            "billable": item.get("billable"),
        }

    def _entry_body(
        self,
        *,
        day: date,
        start_time: dtime,
        hours: float,
        project_id: str | None,
        description: str,
        billable: bool | None,
    ) -> dict[str, Any]:
        if hours <= 0 or hours > 24:
            raise ClockifyError("hours must be greater than 0 and at most 24.")
        start = datetime.combine(day, start_time, tzinfo=self.tz)
        end = start + timedelta(seconds=round(hours * 3600))
        body: dict[str, Any] = {
            "start": to_utc_iso(start),
            "end": to_utc_iso(end),
            "description": description,
        }
        if project_id:
            body["projectId"] = project_id
        if billable is not None:
            body["billable"] = billable
        return body

    def add_entry(
        self,
        *,
        project: str,
        day: str,
        hours: float,
        start_time: str = "08:00",
        description: str = "",
        billable: bool | None = None,
    ) -> dict[str, Any]:
        resolved = self.resolve_project(project)
        body = self._entry_body(
            day=parse_date(day),
            start_time=parse_time(start_time),
            hours=hours,
            project_id=resolved.id,
            description=description,
            billable=billable,
        )
        created = self._request("POST", f"/workspaces/{self.workspace_id}/time-entries", json=body)
        return self.describe_entry(created)

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workspaces/{self.workspace_id}/time-entries/{entry_id}")

    def update_entry(
        self,
        entry_id: str,
        *,
        project: str | None = None,
        day: str | None = None,
        hours: float | None = None,
        start_time: str | None = None,
        description: str | None = None,
        billable: bool | None = None,
    ) -> dict[str, Any]:
        """Change the given fields; Clockify needs the full entry, so read it first."""
        current = self.get_entry(entry_id)
        interval = current.get("timeInterval") or {}
        cur_start = parse_iso(interval["start"]).astimezone(self.tz)
        if interval.get("end"):
            cur_hours = (parse_iso(interval["end"]) - parse_iso(interval["start"])).total_seconds() / 3600
        elif hours is None:
            raise ClockifyError("This entry is still running; give hours to set its length.")
        else:
            cur_hours = hours
        project_id = self.resolve_project(project).id if project else current.get("projectId")
        body = self._entry_body(
            day=parse_date(day) if day else cur_start.date(),
            start_time=parse_time(start_time) if start_time else cur_start.time().replace(tzinfo=None),
            hours=hours if hours is not None else cur_hours,
            project_id=project_id,
            description=description if description is not None else (current.get("description") or ""),
            billable=billable if billable is not None else current.get("billable"),
        )
        # Keep fields this server does not manage.
        for key in ("taskId", "tagIds"):
            if current.get(key):
                body[key] = current[key]
        updated = self._request(
            "PUT", f"/workspaces/{self.workspace_id}/time-entries/{entry_id}", json=body
        )
        return self.describe_entry(updated)

    def delete_entry(self, entry_id: str) -> None:
        self._request("DELETE", f"/workspaces/{self.workspace_id}/time-entries/{entry_id}")


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Total hours per project and per day for a list of described entries."""
    per_project: dict[str, float] = {}
    per_day: dict[str, float] = {}
    for entry in entries:
        hours = entry["hours"] or 0.0
        per_project[entry["project"]] = per_project.get(entry["project"], 0.0) + hours
        per_day[entry["date"]] = per_day.get(entry["date"], 0.0) + hours
    total = sum(per_project.values())
    return {
        "total_hours": round(total, 2),
        "per_project": [
            {
                "project": name,
                "hours": round(hours, 2),
                "share_percent": round(100 * hours / total, 1) if total else 0.0,
            }
            for name, hours in sorted(per_project.items(), key=lambda kv: -kv[1])
        ],
        "per_day": {day: round(hours, 2) for day, hours in sorted(per_day.items())},
    }
