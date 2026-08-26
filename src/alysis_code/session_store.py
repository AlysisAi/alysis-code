from __future__ import annotations

import getpass
import json
import os
import re
import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, default_sessions_dir
from .logging_redaction import redact_log_text
from .session_artifacts import SessionArtifactLayout
from .web_research import SessionWebResearchTracker


def _now_ts() -> str:
    return datetime.now(UTC).isoformat()


def make_session_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # 8 chars is enough for local uniqueness.
    import uuid

    return f"{ts}_{uuid.uuid4().hex[:8]}"


def sanitize_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id.strip()) or "session"


def resolve_sessions_dir(cfg: AppConfig) -> Path:
    if cfg.session_log_dir:
        return Path(cfg.session_log_dir)
    return default_sessions_dir()


def local_session_owner() -> str | None:
    """Deterministic identity (``os-user@hostname``) of the local account.

    Session logs are stamped with their creator's identity so listing surfaces
    (``/resume``, "latest session" defaults) can hide conversations that were
    not created by this account — regardless of how a foreign log file arrived
    in the sessions directory (copied archive, baked disk image, shared host
    with distinct accounts). Purely local and deterministic: no network, no
    model/provider involvement. Returns ``None`` when neither component can be
    resolved; callers must treat that as "no local identity established".
    """
    try:
        user = getpass.getuser().strip()
    except (KeyError, OSError, ImportError):
        # No passwd entry / no LOGNAME-USER-USERNAME env; on py<=3.12 the
        # env-less fallback is a bare ``import pwd`` which raises
        # ModuleNotFoundError on Windows-family hosts (3.13+ maps it to
        # OSError) — this function must degrade to None, never crash startup.
        user = ""
    try:
        # Use the direct socket API instead of ``platform.node()``. On Windows,
        # ``platform.node()`` can invoke ``ver`` through a shell, which makes a
        # harmless identity lookup observable as an unrelated subprocess and
        # lets hook subprocess instrumentation intercept it.
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    if not user and not host:
        return None
    return f"{user}@{host}"


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    mtime: float
    # Log-derived scoping/recency metadata (all optional so any existing
    # ``SessionInfo(session_id=..., path=..., mtime=...)`` construction keeps
    # working). ``workspace_root``/``git_root`` come from the session log's own
    # events (not the filesystem) and let ``/resume`` show only the current
    # workspace's chats. ``last_event_ts`` is the log's own UTC ISO timestamp of
    # the most recent event, used for the displayed "when" instead of file mtime
    # (which drifts after copies/extracts).
    # ``owner`` is the log-recorded creator identity (see
    # :func:`local_session_owner`); listings hide sessions stamped by a
    # different account so one user's conversations never surface for another.
    # ``last_owner`` is the identity stamped on the log's newest event — the
    # account that most recently used the session. Matching either lets an
    # explicit ``/resume <id>`` re-adopt a session whose recorded identity
    # drifted (e.g. after a hostname rename): the resume appends fresh events
    # with the current identity, so the session self-heals into the listing.
    workspace_root: str | None = None
    git_root: str | None = None
    last_event_ts: str | None = None
    owner: str | None = None
    last_owner: str | None = None


