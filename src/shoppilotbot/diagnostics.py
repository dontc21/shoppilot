from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, ConsoleMessage, Page, Request, Response

from .browser import BrowserSafetyStop, normalize_text, safe_url


CLICK_MARKER = "__SHOPPILOTBOT_CUSTOMER_SERVICE_CLICK__"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(token|authorization|cookie|ticket|code|secret|session|access_key)"
    r"\s*[:=]\s*[^\s,;&]+"
)
URL_IN_TEXT = re.compile(r"https?://[^\s\]\[()'\"<>]+")
LONG_VALUE = re.compile(r"\b[A-Za-z0-9_+/=-]{28,}\b")


def diagnostic_url(raw_url: str) -> str:
    """只保留诊断所需域名和路径，并遮盖可能是账号 ID 的路径段。"""
    base = safe_url(raw_url)
    parts = urlsplit(base)
    safe_segments = []
    for segment in parts.path.split("/"):
        if len(segment) >= 20 or (segment.isdigit() and len(segment) >= 7):
            safe_segments.append("<redacted>")
        else:
            safe_segments.append(segment)
    safe_path = "/".join(safe_segments)
    return urlunsplit((parts.scheme, parts.netloc, safe_path, "", parts.fragment))


def sanitize_diagnostic_text(value: str, limit: int = 500) -> str:
    """清理错误文本中的 URL 参数、凭据字段和长令牌。"""
    text = URL_IN_TEXT.sub(lambda match: diagnostic_url(match.group(0)), value)
    text = SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = LONG_VALUE.sub("<redacted>", text)
    return normalize_text(text)[:limit]


def sanitize_diagnostic_value(value: object) -> object:
    """递归清理列表和字典，避免嵌套 DOM 属性绕过脱敏。"""
    if isinstance(value, str):
        return sanitize_diagnostic_text(value)
    if isinstance(value, list):
        return [sanitize_diagnostic_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_diagnostic_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): sanitize_diagnostic_value(item)
            for key, item in value.items()
        }
    return value


