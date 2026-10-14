from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import BrowserContext, Frame, Page, Playwright

from .config import Settings


class BrowserSafetyStop(RuntimeError):
    """页面状态不再可信时停止监听。"""


class LoginRequired(BrowserSafetyStop):
    """登录状态失效时暂停处理，但保留浏览器供店主重新登录。"""


@dataclass(frozen=True)
class IncomingMessage:
    conversation: str
    conversation_key: str
    message_id: str
    text: str
    time_text: str
    is_self: bool
    fallback_position: str = ""

    @property
    def key(self) -> str:
        return message_key(
            self.conversation_key,
            self.message_id,
            self.text,
            self.time_text,
            self.is_self,
            self.fallback_position,
        )


def normalize_text(value: str) -> str:
    """压缩页面中的多余空白，便于显示和生成稳定指纹。"""
    return re.sub(r"\s+", " ", value).strip()


def message_key(
    conversation_key: str,
    message_id: str,
    text: str,
    time_text: str,
    is_self: bool,
    fallback_position: str = "",
) -> str:
    """平台 ID 优先；没有 ID 时对多项页面证据生成本地 SHA-256。"""
    identity = (
        f"id:{conversation_key}:{message_id}"
        if message_id
        else (
            f"fallback:{conversation_key}:{normalize_text(text)}:{time_text}:"
            f"{is_self}:{fallback_position}"
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class ProcessedMessageStore:
    """SQLite 仅保存哈希和时间，不持久化买家姓名或消息正文。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_key TEXT PRIMARY KEY,
                conversation_key TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def add_if_new(self, message: IncomingMessage) -> bool:
        before = self.connection.total_changes
        self.connection.execute(
            """
            INSERT OR IGNORE INTO processed_messages
                (message_key, conversation_key, first_seen_at)
            VALUES (?, ?, ?)
            """,
            (message.key, message.conversation_key, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return self.connection.total_changes > before

    def close(self) -> None:
        self.connection.close()


def launch_browser(playwright: Playwright, settings: Settings) -> BrowserContext:
    """使用独立持久化目录；登录完全由店主在可见窗口中完成。"""
    settings.profile_dir.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=settings.profile_dir,
        channel=settings.browser_channel,
        headless=False,
        accept_downloads=False,
        viewport={"width": 1440, "height": 900},
    )


def safe_url(raw_url: str) -> str:
    """保留无参数的 SPA 路由，但剔除查询参数、Token 类片段。"""
    parts = urlsplit(raw_url)
    fragment_path = parts.fragment.split("?", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_./-]*", fragment_path):
        fragment_path = ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", fragment_path))


def _dom_skeleton(frame: Frame) -> list[dict[str, object]]:
    # 故意不采集正文、input value、Cookie、href、aria-label 等潜在隐私字段。
    return frame.evaluate(
        r"""
        () => {
          const safeToken = (value, limit = 80) => {
            const text = (value || '').slice(0, limit);
            return /^[A-Za-z_-][A-Za-z0-9_-]*$/.test(text) && !/\d{4}/.test(text)
              ? text : '';
          };
          return Array.from(document.querySelectorAll('body *')).slice(0, 12000).map((el) => ({
            tag: el.tagName.toLowerCase(),
            id: safeToken(el.id, 120),
            has_id: Boolean(el.id),
            classes: Array.from(el.classList || []).map((item) => safeToken(item)).filter(Boolean).slice(0, 12),
            role: safeToken(el.getAttribute('role') || ''),
            name: safeToken(el.getAttribute('name') || ''),
            type: safeToken(el.getAttribute('type') || '', 40),
            testid: safeToken(el.getAttribute('data-testid') || '', 120),
            data_attributes: Array.from(el.attributes || [])
              .map((attribute) => attribute.name)
              .filter((name) => name.startsWith('data-'))
              .slice(0, 16),
            children: el.children.length
          }));
        }
        """
    )


def inspect_page(
    page: Page, output_dir: Path, filename: str = "dom.json"
) -> tuple[Path, str]:
    """只保存不含正文、输入值、Cookie 和 URL 参数的 DOM 结构。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, object]] = []
    for index, frame in enumerate(page.frames):
        try:
            nodes = _dom_skeleton(frame)
            error = ""
        except Exception as exc:  # 跨域 frame 由 Playwright 处理；失败时保留诊断。
            nodes = []
            error = type(exc).__name__
        frames.append(
            {
                "index": index,
                "name_present": bool(frame.name),
                "url": safe_url(frame.url),
                "error": error,
                "nodes": nodes,
            }
        )

    captured_page_url = safe_url(page.url)
    dom_path = output_dir / filename
    dom_path.write_text(
        json.dumps(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "page_url": captured_page_url,
                "frames": frames,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dom_path, captured_page_url


def listen(adapter, settings: Settings) -> None:
    """平台无关的只读循环；适配器负责解释具体网页 DOM。"""
    store = ProcessedMessageStore(settings.state_db)
    baseline_complete = False
    try:
        while True:
            try:
                messages = adapter.read_messages()
            except LoginRequired as exc:
                print(
                    f"登录状态需要处理：{exc} 已暂停读取，请在当前 Edge 中重新登录。",
                    flush=True,
                )
                while True:
                    time.sleep(settings.poll_seconds)
                    if not adapter.is_logged_in():
                        continue
                    print("登录状态已恢复，继续只读监听。", flush=True)
                    break
                continue
            for message in messages:
                is_new = store.add_if_new(message)
                if baseline_complete and is_new and not message.is_self:
                    observed_at = datetime.now().astimezone().strftime(
                        "%Y-%m-%d %H:%M:%S %z"
                    )
                    received_at = (
                        f"平台时间：{message.time_text}"
                        if message.time_text
                        else f"本地观察时间：{observed_at}"
                    )
                    print("\n识别到新的买家消息")
                    print(f"买家会话标识：{message.conversation}")
                    print(f"消息接收时间：{received_at}")
                    print("是否首次处理：是")
                    print(f"消息内容：{message.text}", flush=True)
            if not baseline_complete:
                baseline_complete = True
                print(
                    "已建立当前会话消息基线。现在可用测试买家账号发送“你好”。",
                    flush=True,
                )
            time.sleep(settings.poll_seconds)
    finally:
        store.close()
