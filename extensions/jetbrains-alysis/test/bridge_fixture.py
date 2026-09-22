"""Real Alysis protocol/lifecycle with a deterministic agent; no network/model calls."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.surface.types import ApprovalRequest


class Agent:
    def __init__(self, surface):
        self.surface = surface
        self.store = SimpleNamespace(
            session_artifact_root=Path(os.environ["ALYSIS_DATA_DIR"]) / "artifacts"
        )
        self.cfg = AppConfig(model="integration-fixture", subagents_enabled=False)

    def run_turn(self, message, cancellation_token=None):
        if message == "cancel":
            while True:
                if cancellation_token:
                    cancellation_token.throw_if_cancelled()
                time.sleep(0.02)
        self.surface.emit_message_delta("Reviewing ")
        decision = self.surface.request_approval(
            ApprovalRequest(
                kind="write_file",
                reason="Review the proposed test change",
                preview="+ Example",
                files=["README.md"],
            )
        )
        response = "Approved test change" if decision.allow else "Denied test change"
        self.surface.emit_message_end(response)
        return 0

    def close(self):
        pass


stdio_bridge.load_config = lambda: AppConfig(model="integration-fixture", subagents_enabled=False)
bridge = stdio_bridge.StdioBridge(create_session_fn=lambda **kwargs: Agent(kwargs["surface"]))
raise SystemExit(bridge.run())
