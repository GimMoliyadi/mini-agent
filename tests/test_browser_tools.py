"""Browser tool wiring tests that do not require Chromium binaries."""

import json
from types import SimpleNamespace

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
        assert set(tools) == {"browser_action", "browser_screenshot", "browser_diagnostics"}
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


def test_browser_collects_js_console_and_http_diagnostics_without_secrets(tmp_path):
    session = BrowserSession(tmp_path)

    class FakePage:
        url = "http://127.0.0.1:5173/home?session=secret"

        def __init__(self):
            self.handlers = {}

        def on(self, event, callback):
            self.handlers[event] = callback

    try:
        page = FakePage()
        session.page = page
        session._attach_diagnostics(page)
        page.handlers["console"](SimpleNamespace(type="error", text="ReferenceError: missing"))
        page.handlers["console"](SimpleNamespace(type="log", text="ignored"))
        page.handlers["pageerror"]("TypeError: undefined")
        request = SimpleNamespace(
            url="http://127.0.0.1:5173/api/users?token=never_echo",
            method="GET", failure="net::ERR_CONNECTION_REFUSED",
        )
        page.handlers["requestfailed"](request)
        page.handlers["response"](SimpleNamespace(
            status=404, url=request.url, request=request,
        ))
        page.handlers["response"](SimpleNamespace(
            status=200, url="http://127.0.0.1:5173/ok", request=request,
        ))
        info = session.diagnostic_snapshot()
        assert info["events_seen"] == 4
        assert [item["kind"] for item in info["events"]] == [
            "console_error", "javascript_exception", "request_failed", "http_error",
        ]
        assert info["events"][-1]["status"] == 404
        encoded = json.dumps(info)
        assert "token=" not in encoded
        assert "session=" not in encoded
        assert info["events"][-1]["url"] == "http://127.0.0.1:5173/api/users"
        assert session.diagnostic_snapshot(reset=True)["events_seen"] == 4
        assert session.diagnostic_snapshot()["events_seen"] == 0
    finally:
        session.page = None
        session.shutdown()


def test_registered_browser_diagnostics_tool_clears_events_on_request(tmp_path):
    session = BrowserSession(tmp_path)
    try:
        session._record("javascript_exception", "Unexpected token", "http://localhost:3000/app")
        tools = {tool.name: tool for tool in build_browser_tools(session)}
        data = json.loads(tools["browser_diagnostics"].invoke({"reset": True}))
        assert data["events_seen"] == 1
        assert data["events"][0]["kind"] == "javascript_exception"
        assert session.diagnostic_snapshot()["events_seen"] == 0
        assert tools["browser_diagnostics"].metadata["risk"] == "read"
    finally:
        session.shutdown()
