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
        if path == "/workspaces":
            return httpx.Response(200, json=[
                {"id": WS, "name": "RISE", "featureSubscriptionType": "STANDARD"},
                {"id": "ws2", "name": "Private", "featureSubscriptionType": "FREE_2026"},
            ])
        if path == "/workspaces/ws2/projects":
            return httpx.Response(200, json=[{"id": "q1", "name": "Hobby", "archived": False}])
        if path == f"/workspaces/{WS}/projects" and request.method == "POST":
            client_name = {"c1": "Energimyndigheten"}.get(body.get("clientId"), "")
            return httpx.Response(
                201, json={"id": "new1", "name": body["name"], "clientName": client_name, "archived": False}
            )
        if path == f"/workspaces/{WS}/projects":
            return httpx.Response(200, json=PROJECTS)
        if path == f"/workspaces/{WS}/clients":
            wanted = request.url.params.get("name", "").casefold()
            clients = [{"id": "c1", "name": "Energimyndigheten"}, {"id": "c2", "name": "EU"}]
            return httpx.Response(200, json=[c for c in clients if wanted in c["name"].casefold()])
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
        config_file=tmp_path / "config.json",
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


def test_create_project_without_client(client, fake):
    project = client.create_project("P128953 BARON")
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", f"/workspaces/{WS}/projects")
    assert body == {"name": "P128953 BARON"}
    assert project.id == "new1"
    # The new project is added to the cache, so it resolves without another request.
    before = client.requests.count()
    assert client.resolve_project("P128953").id == "new1"
    assert client.requests.count() == before


def test_create_project_with_client(client, fake):
    project = client.create_project("P129999 Test", client="energimyndigheten")
    assert fake.calls[-1][2] == {"name": "P129999 Test", "clientId": "c1"}
    assert project.client == "Energimyndigheten"


def test_create_project_unknown_client_makes_no_post(client, fake):
    with pytest.raises(ClockifyError, match="Expected one client"):
        client.create_project("P129999 Test", client="Nobody")
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_create_project_refuses_duplicate(client, fake):
    with pytest.raises(ClockifyError, match="already exists \\(archived"):
        client.create_project("baltic old")
    assert not [c for c in fake.calls if c[0] == "POST"]


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


def make_client(fake, tmp_path):
    return ClockifyClient(
        "test-key",
        api_url="https://api.clockify.me/api/v1",
        usage_file=tmp_path / "requests.json",
        config_file=tmp_path / "config.json",
        transport=httpx.MockTransport(fake),
    )


def test_limit_before_subscription_is_known(client):
    assert client.subscription is None
    assert client.hourly_limit == 30


def test_list_workspaces_saves_subscriptions(client, fake, tmp_path):
    items = client.workspaces()
    assert [w["name"] for w in items] == ["RISE", "Private"]
    assert items[0]["subscription"] == "STANDARD"
    client.workspaces()
    assert [c[1] for c in fake.calls].count("/workspaces") == 1
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["workspaces"][WS] == {"name": "RISE", "subscription": "STANDARD"}


def test_limit_follows_subscription(client):
    client.workspaces()
    client.select_workspace("RISE")
    assert client.subscription == "STANDARD" and client.hourly_limit is None
    client.select_workspace("Private")
    assert client.subscription == "FREE_2026" and client.hourly_limit == 30


def test_select_workspace_switches_projects_and_persists(client, fake, tmp_path, monkeypatch):
    monkeypatch.delenv("CLOCKIFY_WORKSPACE_ID", raising=False)
    assert client.resolve_project("p1").name == "TRANS4M-R"
    chosen = client.select_workspace("priv")
    assert chosen["id"] == "ws2" and chosen["hourly_limit"] == 30
    assert client.resolve_project("Hobby").id == "q1"
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["workspace"] == "ws2"
    # A new client (a restart) knows the workspace and its limit without asking Clockify.
    again = make_client(fake, tmp_path)
    calls = len(fake.calls)
    assert again.workspace_id == "ws2"
    assert again.subscription == "FREE_2026" and again.hourly_limit == 30
    assert len(fake.calls) == calls


def test_select_unknown_workspace(client):
    with pytest.raises(ClockifyError, match="matches no workspaces"):
        client.select_workspace("nope")
