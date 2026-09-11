from __future__ import annotations

import asyncio
import threading

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.branding import PROJECT_SOURCE_URL
from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.tui.tips import TIP_INTERVAL_SECONDS, WORKING_TIPS, working_tip


def test_working_tips_rotate_without_changing_on_every_repaint():
    first = working_tip(0)
    assert working_tip(TIP_INTERVAL_SECONDS - 0.1) == first
    assert working_tip(TIP_INTERVAL_SECONDS) != first
    assert working_tip(TIP_INTERVAL_SECONDS * len(WORKING_TIPS)) == first
    assert working_tip(0, turn_index=1) != first


@pytest.mark.parametrize("columns,outcome", [(120, "complete"), (40, "cancel"), (80, "fail")])
def test_working_tips_follow_live_ui_without_entering_history(monkeypatch, columns, outcome):
    release = threading.Event()
    browser_release = threading.Event()
    finished = threading.Event()
    opened_urls: list[str] = []
    screens: dict[str, str] = {}
    errors: list[Exception] = []
    clock = {"offset": 0.0}
    rendered = {"text": ""}

    class SizedOutput(DummyOutput):
        def get_size(self):
            return Size(rows=30, columns=columns)

    class Session:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            try:
                self.surface.on_user_message(text)
                while not release.wait(0.01):
                    cancellation_token.throw_if_cancelled()
                if outcome == "fail":
                    raise RuntimeError("Simulated turn failure")
                self.surface.on_assistant_message_done("Done")
                return 0
            finally:
                finished.set()

    def capture_screen(application):
        screen = application.renderer.last_rendered_screen
        if screen is not None:
            rendered["text"] = "\n".join(
                "".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
                for y in range(30)
            )

    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)

    async def exercise(application, pipe):
        try:
            await wait_for(lambda: bool(rendered["text"]))
            screens["idle"] = rendered["text"]
            pipe.send_text("Inspect the project\r")
            await wait_for(lambda: "Tip: " in rendered["text"])
            screens["working"] = rendered["text"]
            clock["offset"] = TIP_INTERVAL_SECONDS
            await wait_for(lambda: "Tip: Use /permissions" in rendered["text"])
            screens["rotated"] = rendered["text"]

            link_index = next(
                i for i, tip in enumerate(WORKING_TIPS) if tip.url == PROJECT_SOURCE_URL
            )
            clock["offset"] = TIP_INTERVAL_SECONDS * link_index
            await wait_for(lambda: "Tip: Star us on GitHub!" in rendered["text"])
            screens["linked"] = rendered["text"]
            assert opened_urls == []
            input_buffer = application.layout.current_buffer
            row, line = next(
                (row, line)
                for row, line in enumerate(rendered["text"].splitlines())
                if "Star us on GitHub!" in line
            )
            x, y = line.index("Star") + 1, row + 1
            # A right click must not launch a browser. Use real terminal mouse
            # events to cover hit testing, including the narrow clipped layout.
            pipe.send_text(f"\x1b[<2;{x};{y}M\x1b[<2;{x};{y}m")
            await asyncio.sleep(0.05)
            assert opened_urls == []

            # Releasing on a link is not a click unless the press began there.
            pipe.send_text(f"\x1b[<0;{x};{y}m")
            await asyncio.sleep(0.05)
            assert opened_urls == []
            pipe.send_text("draft message")
            await wait_for(lambda: "draft message" in rendered["text"])
            input_row, input_line = next(
                (row, line)
                for row, line in enumerate(rendered["text"].splitlines())
                if "draft message" in line
            )
            input_x, input_y = input_line.index("draft message") + 1, input_row + 1
            pipe.send_text(f"\x1b[<0;{input_x};{input_y}M")
            await asyncio.sleep(0.05)
            pipe.send_text(f"\x1b[<32;{x};{y}M\x1b[<0;{x};{y}m")
            await asyncio.sleep(0.05)
            assert opened_urls == []

            # Capture must also cancel a drag that leaves the link and returns
            # to the original cell, or a release outside followed by a stray up.
            for finish_inside in (True, False):
                pipe.send_text(f"\x1b[<0;{x};{y}M")
                await asyncio.sleep(0.05)
                if finish_inside:
                    pipe.send_text(f"\x1b[<32;{input_x};{input_y}M")
                    await asyncio.sleep(0.05)
                else:
                    pipe.send_text(f"\x1b[<0;{input_x};{input_y}m")
                    await asyncio.sleep(0.05)
                pipe.send_text(f"\x1b[<0;{x};{y}m")
                await asyncio.sleep(0.05)
                assert opened_urls == []

            # A tip rotating away during the press cancels its pending click.
            pipe.send_text(f"\x1b[<0;{x};{y}M")
            await asyncio.sleep(0.05)
            clock["offset"] += TIP_INTERVAL_SECONDS
            await wait_for(lambda: "Tip: Use /trace" in rendered["text"])
            pipe.send_text(f"\x1b[<0;{x};{y}m")
            await asyncio.sleep(0.05)
            assert opened_urls == []
            clock["offset"] = TIP_INTERVAL_SECONDS * link_index
            await wait_for(lambda: "Tip: Star us on GitHub!" in rendered["text"])
            input_buffer.reset()

            # Press/release in one input batch exercises the pre-repaint path.
            pipe.send_text(f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m")
            await wait_for(lambda: len(opened_urls) == 1)
            assert opened_urls == [PROJECT_SOURCE_URL]
            assert application.layout.current_buffer is input_buffer

            # The browser opener remains blocked while the UI handles /help.
            pipe.send_text("/help\r")
            await wait_for(
                lambda: "Commands" in rendered["text"] and "Tip: " not in rendered["text"]
            )
            screens["dialog"] = rendered["text"]
            browser_release.set()
            pipe.send_text("\x1b")
            await wait_for(lambda: "Tip: " in rendered["text"])
            row, line = next(
                (row, line)
                for row, line in enumerate(rendered["text"].splitlines())
                if "Star us on GitHub!" in line
            )
            x, y = line.index("https://") + 1, row + 1
            # Holding through a repaint exercises the capture overlay path.
            pipe.send_text(f"\x1b[<0;{x};{y}M")
            await asyncio.sleep(0.05)
            pipe.send_text(f"\x1b[<0;{x};{y}m")
            await wait_for(lambda: len(opened_urls) == 2)
            assert opened_urls == [PROJECT_SOURCE_URL, PROJECT_SOURCE_URL]
            if outcome == "cancel":
                pipe.send_text("\x1b")
            else:
                release.set()
            await wait_for(lambda: finished.is_set() and "Tip: " not in rendered["text"])
            screens["finished"] = rendered["text"]
        except Exception as exc:
            errors.append(exc)
        finally:
            release.set()
            browser_release.set()
            application.exit()

    def capture_application(*args, **kwargs):
        application = Application(*args, **kwargs, after_render=capture_screen)
        application.pre_run_callables.append(
            lambda: application.create_background_task(exercise(application, pipe))
        )
        return application

    def open_browser(url):
        opened_urls.append(url)
        browser_release.wait(3)
        return True

    monkeypatch.setattr(app_module, "Application", capture_application)
    monkeypatch.setattr(app_module, "open_url", open_browser)
    monkeypatch.setattr(
        app_module,
        "working_tip",
        lambda elapsed_seconds, *, turn_index: working_tip(
            elapsed_seconds + clock["offset"], turn_index=turn_index
        ),
    )
    with create_pipe_input() as pipe:
        _, transcript = app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=SizedOutput(),
            session_builder=Session,
        )

    assert not errors, (errors, rendered["text"])
    assert "Tip: " not in screens["idle"]
    assert "Tip: Use /trace" in screens["working"]
    assert "Tip: Use /permissions" in screens["rotated"]
    assert "Tip: " not in screens["dialog"]
    assert "Tip: " not in screens["finished"]
    for phase in ("working", "rotated", "linked"):
        assert sum("Tip: " in row for row in screens[phase].splitlines()) == 1
        assert "context:" in screens[phase]
    assert all("Tip: " not in text for _, text in transcript)