class SessionStore:
    def __init__(
        self,
        *,
        enabled: bool,
        artifact_persistence_enabled: bool | None = None,
        sessions_dir: Path,
        session_id: str,
        cwd: str,
        repo_root: str | None,
        workspace_root: str | None = None,
        focus_dir: str | None = None,
        git_root: str | None = None,
        workspace_kind: str | None = None,
        binding_source: str | None = None,
        binding_requested_path: str | None = None,
        binding_risk_level: str | None = None,
        binding_created_path: bool | None = None,
        runtime_kind: str | None = None,
        active_workdir: str | None = None,
        active_workdir_relpath: str | None = None,
        owner: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self.cwd = cwd
        self.repo_root = repo_root
        self.workspace_root = workspace_root
        # Stamp every log with its creator so listings can scope per account.
        self.owner = owner if owner is not None else local_session_owner()
        self.focus_dir = focus_dir
        self.git_root = git_root
        self.workspace_kind = workspace_kind
        self.binding_source = binding_source
        self.binding_requested_path = binding_requested_path
        self.binding_risk_level = binding_risk_level
        self.binding_created_path = binding_created_path
        self.runtime_kind = runtime_kind
        self.active_workdir = active_workdir or cwd
        self.active_workdir_relpath = active_workdir_relpath
        self.path = sessions_dir / f"{session_id}.jsonl"
        self._lock = threading.RLock()
        self.artifact_persistence_enabled = (
            enabled if artifact_persistence_enabled is None else bool(artifact_persistence_enabled)
        )
        self._events: list[dict[str, Any]] = []
        self._web_research = SessionWebResearchTracker()
        self._hydrate_existing_state()

        self._fh = None
        if self.enabled:
            try:
                self.sessions_dir.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
            except OSError:
                self.enabled = False
                self.artifact_persistence_enabled = False

    @property
    def session_artifact_root(self) -> Path:
        return self.sessions_dir / sanitize_session_id(self.session_id)

    @property
    def session_artifact_layout(self) -> SessionArtifactLayout:
        return SessionArtifactLayout(filesystem_root=self.session_artifact_root)

    def runtime_artifact_path(self, *parts: str) -> Path:
        return self.session_artifact_layout.artifact_fs_path(*parts)

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        observation_payload: dict[str, Any] | None = None,
    ) -> None:
        event = self._build_event(event_type=event_type, payload=payload)
        with self._lock:
            event["event_id"] = f"{self.session_id}:{len(self._events) + 1}"
            self._events.append(event)
            observed_web_research = self._web_research.observe_event(
                event_type=event_type,
                payload=observation_payload if observation_payload is not None else payload,
                ts=str(event.get("ts") or "").strip() or None,
                event_id=str(event.get("event_id") or "").strip() or None,
            )
            if self.enabled and self._fh:
                self._fh.write(redact_log_text(json.dumps(event, ensure_ascii=True) + "\n"))
                self._fh.flush()
            if observed_web_research and self.artifact_persistence_enabled:
                self._persist_web_research_artifact()

    def _build_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "type": event_type,
            "ts": _now_ts(),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "pid": os.getpid(),
            "payload": payload,
        }
        if self.workspace_root is not None:
            event["workspace_root"] = self.workspace_root
        if self.owner is not None:
            event["owner"] = self.owner
        if self.focus_dir is not None:
            event["focus_dir"] = self.focus_dir
        if self.git_root is not None:
            event["git_root"] = self.git_root
        if self.workspace_kind is not None:
            event["workspace_kind"] = self.workspace_kind
        if self.binding_source is not None:
            event["binding_source"] = self.binding_source
        if self.binding_requested_path is not None:
            event["binding_requested_path"] = self.binding_requested_path
        if self.binding_risk_level is not None:
            event["binding_risk_level"] = self.binding_risk_level
        if self.binding_created_path is not None:
            event["binding_created_path"] = self.binding_created_path
        if self.runtime_kind is not None:
            event["runtime_kind"] = self.runtime_kind
        if self.active_workdir is not None:
            event["active_workdir"] = self.active_workdir
        if self.active_workdir_relpath is not None:
            event["active_workdir_relpath"] = self.active_workdir_relpath
        return event

    def update_active_workdir(self, *, cwd: str, active_workdir_relpath: str) -> None:
        self.cwd = cwd
        self.active_workdir = cwd
        self.active_workdir_relpath = active_workdir_relpath

    def append_artifact_jsonl(self, *parts: str, payload: dict[str, Any]) -> Path | None:
        if not self.enabled:
            return None
        path = self.runtime_artifact_path(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(redact_log_text(json.dumps(payload, ensure_ascii=True) + "\n"))
        return path

    def events_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._events, ensure_ascii=True))

    def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
        """Return events appended at or after ``cursor`` and the next cursor.

        Events are append-only and their positional order is protected by the
        store lock. A slice is therefore enough for incremental consumers; in
        particular, this avoids the full JSON round-trip used by
        :meth:`events_snapshot`.
        """
        with self._lock:
            start = max(0, int(cursor))
            next_cursor = len(self._events)
            return self._events[start:], next_cursor

    def classify_web_fetch_url(self, raw_url: Any) -> str | None:
        return self._web_research.classify_fetch_url(raw_url)

    def resolve_web_fetch_url(self, raw_url: Any) -> tuple[str | None, str | None]:
        return self._web_research.resolve_fetch_url(raw_url)

    def fetchable_web_fetch_urls(self, *, limit: int = 10) -> list[str]:
        return self._web_research.fetchable_urls(limit=limit)

    def establish_search_mediated_web_fetch_url(
        self,
        *,
        raw_url: Any,
        query: str,
        source_url: str | None = None,
    ) -> tuple[bool, str | None]:
        with self._lock:
            changed, normalized = self._web_research.establish_search_mediated_fetch_url(
                raw_url=raw_url,
                query=query,
                source_url=source_url,
            )
            if changed and self.artifact_persistence_enabled:
                self._persist_web_research_artifact()
            return changed, normalized

    def web_research_artifact_payload(self) -> dict[str, Any]:
        return self._web_research.artifact_payload()

    def web_research_metrics_payload(self) -> dict[str, int]:
        return self._web_research.metrics_payload()

    def has_web_research_activity(self) -> bool:
        return self._web_research.has_activity()

    def _persist_web_research_artifact(self) -> Path | None:
        if not self.artifact_persistence_enabled or not self._web_research.has_activity():
            return None
        path = self.runtime_artifact_path("web_research_sources.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            redact_log_text(
                json.dumps(
                    self._web_research.artifact_payload(),
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=True,
                )
                + "\n"
            ),
            encoding="utf-8",
        )
        return path

    def _hydrate_existing_state(self) -> None:
        self._hydrate_from_existing_log()
        self._hydrate_from_existing_web_research_artifact()
        self._web_research.clear_pending()

    def _hydrate_from_existing_log(self) -> bool:
        try:
            if not self.path.exists():
                return False
            events = list(read_session_events(self.path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not events:
            return False
        self._events = json.loads(json.dumps(events, ensure_ascii=True))
        for index, event in enumerate(self._events, start=1):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            self._web_research.observe_event(
                event_type=str(event.get("type") or "").strip(),
                payload=payload,
                ts=str(event.get("ts") or "").strip() or None,
                event_id=str(event.get("event_id") or "").strip() or f"{self.session_id}:{index}",
            )
        return True

    def _hydrate_from_existing_web_research_artifact(self) -> bool:
        artifact_path = self.runtime_artifact_path("web_research_sources.json")
        try:
            if not artifact_path.exists():
                return False
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return self._web_research.merge_from_artifact_payload(payload)

    def close(self) -> None:
        if self.artifact_persistence_enabled and self._web_research.has_activity():
            self._persist_web_research_artifact()
        if self._fh:
            self._fh.close()
            self._fh = None


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def canonical_workspace_path(raw: str | Path | None) -> Path | None:
    """Canonicalize a workspace/git-root path for identity comparison.

    Uses ``Path.resolve()`` so trailing slashes, ``.``/``..`` segments, and the
    OS's own case rules are absorbed (case-insensitive on Windows via the
    resolved canonical casing, case-sensitive on POSIX) without any per-platform
    special-casing. Returns ``None`` for empty/unresolvable input. This is
    deterministic and provider/model-agnostic by construction.
    """
    text = _nonempty_str(raw if isinstance(raw, str) else (str(raw) if raw is not None else None))
    if text is None:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def session_belongs_to_workspace(
    info: SessionInfo,
    current_workspace_root: Path | None,
    current_git_root: Path | None = None,
) -> bool:
    """Return True if ``info`` (a session log) belongs to the current workspace.

    Primary identity is the log's recorded ``workspace_root``. When that is
    absent (a legacy log predating workspace_root recording) we fall back to
    ``git_root``. When neither yields a deterministic match we return False:
    such strays are hidden from the scoped picker but remain reachable by an
    explicit ``/resume <id>``. ``current_workspace_root``/``current_git_root``
    must already be canonicalized via :func:`canonical_workspace_path`.
    """
    if info.workspace_root is not None:
        if current_workspace_root is None:
            return False
        session_ws = canonical_workspace_path(info.workspace_root)
        return session_ws is not None and session_ws == current_workspace_root
    if info.git_root is not None and current_git_root is not None:
        session_git = canonical_workspace_path(info.git_root)
        return session_git is not None and session_git == current_git_root
    return False


def session_belongs_to_owner(info: SessionInfo, current_owner: str | None) -> bool:
    """Return True if ``info`` (a session log) may be listed for this account.

    A log stamped with an owner is only listed when the current identity
    matches either the creator stamp (``owner``, from the log's first events)
    or the newest event's stamp (``last_owner``) — compared case-insensitively,
    since Windows usernames and DNS hostnames are not case-significant. The
    ``last_owner`` leg is the self-heal path: when a user's identity drifts
    (hostname rename, WSL vs native), one explicit ``/resume <id>`` appends
    fresh events under the new identity and the session lists again. A foreign
    log stays hidden either way — both its stamps are foreign.

    Legacy logs with no recorded owner on either end stay visible so an
    upgrade never empties a user's own list (workspace scoping still applies
    to them). When the log records an owner but the local identity cannot be
    established, the log is hidden: a foreign-stamped conversation must never
    surface on an unidentifiable account. Hidden logs remain reachable by an
    explicit ``/resume <id>`` on the machine that holds them.
    """
    recorded_first = _nonempty_str(info.owner)
    recorded_last = _nonempty_str(info.last_owner)
    if recorded_first is None and recorded_last is None:
        return True
    current = _nonempty_str(current_owner)
    if current is None:
        return False
    current_folded = current.casefold()
    if recorded_first is not None and recorded_first.casefold() == current_folded:
        return True
    return recorded_last is not None and recorded_last.casefold() == current_folded


def filter_sessions_to_local_owner(infos: list[SessionInfo]) -> list[SessionInfo]:
    """Drop sessions stamped by a different account than the local one."""
    current_owner = local_session_owner()
    return [info for info in infos if session_belongs_to_owner(info, current_owner)]


def read_session_first_event_workspace(path: Path) -> tuple[str | None, str | None]:
    """Read the session log's recorded (workspace_root, git_root)."""
    workspace_root, git_root, _owner = read_session_first_event_scope(path)
    return workspace_root, git_root


def read_session_first_event_scope(path: Path) -> tuple[str | None, str | None, str | None]:
    """Read the log's recorded (workspace_root, git_root, owner).

    Every event carries these at top level (see :meth:`SessionStore._build_event`)
    and the ``session_start`` payload also records the workspace fields; we scan
    the first few events and return the first non-empty values found. Bounded so
    we never read a whole (possibly large) log. Any read/parse error yields
    ``(None, None, None)``.
    """
    workspace_root: str | None = None
    git_root: str | None = None
    owner: str | None = None
    events = read_session_events(path)
    try:
        checked = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            checked += 1
            if workspace_root is None:
                workspace_root = _nonempty_str(event.get("workspace_root"))
            if git_root is None:
                git_root = _nonempty_str(event.get("git_root"))
            if owner is None:
                owner = _nonempty_str(event.get("owner"))
            payload = event.get("payload")
            if isinstance(payload, dict):
                if workspace_root is None:
                    workspace_root = _nonempty_str(payload.get("workspace_root"))
                if git_root is None:
                    git_root = _nonempty_str(payload.get("git_root"))
                if owner is None:
                    owner = _nonempty_str(payload.get("owner"))
            # Keep scanning (within the bound) until the owner stamp is found
            # too: a pre-upgrade log resumed post-upgrade carries its owner
            # only on later events, and classifying it as legacy would leave
            # it visible to every account.
            if workspace_root is not None and git_root is not None and owner is not None:
                break
            if checked >= 5:
                break
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None
    finally:
        close = getattr(events, "close", None)
        if callable(close):
            close()
    return workspace_root, git_root, owner


def read_session_last_event_ts(path: Path) -> str | None:
    """Return the UTC ISO ``ts`` of the log's most recent event."""
    ts, _owner = read_session_last_event_fields(path)
    return ts


def read_session_last_event_fields(path: Path) -> tuple[str | None, str | None]:
    """Return the (``ts``, ``owner``) recorded at the tail of the log.

    Tail-scans the last chunk of the file (events are append-only and flushed,
    so the final parseable line is the newest event) rather than parsing the
    whole log. ``ts`` comes from the newest event that carries one; ``owner``
    comes from the newest parseable event — the account that most recently
    used the session. Returns ``(None, None)`` on empty/corrupt/unreadable
    logs so callers fall back to file mtime / creator-stamp semantics.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size <= 0:
                return None, None
            read_size = min(file_size, 16384)
            fh.seek(file_size - read_size)
            data = fh.read()
    except OSError:
        return None, None
    owner: str | None = None
    seen_event = False
    for raw_line in reversed(data.split(b"\n")):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            if not seen_event:
                owner = _nonempty_str(obj.get("owner"))
                seen_event = True
            ts = _nonempty_str(obj.get("ts"))
            if ts is not None:
                return ts, owner
    return None, owner


def _epoch_from_iso(raw: str | None) -> float | None:
    text = _nonempty_str(raw)
    if text is None:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _session_recency_sort_key(info: SessionInfo) -> float:
    # Prefer the log's own last-event timestamp (immune to copy/extract mtime
    # resets); fall back to filesystem mtime. Both are absolute epoch seconds,
    # so they order correctly even when mixed.
    epoch = _epoch_from_iso(info.last_event_ts)
    if epoch is not None:
        return epoch
    try:
        return float(info.mtime)
    except (TypeError, ValueError):
        return 0.0


def list_sessions(sessions_dir: Path) -> list[SessionInfo]:
    if not sessions_dir.exists():
        return []
    out: list[SessionInfo] = []
    for p in sessions_dir.glob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        try:
            workspace_root, git_root, owner = read_session_first_event_scope(p)
        except OSError:
            workspace_root, git_root, owner = None, None, None
        try:
            last_event_ts, last_owner = read_session_last_event_fields(p)
        except OSError:
            last_event_ts, last_owner = None, None
        out.append(
            SessionInfo(
                session_id=p.stem,
                path=p,
                mtime=st.st_mtime,
                workspace_root=workspace_root,
                git_root=git_root,
                last_event_ts=last_event_ts,
                owner=owner,
                last_owner=last_owner,
            )
        )
    out.sort(key=_session_recency_sort_key, reverse=True)
    return out


def read_session_events(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
