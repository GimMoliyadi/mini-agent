"""Reusable local Playwright browser session for visual coding workflows.

Browser starts only on first open(), and is closed with the owning
AgentApplication. Playwright is an optional installed dependency.
"""

from __future__ import annotations

import json
import contextvars
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from langchain.tools import tool

from . import sandbox
from . import dev_environment as dev

_MAX_TEXT = 3500
_MAX_BROWSER_EVENTS = 100
_MAX_EVENT_MESSAGE = 550


class BrowserSession:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        # Playwright's sync API is thread-affine; all browser calls use one worker.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mini-agent-browser")
        self.playwright: Any = None
        self.browser: Any = None
        self.page: Any = None
        self.events: deque[dict[str, Any]] = deque(maxlen=_MAX_BROWSER_EVENTS)
        self.events_seen = 0

    def invoke(self, action: str, url: str, selector: str, value: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            if action == "open":
                return self.open(url)
            if action == "click":
                return self.click(selector)
            if action == "fill":
                return self.fill(selector, value)
            if action == "inspect":
                return self.inspect(selector)
            self.close()
            return {"closed": True}

        ctx = contextvars.copy_context()
        return self.executor.submit(ctx.run, run).result(timeout=40)

    def capture(self, output_path: str) -> dict[str, Any]:
        ctx = contextvars.copy_context()
        return self.executor.submit(ctx.run, self.screenshot, output_path).result(timeout=30)

    def diagnostic_snapshot(self, reset: bool = False) -> dict[str, Any]:
        ctx = contextvars.copy_context()
        return self.executor.submit(ctx.run, self.diagnostics, reset).result(timeout=10)

    @staticmethod
    def _safe_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            if not parsed.hostname:
                return ""
            host = parsed.hostname
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{host}{port}{parsed.path}"
        except (ValueError, TypeError):
            return ""

    def _record(self, kind: str, message: str, url: str = "",
                method: str = "", status: int | None = None) -> None:
        event: dict[str, Any] = {
            "kind": kind,
            "message": sandbox.redact_text(str(message)[:_MAX_EVENT_MESSAGE]),
            "url": self._safe_url(url),
        }
        if method:
            event["method"] = method
        if status is not None:
            event["status"] = status
        self.events.append(event)
        self.events_seen += 1

    def _attach_diagnostics(self, page: Any) -> None:
        def on_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                self._record("console_" + message.type, message.text, page.url)

        def on_page_error(exc: Any) -> None:
            self._record("javascript_exception", str(exc), page.url)

        def on_request_failed(request: Any) -> None:
            self._record(
                "request_failed", str(request.failure or "network request failed"),
                request.url, request.method,
            )

        def on_response(response: Any) -> None:
            if response.status >= 400:
                self._record(
                    "http_error", f"HTTP {response.status}",
                    response.url, response.request.method, response.status,
                )

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)
        page.on("response", on_response)

    def diagnostics(self, reset: bool = False) -> dict[str, Any]:
        rows = list(self.events)
        result = {
            "url": self._safe_url(self.page.url) if self.page is not None else "",
            "events_seen": self.events_seen,
            "dropped_older_events": max(0, self.events_seen - len(rows)),
            "events": rows,
            "notice": "Only console errors/warnings, JS exceptions, failed requests and HTTP >=400 are captured; no request bodies or headers.",
        }
        if reset:
            self.events.clear()
            self.events_seen = 0
        return result

    def shutdown(self) -> None:
        try:
            self.executor.submit(self.close).result(timeout=15)
        finally:
            self.executor.shutdown(wait=True, cancel_futures=True)

    def close(self) -> None:
        page, browser, playwright = self.page, self.browser, self.playwright
        self.page, self.browser, self.playwright = None, None, None
        try:
            if page is not None:
                page.close()
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                if playwright is not None:
                    playwright.stop()

    def open(self, url: str) -> dict[str, Any]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1", "localhost", "::1",
        }:
            raise ValueError("浏览器测试仅支持本机 HTTP 服务的 localhost / 127.0.0.1 / ::1 地址")
        if not isinstance(parsed.port, int) or parsed.port <= 0:
            raise ValueError("请提供包含本机端口的完整 URL，例如 http://127.0.0.1:5173")
        if self.page is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise ValueError(
                    "缺少 Playwright；先安装 mini-agent[browser] 并运行 playwright install chromium"
                ) from exc
            try:
                self.playwright = sync_playwright().start()
                self.browser = self.playwright.chromium.launch(headless=True)
                self.page = self.browser.new_page(viewport={"width": 1280, "height": 800})
                self.events.clear()
                self.events_seen = 0
                self._attach_diagnostics(self.page)
            except Exception as exc:
                self.close()
                raise ValueError(f"浏览器启动失败：{type(exc).__name__}: {exc}") from exc
        self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return self.inspect("body")

    def _page(self) -> Any:
        if self.page is None:
            raise ValueError("浏览器尚未打开；先调用 browser_action(action='open', url=...)")
        return self.page

    def inspect(self, selector: str = "body") -> dict[str, Any]:
        page = self._page()
        if len(selector) > 256:
            raise ValueError("CSS selector 过长")
        locator = page.locator(selector).first
        text = locator.inner_text(timeout=5000)
        result = {
            "url": page.url, "title": page.title(), "selector": selector,
            "text": text[:_MAX_TEXT], "truncated": len(text) > _MAX_TEXT,
        }
        value = locator.evaluate(
            "el => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) "
            "&& !['password', 'hidden', 'file'].includes(el.type) ? el.value : null",
            timeout=5000,
        )
        if value is not None:
            result["value"] = sandbox.redact_text(value[:_MAX_TEXT])
            result["truncated"] = result["truncated"] or len(value) > _MAX_TEXT
        return result

    def click(self, selector: str) -> dict[str, Any]:
        page = self._page()
        page.locator(selector).first.click(timeout=5000)
        return self.inspect("body")

    def fill(self, selector: str, value: str) -> dict[str, Any]:
        page = self._page()
        if len(value) > 2000:
            raise ValueError("输入内容过长")
        locator = page.locator(selector).first
        locator.fill(value, timeout=5000)
        return {"selector": selector, "value_length": len(value), "url": page.url}

    def screenshot(self, output_path: str) -> dict[str, Any]:
        page = self._page()
        target = sandbox.resolve_path(output_path, self.workspace)
        if target.suffix.lower() != ".png":
            raise ValueError("浏览器截图输出必须为工作区内的 .png 文件")
        binary: bytes = page.screenshot(type="png", full_page=False, timeout=10000)
        if len(binary) > sandbox.MAX_FILE_BYTES:
            raise ValueError("截图超过单文件大小上限")
        with sandbox.file_change({target: binary}, self.workspace):
            sandbox.atomic_write_bytes(target, binary)
        return {"output": target.relative_to(self.workspace).as_posix(), "bytes": len(binary)}


