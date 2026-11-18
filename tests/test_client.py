"""Tests against a fake Clockify API (no network, no real key)."""

from __future__ import annotations

import json

import httpx
import pytest

from clockify_mcp.client import ClockifyClient, ClockifyError, summarize

WS = "ws1"
USER = {
    "id": "u1",
    "name": "Test User",
    "activeWorkspace": WS,
    "settings": {"timeZone": "Europe/Stockholm"},
}
PROJECTS = [
    {"id": "p1", "name": "TRANS4M-R", "archived": False},
    {"id": "p2", "name": "Baltic-FIT", "archived": False},
    {"id": "p3", "name": "Baltic old", "archived": True},
    {"id": "p4", "name": "Baltic Sea", "archived": False},
]


class FakeClockify:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.entries: dict[str, dict] = {}
        self.status_override: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path, body))
        assert request.headers["X-Api-Key"] == "test-key"
        if self.status_override:
            return httpx.Response(self.status_override, json={"message": "nope"})
        if path == "/user":
            return httpx.Response(200, json=USER)
        if path == f"/workspaces/{WS}/projects":
            return httpx.Response(200, json=PROJECTS)
        if path == f"/workspaces/{WS}/user/u1/time-entries":
            self.last_params = dict(request.url.params)
            return httpx.Response(200, json=list(self.entries.values()))
        if path == f"/workspaces/{WS}/time-entries" and request.method == "POST":
            entry = self._store(f"e{len(self.entries) + 1}", body)
            return httpx.Response(201, json=entry)
        if path.startswith(f"/workspaces/{WS}/time-entries/"):
            entry_id = path.rsplit("/", 1)[1]
            if request.method == "GET":
                return httpx.Response(200, json=self.entries[entry_id])
            if request.method == "PUT":
                return httpx.Response(200, json=self._store(entry_id, body))
            if request.method == "DELETE":
                del self.entries[entry_id]
                return httpx.Response(204)
        return httpx.Response(404, json={"message": "not found"})

    def _store(self, entry_id: str, body: dict) -> dict:
        entry = {
            "id": entry_id,
            "description": body.get("description", ""),
            "projectId": body.get("projectId"),
            "billable": body.get("billable", False),
            "tagIds": body.get("tagIds"),
            "timeInterval": {"start": body["start"], "end": body["end"]},
        }
        self.entries[entry_id] = entry
        return entry


@pytest.fixture
def fake():
    return FakeClockify()


@pytest.fixture
def client(fake, tmp_path):
    return ClockifyClient(
        "test-key",
        api_url="https://api.clockify.me/api/v1",
        usage_file=tmp_path / "requests.json",
        transport=httpx.MockTransport(fake),
    )


def test_add_entry_converts_local_time_to_utc(client, fake):
    created = client.add_entry(project="TRANS4M-R", day="2026-11-02", hours=7.5, description="work")
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", f"/workspaces/{WS}/time-entries")
    # Stockholm is UTC+1 in November.
    assert body["start"] == "2026-11-02T07:00:00Z"
    assert body["end"] == "2026-11-02T14:30:00Z"
    assert body["projectId"] == "p1"
    assert "billable" not in body
    assert created["hours"] == 7.5
    assert created["start"] == "2026-11-02 08:00"
    assert created["project"] == "TRANS4M-R"


def test_summer_time_offset(client, fake):
    client.add_entry(project="p1", day="2026-07-01", hours=1, start_time="09:00")
    assert fake.calls[-1][2]["start"] == "2026-07-01T07:00:00Z"


def test_user_and_projects_are_cached(client, fake):
    client.add_entry(project="p1", day="2026-11-02", hours=1)
    client.add_entry(project="p2", day="2026-11-03", hours=1)
    paths = [c[1] for c in fake.calls]
    assert paths.count("/user") == 1
    assert paths.count(f"/workspaces/{WS}/projects") == 1
    assert client.requests.count() == 4


def test_resolve_project(client):
    assert client.resolve_project("p2").name == "Baltic-FIT"
    assert client.resolve_project("baltic-fit").id == "p2"
    assert client.resolve_project("trans4m").id == "p1"
    with pytest.raises(ClockifyError, match="several projects"):
        client.resolve_project("Baltic")
    with pytest.raises(ClockifyError, match="No active project"):
        client.resolve_project("Baltic old")


def test_time_entries_range_and_summary(client, fake):
    client.add_entry(project="p1", day="2026-11-02", hours=6)
    client.add_entry(project="p2", day="2026-11-02", hours=2, start_time="14:00")
    client.add_entry(project="p1", day="2026-11-03", hours=4)
    entries = client.time_entries("2026-11-01", "2026-11-30")
    # Inclusive local range: 1 Nov 00:00 to 1 Dec 00:00 Stockholm time.
    assert fake.last_params["start"] == "2026-10-31T23:00:00Z"
    assert fake.last_params["end"] == "2026-11-30T23:00:00Z"
    summary = summarize(entries)
    assert summary["total_hours"] == 12
    assert summary["per_project"][0] == {"project": "TRANS4M-R", "hours": 10, "share_percent": 83.3}
    assert summary["per_day"] == {"2026-11-02": 8, "2026-11-03": 4}


def test_update_keeps_unchanged_fields(client, fake):
    created = client.add_entry(project="p1", day="2026-11-02", hours=6, description="keep me")
    fake.entries[created["id"]]["tagIds"] = ["t1"]
    updated = client.update_entry(created["id"], hours=4)
    body = fake.calls[-1][2]
    assert fake.calls[-1][0] == "PUT"
    assert body["start"] == "2026-11-02T07:00:00Z"
    assert body["end"] == "2026-11-02T11:00:00Z"
    assert body["description"] == "keep me"
    assert body["projectId"] == "p1"
    assert body["tagIds"] == ["t1"]
    assert updated["hours"] == 4


def test_update_moves_project_and_date(client, fake):
    created = client.add_entry(project="p1", day="2026-11-02", hours=3)
    updated = client.update_entry(created["id"], project="Baltic-FIT", day="2026-11-05")
    assert updated["project"] == "Baltic-FIT"
    assert updated["date"] == "2026-11-05"
    assert updated["hours"] == 3


def test_delete(client, fake):
    created = client.add_entry(project="p1", day="2026-11-02", hours=3)
    client.delete_entry(created["id"])
    assert fake.entries == {}


@pytest.mark.parametrize("hours", [0, -1, 25])
def test_rejects_bad_hours(client, hours):
    with pytest.raises(ClockifyError, match="hours"):
        client.add_entry(project="p1", day="2026-11-02", hours=hours)


def test_rejects_bad_date(client):
    with pytest.raises(ClockifyError, match="Invalid date"):
        client.add_entry(project="p1", day="2 Nov", hours=1)


def test_rate_limit_message(client, fake):
    fake.status_override = 429
    with pytest.raises(ClockifyError, match="rate limit"):
        client.user()


def test_error_message_does_not_leak_key(client, fake):
    fake.status_override = 500
    with pytest.raises(ClockifyError) as info:
        client.user()
    assert "test-key" not in str(info.value)


def test_request_log_persists(client, tmp_path):
    client.user()
    from clockify_mcp.client import RequestLog

    assert RequestLog(tmp_path / "requests.json").count() == 1
