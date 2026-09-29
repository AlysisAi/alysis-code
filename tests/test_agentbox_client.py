from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alysis_code.agentbox_client import AgentBoxClient


class _FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _accepted(event_id: str) -> _FakeResponse:
    return _FakeResponse({"results": [{"event_id": event_id, "status": "accepted"}]})


def _queued_events(queue_dir: Path) -> list[dict[str, object]]:
    queue_path = queue_dir / "events.ndjson"
    return [
        json.loads(line)["event"]
        for line in queue_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_client_enqueues_contract_safe_metadata_and_omits_unknown_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    queue_dir = home / "sdk-queue"
    monkeypatch.setenv("AGENTBOX_HOME", str(home))
    client = AgentBoxClient(
        token="",
        org_id="org_a",
        person_id="person_a",
        machine_id="machine_a",
        queue_dir=queue_dir,
        start_background=False,
    )

    with client.session(workspace="/Users/me/private/repo-name") as session:
        session.task("Fix /Users/me/private/repo-name/auth.py and `secret()`")
        with session.turn():
            session.tool("fs_read", category="read")
            session.tokens(in_=3, out=2, usd=None)
    client.close()

    events = _queued_events(queue_dir)
    assert events[0]["payload"] == {
        "workspace_label": "repo-name",
        "git_repo_name": "repo-name",
    }
    task = next(event for event in events if event["type"] == "task.update")
    assert task["payload"]["task_hint"] == "Fix auth.py and"
    assert "secret" not in json.dumps(events)
    turn_end = next(event for event in events if event["type"] == "turn.end")
    assert turn_end["payload"]["input_tokens"] == 3
    assert turn_end["payload"]["output_tokens"] == 2
    assert "usd" not in turn_end["payload"]
    assert all(event["runtime"] == "alysis" for event in events)
    if os.name != "nt":
        mode = stat.S_IMODE((queue_dir / "events.ndjson").stat().st_mode)
        assert mode == 0o600


def test_ship_preserves_event_enqueued_during_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_dir = tmp_path / "queue"
    client = AgentBoxClient(
        token="token",
        plane_url="http://agentbox.local",
        queue_dir=queue_dir,
        start_background=False,
    )
    first = client._event("heartbeat", {})
    client._enqueue(first)
    request_started = threading.Event()
    allow_response = threading.Event()

    def respond(*args: object, **kwargs: object) -> _FakeResponse:
        request_started.set()
        assert allow_response.wait(timeout=2)
        return _accepted(first["event_id"])

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    shipper = threading.Thread(target=client._ship_once)
    shipper.start()
    assert request_started.wait(timeout=2)
    second = client._event("heartbeat", {})
    client._enqueue(second)
    allow_response.set()
    shipper.join(timeout=2)

    assert not shipper.is_alive()
    assert [event["event_id"] for event in _queued_events(queue_dir)] == [second["event_id"]]
    client.close()


def test_offline_queue_replays_and_log_scrubs_sensitive_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    queue_dir = tmp_path / "queue"
    monkeypatch.setenv("AGENTBOX_HOME", str(home))
    client = AgentBoxClient(
        token="token",
        plane_url="https://secret.example.test/private",
        queue_dir=queue_dir,
        start_background=False,
    )
    event = client._event("heartbeat", {})
    client._enqueue(event)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.URLError(
                "POST https://secret.example.test/private failed at /Users/me/repo/file.py"
            )
        ),
    )

    client._ship_once()

    assert _queued_events(queue_dir)
    log = (home / "alysis-agentbox.log").read_text(encoding="utf-8")
    assert "secret.example.test" not in log
    assert "/Users/me/repo" not in log

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _accepted(event["event_id"]),
    )
    client._ship_once()
    assert (queue_dir / "events.ndjson").read_text(encoding="utf-8") == ""
    client.close()


def test_unexpected_plane_response_does_not_drop_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_dir = tmp_path / "queue"
    client = AgentBoxClient(
        token="token",
        plane_url="http://agentbox.local",
        queue_dir=queue_dir,
        start_background=False,
    )
    client._enqueue(client._event("heartbeat", {}))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: _FakeResponse({"unexpected": True}),
    )

    client._ship_once()

    assert len(_queued_events(queue_dir)) == 1
    client.close()


def test_heartbeat_thread_stops_when_session_ends(tmp_path: Path) -> None:
    client = AgentBoxClient(
        token="",
        queue_dir=tmp_path / "queue",
        heartbeat_interval_s=0.01,
    )
    with client.session(workspace="repo"):
        time.sleep(0.03)
        assert client._heartbeat is not None
        assert client._heartbeat.is_alive()

    assert client._heartbeat is not None
    assert not client._heartbeat.is_alive()
    client.close()
