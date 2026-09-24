from __future__ import annotations

import os
import re
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .agentbox_client import AgentBoxClient
from .tools.registry import iter_builtin_tool_metadata
from .usage_tracker import UsageRecord

AgentBoxToolCategory = Literal["edit", "read", "exec", "net", "other"]

_TRUE_VALUES = {"1", "true", "yes", "on"}
_PATH_RE = re.compile(r"(?:~|\.{1,2}|/)?(?:[\w.-]+/)+[\w.-]+")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_WORD_RE = re.compile(r"\s+")
_TOOL_CATEGORY_BY_NAME: dict[str, AgentBoxToolCategory] | None = None


@dataclass(frozen=True)
class _AgentBoxSettings:
    plane_url: str
    token: str
    org_id: str | None
    person_id: str | None
    machine_id: str | None
    agent_id: str | None
    queue_dir: str | None
    task_detail: Literal["topic", "category"]


def _load_agentbox_settings() -> _AgentBoxSettings | None:
    config = _read_agentbox_config()
    runtime = config.get("runtime", {})
    alysis = runtime.get("alysis", {}) if isinstance(runtime, dict) else {}
    if not isinstance(alysis, dict):
        alysis = {}

    plane_url = _env_or_config("AGENTBOX_PLANE_URL", config, "plane_url")
    token = _env_or_config("AGENTBOX_TOKEN", config, "machine_token")
    if plane_url is None or token is None:
        return None

    machine_id = _env_or_config("AGENTBOX_MACHINE_ID", config, "machine_id")
    agent_id = _env_or_config("AGENTBOX_AGENT_ID", alysis, "agent_id")
    if agent_id is None and machine_id is not None:
        agent_id = f"{machine_id}_alysis"

    return _AgentBoxSettings(
        plane_url=plane_url,
        token=token,
        org_id=_env_or_config("AGENTBOX_ORG_ID", config, "org_id"),
        person_id=_env_or_config("AGENTBOX_PERSON_ID", config, "person_id"),
        machine_id=machine_id,
        agent_id=agent_id,
        queue_dir=_sdk_queue_dir(config),
        task_detail=(
            "category"
            if _env_or_config("AGENTBOX_TASK_DETAIL", config, "task_detail") == "category"
            else "topic"
        ),
    )


def _read_agentbox_config() -> dict[str, Any]:
    configured_path = _text(os.environ.get("AGENTBOX_CONFIG"))
    if configured_path is not None:
        path = Path(configured_path).expanduser()
    else:
        home = Path(os.environ.get("AGENTBOX_HOME", Path.home() / ".agentbox"))
        path = home.expanduser() / "config.toml"
    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, tomllib.TOMLDecodeError, TypeError):
        return {}


def _env_or_config(env_name: str, config: dict[str, Any], config_name: str) -> str | None:
    return _text(os.environ.get(env_name)) or _text(config.get(config_name))


def _sdk_queue_dir(config: dict[str, Any]) -> str | None:
    explicit = _text(os.environ.get("AGENTBOX_QUEUE_DIR"))
    if explicit is not None:
        return str(Path(explicit).expanduser())
    connector_queue = _text(config.get("queue_dir"))
    if connector_queue is None:
        return None
    return str(Path(connector_queue).expanduser().parent / "sdk-queue")


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


