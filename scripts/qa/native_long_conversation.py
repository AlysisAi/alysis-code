"""Exercise long conversations and repeated bounded resume in the frozen CLI.

Uses only a synthetic loopback provider and disposable files. This is component
evidence, not live-provider or signed-release evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from native_provider_recovery import Bridge, FaultHandler, _smoke_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--turns", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 40 <= args.turns <= 500:
        parser.error("--turns must be between 40 and 500")
    executable = args.executable.resolve(strict=True)
    with executable.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FaultHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="anl-") as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = workspace / "human.txt"
            marker.write_bytes(b"Human content must remain unchanged.\n")
            env = _smoke_environment(root)
            env["ALYSIS_API_KEY"] = "qa-synthetic-credential"
            if os.name == "nt":
                env["PATH"] = str(Path(os.environ["SYSTEMROOT"]) / "System32")
            config_dir = Path(env["ALYSIS_CONFIG_DIR"])
            config_dir.mkdir()
            base_url = f"http://127.0.0.1:{server.server_port}/v1"
            config = {
                "active_profile": "qa",
                "base_url": base_url,
                "model": "qa-ok",
                "profiles": {
                    "qa": {
                        "name": "qa",
                        "protocol": "openai_compat",
                        "base_url": base_url,
                        "api_key_env": "ALYSIS_API_KEY",
                        "default_model": "qa-ok",
                        "extra_headers": {},
                    }
                },
                "routing_mode": "code_only",
                "stream": False,
                "skills_enabled": False,
                "skills_auto_invoke": False,
                "custom_tools_enabled": False,
                "subagents_enabled": False,
                "web_search_mode": "off",
            }
            (config_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            bridge = Bridge(executable, env, workspace)
            try:
                bridge.request("initialize")

                def create() -> str:
                    return bridge.request(
                        "session.create", {"workspace": str(workspace), "mode": "readonly"}
                    )["session_id"]

                session = create()
                for number in range(args.turns):
                    FaultHandler.requests.clear()
                    status = bridge.turn(
                        session,
                        f"Long conversation turn {number}. Reply briefly. Do not use tools.",
                    )
                    assert status["status"] == "completed" and status.get("exit_code") == 0
                    messages = FaultHandler.requests[-1]["messages"]
                    assert sum(m.get("role") == "assistant" for m in messages) == number
                    bridge.events.clear()
                    if (number + 1) % 20 == 0:
                        print(json.dumps({"completed_turns": number + 1}), flush=True)

                replay_counts = []
                for limit in (60, 500, 500):
                    bridge.request("session.cancel", {"session_id": session})
                    retained = session
                    session = create()
                    bridge.events.clear()
                    result = bridge.request(
                        "session.resume",
                        {
                            "session_id": session,
                            "target_session_id": retained,
                            "max_messages": limit,
                            "emit_history": True,
                        },
                    )
                    visible = [
                        event["payload"]
                        for event in bridge.events
                        if event.get("type") == "message_end" and event.get("session_id") == session
                    ]
                    assert len(visible) == 60
                    assert [message["role"] for message in visible] == ["user", "assistant"] * 30
                    assert visible[0]["text"].startswith(
                        f"Long conversation turn {args.turns - 30}."
                    )
                    assert all("<resume_context>" not in message["text"] for message in visible)
                    replay_counts.append(result["history_count"])

                FaultHandler.requests.clear()
                status = bridge.turn(session, "Continue after recovery. Reply briefly; no tools.")
                assert status["status"] == "completed" and status.get("exit_code") == 0
                wire_messages = FaultHandler.requests[-1]["messages"]
                assert any("<resume_context>" in m.get("content", "") for m in wire_messages)
                assert all("_alysis_resume_context" not in m for m in wire_messages)
                assert marker.read_bytes() == b"Human content must remain unchanged.\n"
                report = {
                    "status": "passed",
                    "runtime_sha256": digest,
                    "provider": "synthetic_loopback",
                    "completed_turns": args.turns,
                    "repeated_resumes": 3,
                    "visible_replay_messages_each": 60,
                    "model_replay_counts": replay_counts,
                    "speaker_roles_preserved": True,
                    "recovery_context_hidden_from_ui_and_preserved_for_model": True,
                    "human_file_preserved": True,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
                args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report), flush=True)
            finally:
                bridge.close()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
