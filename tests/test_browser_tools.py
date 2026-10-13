"""Browser tool wiring tests that do not require Chromium binaries."""

import json

import pytest

from mini_agent.browser_tools import BrowserSession, build_browser_tools


def test_browser_rejects_nonlocal_urls_before_starting_browser(tmp_path):
    session = BrowserSession(tmp_path)
    try:
        with pytest.raises(ValueError, match="本机"):
            session.open("https://example.com")
        with pytest.raises(ValueError, match="端口"):
            session.open("http://127.0.0.1")
        assert session.page is None
    finally:
        session.shutdown()


def test_browser_tools_register_action_and_screenshot_metadata(tmp_path, monkeypatch):
    session = BrowserSession(tmp_path)
    try:
        monkeypatch.setattr(session, "invoke", lambda action, url, selector, value: {
            "action": action, "url": url, "selector": selector,
        })
        monkeypatch.setattr(session, "capture", lambda output_path: {
            "output": output_path, "bytes": 123,
        })
        tools = {tool.name: tool for tool in build_browser_tools(session)}
        assert set(tools) == {"browser_action", "browser_screenshot"}
        info = json.loads(tools["browser_action"].invoke({
            "action": "open", "url": "http://127.0.0.1:5173",
        }))
        assert info["url"] == "http://127.0.0.1:5173"
        assert tools["browser_screenshot"].metadata["mutation_paths"] == ("output_path",)
    finally:
        session.shutdown()


def test_browser_requires_open_before_inspection(tmp_path):
    session = BrowserSession(tmp_path)
    try:
        with pytest.raises(ValueError, match="尚未打开"):
            session.invoke("inspect", "", "body", "")
    finally:
        session.shutdown()