class AgentBoxTelemetry:
    def __init__(self, *, client: Any, root: Path) -> None:
        self._client = client
        self._root = root
        self._session_cm: Any | None = None
        self._session: Any | None = None
        self._turn_cm: Any | None = None
        self._turn: Any | None = None
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_usd: float | None = None
        self._last_task = ""

    @classmethod
    def from_env(cls, *, root: Path, runtime_version: str) -> AgentBoxTelemetry | None:
        if os.environ.get("AGENTBOX_ENABLED", "").strip().lower() not in _TRUE_VALUES:
            return None
        settings = _load_agentbox_settings()
        if settings is None:
            return None

        try:
            client = AgentBoxClient(
                token=settings.token,
                plane_url=settings.plane_url,
                org_id=settings.org_id,
                person_id=settings.person_id,
                machine_id=settings.machine_id,
                agent_id=settings.agent_id,
                runtime_version=runtime_version,
                queue_dir=settings.queue_dir,
                task_detail=settings.task_detail,
            )
            telemetry = cls(client=client, root=root)
            telemetry.start_session()
            return telemetry
        except Exception:
            return None

    def start_session(self) -> None:
        if self._session is not None:
            return
        try:
            self._session_cm = self._client.session(workspace=self._root.name)
            self._session = self._session_cm.__enter__()
        except Exception:
            self._session_cm = None
            self._session = None

    def close(self, *, error: bool = False) -> None:
        try:
            if self._turn_cm is not None:
                self._turn_cm.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            self._turn_cm = None
            self._turn = None

        try:
            if self._session_cm is not None:
                exc_type = RuntimeError if error else None
                exc = RuntimeError("session ended") if error else None
                self._session_cm.__exit__(exc_type, exc, None)
        except Exception:
            pass
        finally:
            self._session_cm = None
            self._session = None

        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def turn(self) -> Any:
        if self._session is None:
            return nullcontext()
        return _AgentBoxTurnContext(self)

    def task(self, objective: str) -> None:
        if self._session is None:
            return
        hint = sanitize_task_hint(objective)
        if not hint or hint == self._last_task:
            return
        self._last_task = hint
        try:
            self._session.task(hint)
        except Exception:
            pass

    def tool(self, name: str) -> None:
        if self._session is None:
            return
        clean_name = safe_tool_name(name)
        try:
            self._session.tool(clean_name, category=tool_category(clean_name))
        except Exception:
            pass

    def record_usage(self, record: UsageRecord) -> None:
        if self._turn_cm is None or self._session is None:
            return
        self._turn_input_tokens += max(0, int(record.prompt_tokens or 0))
        self._turn_output_tokens += max(0, int(record.completion_tokens or 0))
        if record.cost_usd is not None:
            self._turn_usd = (self._turn_usd or 0.0) + max(0.0, float(record.cost_usd))
        tokens = getattr(self._session, "tokens", None)
        if not callable(tokens):
            tokens = getattr(self._turn, "tokens", None)
        if not callable(tokens):
            return
        try:
            tokens(
                in_=self._turn_input_tokens,
                out=self._turn_output_tokens,
                usd=self._turn_usd,
            )
        except Exception:
            pass


class _AgentBoxTurnContext:
    def __init__(self, telemetry: AgentBoxTelemetry) -> None:
        self._telemetry = telemetry

    def __enter__(self) -> AgentBoxTelemetry:
        self._telemetry._turn_input_tokens = 0
        self._telemetry._turn_output_tokens = 0
        self._telemetry._turn_usd = None
        try:
            self._telemetry._turn_cm = self._telemetry._session.turn()
            self._telemetry._turn = self._telemetry._turn_cm.__enter__()
        except Exception:
            self._telemetry._turn_cm = None
            self._telemetry._turn = None
        return self._telemetry

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        try:
            if self._telemetry._turn_cm is not None:
                self._telemetry._turn_cm.__exit__(exc_type, exc, tb)
        except Exception:
            pass
        finally:
            self._telemetry._turn_cm = None
            self._telemetry._turn = None
        return False


def sanitize_task_hint(value: str) -> str:
    text = str(value or "")
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _PATH_RE.sub(" file ", text)
    text = re.sub(r"\bfile(?:\s+file\b)+", "file", text)
    text = _WORD_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    if not text:
        return "working"
    return text[:140].rstrip()


def safe_tool_name(value: str) -> str:
    return re.sub(r"[^\w:.-]", "_", str(value or "unknown"))[:80] or "unknown"


def tool_category(tool_name: str) -> AgentBoxToolCategory:
    global _TOOL_CATEGORY_BY_NAME
    if _TOOL_CATEGORY_BY_NAME is None:
        _TOOL_CATEGORY_BY_NAME = _build_tool_category_map()
    return _TOOL_CATEGORY_BY_NAME.get(tool_name, _heuristic_tool_category(tool_name))


def _build_tool_category_map() -> dict[str, AgentBoxToolCategory]:
    mapped: dict[str, AgentBoxToolCategory] = {}
    for metadata in iter_builtin_tool_metadata():
        tags = {tag.strip().lower() for tag in metadata.categories}
        if "write" in tags:
            mapped[metadata.name] = "edit"
        elif "shell" in tags or "verify" in tags:
            mapped[metadata.name] = "exec"
        elif "web" in tags:
            mapped[metadata.name] = "net"
        elif "read" in tags or "search" in tags or "history" in tags:
            mapped[metadata.name] = "read"
        else:
            mapped[metadata.name] = "other"
    return mapped


def _heuristic_tool_category(tool_name: str) -> AgentBoxToolCategory:
    normalized = tool_name.strip().lower()
    if any(token in normalized for token in ("write", "edit", "patch", "delete", "move", "mkdir")):
        return "edit"
    if any(token in normalized for token in ("shell", "exec", "run", "verify", "test")):
        return "exec"
    if any(token in normalized for token in ("web", "http", "fetch", "search")):
        return "net"
    if any(token in normalized for token in ("read", "list", "history", "symbol", "grep", "rg")):
        return "read"
    return "other"
