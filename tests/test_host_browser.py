from __future__ import annotations

from types import SimpleNamespace

from alysis_code import host_browser


def test_native_browser_uses_python_webbrowser(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: False)
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda url: opened.append(url) or True,
    )

    assert host_browser.open_url("https://example.test/login") is True
    assert opened == ["https://example.test/login"]


def test_wsl_browser_prefers_wslview(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: True)
    monkeypatch.setattr(
        host_browser,
        "_wsl_browser_commands",
        lambda url: (("/usr/bin/wslview", url), ("powershell.exe", url)),
    )
    monkeypatch.setattr(
        host_browser.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(tuple(command)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda _url: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    assert host_browser.open_url("https://example.test/login?a=1&b=2") is True
    assert commands == [("/usr/bin/wslview", "https://example.test/login?a=1&b=2")]


def test_wsl_browser_falls_through_launchers_then_python(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    opened: list[str] = []
    monkeypatch.setattr(host_browser, "is_wsl", lambda: True)
    monkeypatch.setattr(
        host_browser,
        "_wsl_browser_commands",
        lambda url: (("wslview", url), ("powershell.exe", url)),
    )
    monkeypatch.setattr(
        host_browser.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(tuple(command)) or SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(
        host_browser.webbrowser,
        "open",
        lambda url: opened.append(url) or True,
    )

    assert host_browser.open_url("https://example.test/login") is True
    assert commands == [
        ("wslview", "https://example.test/login"),
        ("powershell.exe", "https://example.test/login"),
    ]
    assert opened == ["https://example.test/login"]


def test_wsl_browser_commands_keep_url_as_one_argument(monkeypatch) -> None:
    monkeypatch.setattr(host_browser.shutil, "which", lambda name: f"/bin/{name}")

    url = "https://example.test/login?state=a&next=https%3A%2F%2Fexample.test"
    commands = host_browser._wsl_browser_commands(url)

    assert commands[0] == ("/bin/wslview", url)
    assert commands[1][-1] == url
    assert commands[2] == ("/bin/rundll32.exe", "url.dll,FileProtocolHandler", url)
