from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AgentBoxToolCategory = Literal["edit", "read", "exec", "net", "other"]
AgentBoxTaskDetail = Literal["topic", "category"]

_SDK_LOG_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class AgentBoxClientConfig:
    plane_url: str
    token: str
    org_id: str
    person_id: str
    machine_id: str
    agent_id: str
    runtime_version: str
    queue_dir: Path
    task_detail: AgentBoxTaskDetail


class AgentBoxClient:
    """Best-effort metadata client for a separately deployed AgentBox plane."""

    def __init__(
        self,
        token: str | None = None,
        plane_url: str | None = None,
        org_id: str | None = None,
        person_id: str | None = None,
        machine_id: str | None = None,
        agent_id: str | None = None,
        runtime_version: str = "alysis",
        queue_dir: str | Path | None = None,
        task_detail: AgentBoxTaskDetail | None = None,
        heartbeat_interval_s: float = 60.0,
        start_background: bool = True,
    ) -> None:
        home = Path(os.environ.get("AGENTBOX_HOME", Path.home() / ".agentbox")).expanduser()
        resolved_machine_id = machine_id or os.environ.get("AGENTBOX_MACHINE_ID") or "machine_local"
        self.config = AgentBoxClientConfig(
            plane_url=(
                plane_url or os.environ.get("AGENTBOX_PLANE_URL") or "http://localhost:3000"
            ),
            token=token or os.environ.get("AGENTBOX_TOKEN", ""),
            org_id=org_id or os.environ.get("AGENTBOX_ORG_ID", "org_local"),
            person_id=person_id or os.environ.get("AGENTBOX_PERSON_ID", "person_local"),
            machine_id=resolved_machine_id,
            agent_id=(
                agent_id or os.environ.get("AGENTBOX_AGENT_ID") or f"{resolved_machine_id}_alysis"
            ),
            runtime_version=(str(runtime_version).strip() or "alysis")[:80],
            queue_dir=Path(
                queue_dir or os.environ.get("AGENTBOX_QUEUE_DIR", home / "sdk-queue")
            ).expanduser(),
            task_detail=(
                "category"
                if (task_detail or os.environ.get("AGENTBOX_TASK_DETAIL")) == "category"
                else "topic"
            ),
        )
        self._sequence = 0
        self._session_id: str | None = None
        self._turn_tokens: dict[str, int | float] = {}
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_interval_s = heartbeat_interval_s
        self._ship_signal: queue.Queue[None] = queue.Queue()
        self._event_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self.config.queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._shipper: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        if start_background:
            self._shipper = threading.Thread(
                target=self._ship_loop,
                name="alysis-agentbox-shipper",
                daemon=True,
            )
            self._shipper.start()

    def session(self, workspace: str | None = None) -> AgentBoxSession:
        return AgentBoxSession(self, workspace)

    def close(self) -> None:
        self._stop.set()
        self._ship_signal.put(None)
        self._stop_heartbeat()
        if self._shipper is not None:
            self._shipper.join(timeout=2)

    def _event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._event_lock:
            self._sequence += 1
            sequence = self._sequence
            session_id = self._session_id or str(uuid.uuid4())
        return {
            "event_id": str(uuid.uuid4()),
            "schema_version": "1",
            "org_id": self.config.org_id,
            "person_id": self.config.person_id,
            "machine_id": self.config.machine_id,
            "agent_id": self.config.agent_id,
            "runtime": "alysis",
            "runtime_version": self.config.runtime_version,
            "session_id": session_id,
            "seq": sequence,
            "occurred_at": _now(),
            "type": event_type,
            "payload": payload,
        }

    def _enqueue(self, event: dict[str, Any]) -> None:
        try:
            path = self.config.queue_dir / "events.ndjson"
            envelope = json.dumps(
                {"queued_at": _now(), "event": event},
                separators=(",", ":"),
            )
            with self._queue_lock:
                _append_private_text(path, envelope + "\n")
            self._ship_signal.put(None)
        except Exception as exc:
            _log_client_exception("enqueue", exc)

    def _ship_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._ship_signal.get(timeout=5)
            except queue.Empty:
                pass
            self._ship_once()

    def _ship_once(self) -> None:
        if not self.config.token:
            return
        path = self.config.queue_dir / "events.ndjson"
        if not path.exists():
            return
        try:
            with self._queue_lock:
                lines = _queue_lines(path)
            batch = _event_batch(lines, limit=500)
            if not batch:
                return

            request = urllib.request.Request(
                self.config.plane_url.rstrip("/") + "/ingest/events",
                data=json.dumps({"events": batch}).encode("utf-8"),
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {self.config.token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            shipped = _accepted_event_ids(body)
            if not shipped:
                return

            with self._queue_lock:
                remaining = [
                    line for line in _queue_lines(path) if _queued_event_id(line) not in shipped
                ]
                _replace_queue(path, remaining)
        except Exception as exc:
            _log_client_exception("ship", exc)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set() and not self._heartbeat_stop.wait(self._heartbeat_interval_s):
            if self._session_id:
                self._enqueue(self._event("heartbeat", {}))

    def _start_heartbeat(self) -> None:
        if self._heartbeat is not None and self._heartbeat.is_alive():
            return
        self._heartbeat_stop.clear()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name="alysis-agentbox-heartbeat",
            daemon=True,
        )
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=2)