def build_browser_tools(session: BrowserSession) -> list[Any]:
    @tool
    def browser_action(
        action: Literal["open", "click", "fill", "inspect", "close"],
        url: str = "",
        selector: str = "body",
        value: str = "",
    ) -> str:
        """Open a local dev webpage, click buttons, fill forms or inspect live DOM.

        Starts a local Chromium on first open. The browser session persists for
        this AgentApplication lifetime only, including multiple tool calls.
        Browser must be installed as optional dependency and Chromium provisioned.
        Inspect includes ordinary form values, excluding password, hidden and file inputs.
        """
        try:
            result = session.invoke(action, url, selector, value)
            return sandbox.redact_text(json.dumps(result, ensure_ascii=False))
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise ValueError(f"浏览器操作失败：{type(exc).__name__}: {exc}") from exc

    browser_action.metadata = {"risk": "execute", "mutation": False}

    @tool(response_format="content_and_artifact")
    def browser_screenshot(output_path: str = "screenshots/browser.png") -> tuple[str, dict[str, Any]]:
        """Capture a PNG screenshot of the currently open local development page.

        Stores the screenshot in the current workspace for later inspection.
        Requires approval when the Agent runs in ASK mode.
        """
        try:
            result = session.capture(output_path)
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise ValueError(f"浏览器截图失败：{type(exc).__name__}: {exc}") from exc
        return dev._outcome(dev.ToolResult(
            sandbox.redact_text(json.dumps(result, ensure_ascii=False)),
            "success", "browser_screenshot_saved", result,
        ))

    @tool
    def browser_diagnostics(reset: bool = False) -> str:
        """Return browser console errors, JS exceptions and failed/4xx/5xx requests.

        Open a local page with browser_action first. Returns bounded recent
        event metadata, URL paths without query strings, and no request bodies
        or headers. reset=True clears the current in-memory diagnostic list.
        """
        try:
            result = session.diagnostic_snapshot(reset)
            # Keep the tool reply bounded even when many errors were observed.
            while result["events"] and len(json.dumps(result, ensure_ascii=False)) > 7200:
                result["events"].pop(0)
                result["dropped_older_events"] += 1
            return sandbox.redact_text(json.dumps(result, ensure_ascii=False))
        except (OSError, ValueError):
            raise
        except Exception as exc:
            raise ValueError(f"浏览器诊断信息读取失败：{type(exc).__name__}: {exc}") from exc

    browser_diagnostics.metadata = {"risk": "read", "mutation": False}

    browser_screenshot.metadata = {
        "risk": "write", "mutation": True, "mutation_paths": ("output_path",),
        "workspace_arguments": ("output_path",),
    }
    return [browser_action, browser_screenshot, browser_diagnostics]