class EntryDiagnosticRecorder:
    """记录客服入口点击前后的最小化、脱敏浏览器事件。"""

    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = output_dir / f"customer-service-entry-{stamp}.jsonl"
        self.events: list[dict[str, object]] = []
        self._page_labels: dict[int, str] = {}
        self._attached_pages: set[int] = set()
        self.armed = False
        self.trigger_observed = False

    def _label(self, page: Page) -> str:
        identity = id(page)
        if identity not in self._page_labels:
            self._page_labels[identity] = f"page-{len(self._page_labels)}"
        return self._page_labels[identity]

    def emit(self, event: str, **details: object) -> None:
        safe_details = {
            key: sanitize_diagnostic_value(value) for key, value in details.items()
        }
        record = {
            "time": datetime.now(UTC).isoformat(),
            "phase": "after_click" if self.trigger_observed else "before_click",
            "event": event,
            **safe_details,
        }
        self.events.append(record)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print("[诊断] " + json.dumps(record, ensure_ascii=False), flush=True)

    def attach_context(self, context: BrowserContext) -> None:
        context.on("page", self._on_new_page)
        context.on("requestfailed", self._on_request_failed)
        context.on("response", self._on_response)
        for page in context.pages:
            self.attach_page(page)

    def attach_page(self, page: Page) -> None:
        identity = id(page)
        if identity in self._attached_pages:
            return
        self._attached_pages.add(identity)
        label = self._label(page)
        page.on("popup", lambda popup, opener=label: self._on_popup(opener, popup))
        page.on(
            "framenavigated",
            lambda frame, current=page: self.emit(
                "framenavigated",
                page=self._label(current),
                main_frame=frame == current.main_frame,
                url=diagnostic_url(frame.url),
            ),
        )
        page.on("console", lambda message, current=page: self._on_console(current, message))
        page.on("pageerror", lambda error, current=page: self._on_page_error(current, error))

    def _on_new_page(self, page: Page) -> None:
        label = self._label(page)
        if self.armed:
            self.trigger_observed = True
        self.emit("new_page", page=label, url=diagnostic_url(page.url))
        self.attach_page(page)

    def _on_popup(self, opener: str, popup: Page) -> None:
        if self.armed:
            self.trigger_observed = True
        self.emit(
            "popup",
            opener=opener,
            page=self._label(popup),
            url=diagnostic_url(popup.url),
        )
        self.attach_page(popup)

    def _request_page_label(self, request: Request) -> str:
        try:
            return self._label(request.frame.page)
        except Exception:
            return "service-worker-or-unknown"

    def _on_request_failed(self, request: Request) -> None:
        self.emit(
            "requestfailed",
            page=self._request_page_label(request),
            method=request.method,
            resource_type=request.resource_type,
            url=diagnostic_url(request.url),
            error=request.failure or "unknown",
        )

    def _on_response(self, response: Response) -> None:
        if not self.armed:
            return
        request = response.request
        if request.resource_type not in {"document", "xhr", "fetch"}:
            return
        redirected_from = request.redirected_from
        self.emit(
            "response",
            page=self._request_page_label(request),
            method=request.method,
            resource_type=request.resource_type,
            status=response.status,
            url=diagnostic_url(response.url),
            redirected_from=(
                diagnostic_url(redirected_from.url) if redirected_from else ""
            ),
        )

    def _on_console(self, page: Page, message: ConsoleMessage) -> None:
        if message.text == CLICK_MARKER:
            self.trigger_observed = True
            self.emit("entry_clicked", page=self._label(page))
            return
        if message.type not in {"warning", "error", "assert"}:
            return
        location = message.location or {}
        self.emit(
            "console",
            page=self._label(page),
            level=message.type,
            message=message.text,
            source=diagnostic_url(location.get("url", "")),
            line=location.get("lineNumber", 0),
            column=location.get("columnNumber", 0),
        )

    def _on_page_error(self, page: Page, error: Exception) -> None:
        self.emit(
            "pageerror",
            page=self._label(page),
            error_type=type(error).__name__,
            message=str(error),
        )

    def inspect_entry(self, page: Page) -> int:
        locator = page.get_by_text("客服系统", exact=True)
        count = locator.count()
        self.emit(
            "entry_locator",
            page=self._label(page),
            match_count=count,
            page_url=diagnostic_url(page.url),
        )
        for index in range(count):
            info = locator.nth(index).evaluate(
                """
                (el) => {
                  const target = el.closest('a,button,[role="button"]') || el;
                  return {
                    tag: target.tagName.toLowerCase(),
                    id: (target.id || '').slice(0, 120),
                    classes: Array.from(target.classList || []).slice(0, 12),
                    role: target.getAttribute('role') || '',
                    target: target.getAttribute('target') || '',
                    href: target.href || target.getAttribute('href') || '',
                    has_onclick: target.hasAttribute('onclick') || typeof target.onclick === 'function'
                  };
                }
                """
            )
            href = diagnostic_url(str(info.pop("href", "")))
            self.emit("entry_dom", page=self._label(page), index=index, href=href, **info)
        return count

    def arm_click_observer(self, page: Page) -> None:
        page.evaluate(
            """
            (marker) => {
              if (window.__shopPilotEntryObserverInstalled) return;
              window.__shopPilotEntryObserverInstalled = true;
              document.addEventListener('click', (event) => {
                const origin = event.target instanceof Element ? event.target : null;
                const target = origin && (origin.closest('a,button,[role="button"]') || origin);
                const text = target && (target.innerText || target.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text === '客服系统') console.info(marker);
              }, true);
            }
            """,
            CLICK_MARKER,
        )
        self.armed = True
        self.emit("listeners_armed", page=self._label(page))

    def snapshot_pages(self, context: BrowserContext) -> None:
        for page in context.pages:
            if page.is_closed():
                continue
            self.attach_page(page)
            try:
                title_state = "empty" if not page.title().strip() else "present"
            except Exception as exc:
                title_state = f"error:{type(exc).__name__}"
            self.emit(
                "page_snapshot",
                page=self._label(page),
                url=diagnostic_url(page.url),
                title_state=title_state,
                frame_count=len(page.frames),
            )

    def summary(self) -> dict[str, object]:
        after_click = [item for item in self.events if item["phase"] == "after_click"]
        return {
            "event_count": len(self.events),
            "trigger_observed": self.trigger_observed,
            "new_pages": sum(item["event"] == "new_page" for item in after_click),
            "navigations": sum(item["event"] == "framenavigated" for item in after_click),
            "console_errors": sum(
                item["event"] == "console" and item.get("level") == "error"
                for item in after_click
            ),
            "page_errors": sum(item["event"] == "pageerror" for item in after_click),
            "failed_requests": sum(
                item["event"] == "requestfailed" for item in after_click
            ),
            "http_errors": sum(
                item["event"] == "response" and int(item.get("status", 0)) >= 400
                for item in after_click
            ),
            "log_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }


def find_entry_page(context: BrowserContext) -> tuple[Page | None, int]:
    matches: list[tuple[Page, int]] = []
    for page in context.pages:
        if page.is_closed():
            continue
        count = page.get_by_text("客服系统", exact=True).count()
        if count:
            matches.append((page, count))
    if len(matches) > 1:
        raise BrowserSafetyStop("多个标签页包含“客服系统”，无法安全确定入口页。")
    return matches[0] if matches else (None, 0)