class AgentBoxSession:
    def __init__(self, client: AgentBoxClient, workspace: str | None) -> None:
        self.client = client
        self.workspace = _basename(workspace) if workspace else None

    def __enter__(self) -> AgentBoxSession:
        with self.client._event_lock:
            self.client._session_id = str(uuid.uuid4())
        session_payload: dict[str, str] = {}
        if self.workspace is not None:
            session_payload = {
                "workspace_label": self.workspace,
                "git_repo_name": self.workspace,
            }
        self.client._enqueue(self.client._event("session.start", session_payload))
        self.client._enqueue(self.client._event("heartbeat", {}))
        self.client._start_heartbeat()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        reason = "error" if exc_type is not None else "completed"
        self.client._stop_heartbeat()
        self.client._enqueue(self.client._event("session.end", {"end_reason": reason}))
        with self.client._event_lock:
            self.client._session_id = None

    def task(self, hint: str) -> None:
        self.client._enqueue(
            self.client._event(
                "task.update",
                {
                    "task_hint": _task_hint(hint, self.client.config.task_detail),
                    "detail_mode": self.client.config.task_detail,
                },
            )
        )

    @contextmanager
    def turn(self) -> Iterator[None]:
        turn_id = str(uuid.uuid4())
        self.client._turn_tokens = {}
        started = time.monotonic()
        self.client._enqueue(self.client._event("turn.start", {"turn_id": turn_id}))
        try:
            yield
        finally:
            payload: dict[str, Any] = {
                "turn_id": turn_id,
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
            }
            payload.update(self.client._turn_tokens)
            self.client._enqueue(self.client._event("turn.end", payload))

    def tool(
        self,
        name: str,
        category: AgentBoxToolCategory = "other",
        count: int = 1,
    ) -> None:
        self.client._enqueue(
            self.client._event(
                "tool.activity",
                {
                    "tool_name": _safe_name(name),
                    "category": category,
                    "count": min(10_000, max(1, int(count))),
                },
            )
        )

    def tokens(self, in_: int = 0, out: int = 0, usd: float | None = None) -> None:
        totals: dict[str, int | float] = {
            "input_tokens": max(0, int(in_)),
            "output_tokens": max(0, int(out)),
        }
        if usd is not None:
            totals["usd"] = max(0.0, float(usd))
        self.client._turn_tokens = totals


def _event_batch(lines: list[str], *, limit: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)["event"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


def _accepted_event_ids(body: Any) -> set[str]:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return set()
    return {
        event_id
        for item in body["results"]
        if isinstance(item, dict)
        and item.get("status") in {"accepted", "duplicate"}
        and isinstance((event_id := item.get("event_id")), str)
    }


def _queued_event_id(line: str) -> str | None:
    try:
        event_id = json.loads(line)["event"]["event_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return event_id if isinstance(event_id, str) else None


def _queue_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_private_text(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(text)


def _replace_queue(path: Path, lines: list[str]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(("\n".join(lines) + "\n") if lines else "")
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _now() -> str:
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}Z"
    )


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    return Path(value.replace("\\", "/")).name[:80]


def _safe_name(value: str) -> str:
    return re.sub(r"[^\w:.-]", "_", str(value or "unknown"))[:80] or "unknown"


def _task_hint(value: str, detail: AgentBoxTaskDetail) -> str:
    if detail == "category":
        return "editing code"
    text = re.sub(r"```[\s\S]*?```", " ", str(value or ""))
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(
        r"(?:~|\.{1,2}|/)?(?:[\w.-]+/)+[\w.-]+",
        lambda match: Path(match.group(0)).name,
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "working")[:140]


def _log_client_exception(where: str, exc: BaseException) -> None:
    try:
        home = Path(os.environ.get("AGENTBOX_HOME", Path.home() / ".agentbox")).expanduser()
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = home / "alysis-agentbox.log"
        _rotate_log(path)
        line = f"{_now()} {where}: {type(exc).__name__}: {_scrub_log_text(str(exc))}\n"
        _append_private_text(path, line)
    except Exception:
        pass


def _rotate_log(path: Path) -> None:
    try:
        max_bytes = int(os.environ.get("AGENTBOX_SDK_LOG_MAX_BYTES", str(_SDK_LOG_MAX_BYTES)))
        if path.exists() and path.stat().st_size > max_bytes:
            data = path.read_bytes()
            path.write_bytes(data[-max(max_bytes // 2, 1) :])
            path.chmod(0o600)
    except Exception:
        pass


def _scrub_log_text(value: str) -> str:
    text = re.sub(r"https?://[^\s'\"<>]+", "[redacted-url]", value)
    text = re.sub(r"file://[^\s'\"<>]+", "[redacted-path]", text)
    text = re.sub(
        r"(?:~|\.{1,2}|/)(?:[^\s'\"<>:]+/)+[^\s'\"<>:]+",
        "[redacted-path]",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()[:240] or "client error"
