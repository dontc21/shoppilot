from __future__ import annotations

import hashlib

from playwright.sync_api import Frame, Locator, Page

from .browser import (
    BrowserSafetyStop,
    IncomingMessage,
    LoginRequired,
    normalize_text,
)
from .config import BilibiliSelectors


class BilibiliShopAdapter:
    """B站小店客服网页适配器；阶段一只读取当前已选会话。"""

    def __init__(
        self, page: Page, selectors: BilibiliSelectors, *, dry_run: bool
    ) -> None:
        if not dry_run:
            raise ValueError("B站适配器阶段一强制要求 DRY_RUN。")
        self.page = page
        self.selectors = selectors

    def _choose_scope(self) -> Page | Frame:
        needle = self.selectors.frame_url_contains
        if not needle:
            return self.page
        matches = [frame for frame in self.page.frames if needle in frame.url]
        if len(matches) != 1:
            raise BrowserSafetyStop(
                f"期望唯一 iframe URL 包含 {needle!r}，实际找到 {len(matches)} 个。"
            )
        return matches[0]

    @staticmethod
    def _descendant_text(parent: Locator, selector: str) -> str:
        target = parent if selector.strip() == ":scope" else parent.locator(selector).first
        if target.count() == 0:
            return ""
        return normalize_text(target.inner_text(timeout=2_000))

    @staticmethod
    def _matches_or_contains(node: Locator, selector: str) -> bool:
        return bool(
            node.evaluate(
                "(el, selector) => el.matches(selector) || Boolean(el.querySelector(selector))",
                selector,
            )
        )

    def is_logged_in(self) -> bool:
        """只根据已验证的页面标志判断登录状态。"""
        if self.page.is_closed():
            raise BrowserSafetyStop("B站小店页面已关闭。")
        scope = self._choose_scope()
        if self.selectors.login_lost:
            if scope.locator(self.selectors.login_lost).first.is_visible():
                return False
        if self.selectors.login_ready:
            return scope.locator(self.selectors.login_ready).first.is_visible()
        return scope.locator(self.selectors.chat_ready).first.is_visible()

    def matches_chat_page(self) -> bool:
        """用已验证选择器识别标签页，不依赖猜测 URL。"""
        try:
            scope = self._choose_scope()
            return scope.locator(self.selectors.chat_ready).first.is_visible()
        except Exception:
            return False

    def _conversation_key(self, item: Locator, name: str) -> str:
        attribute = self.selectors.conversation_id_attribute
        raw = item.get_attribute(attribute) if attribute else ""
        stable_value = raw or name
        if not stable_value:
            raise BrowserSafetyStop("当前会话缺少可用标识，已停止只读监听。")
        return hashlib.sha256(stable_value.encode("utf-8")).hexdigest()

    def _read_current_messages(
        self, scope: Page | Frame, conversation_key: str
    ) -> list[IncomingMessage]:
        result: list[IncomingMessage] = []
        occurrences: dict[tuple[str, str, bool], int] = {}
        nodes = scope.locator(self.selectors.message_items)
        for index in range(nodes.count()):
            node = nodes.nth(index)
            if not node.is_visible():
                continue
            text = self._descendant_text(node, self.selectors.message_text)
            if not text:
                continue
            message_id = ""
            if self.selectors.message_id_attribute:
                message_id = (
                    node.get_attribute(self.selectors.message_id_attribute) or ""
                )
            time_text = (
                self._descendant_text(node, self.selectors.message_time)
                if self.selectors.message_time
                else ""
            )
            is_self = self._matches_or_contains(node, self.selectors.self_message)
            signature = (text, time_text, is_self)
            occurrence = occurrences.get(signature, 0) + 1
            occurrences[signature] = occurrence
            result.append(
                IncomingMessage(
                    conversation=f"会话-{conversation_key[:10]}",
                    conversation_key=conversation_key,
                    message_id=message_id,
                    text=text,
                    time_text=time_text,
                    is_self=is_self,
                    fallback_position=str(occurrence),
                )
            )
        return result

    def read_messages(self) -> list[IncomingMessage]:
        """只读取用户已在网页中打开的会话，不点击或切换其他买家。"""
        if not self.is_logged_in():
            raise LoginRequired("B站小店客服登录状态无效。")
        scope = self._choose_scope()
        active = scope.locator(self.selectors.active_conversation)
        if active.count() != 1 or not active.first.is_visible():
            raise BrowserSafetyStop(
                "无法唯一确认当前买家会话；请保持一个会话处于选中状态。"
            )
        active = active.first
        conversation_name = self._descendant_text(
            active, self.selectors.conversation_name
        )
        conversation_key = self._conversation_key(active, conversation_name)

        messages = scope.locator(self.selectors.message_items)
        if messages.count() == 0:
            raise BrowserSafetyStop("当前会话没有可识别消息；客服 DOM 可能已变化。")
        return self._read_current_messages(scope, conversation_key)

    def send_message(self, conversation_id: str, text: str) -> None:
        """阶段一硬禁止发送；第三阶段经店主确认后再实现。"""
        del conversation_id, text
        raise BrowserSafetyStop("DRY_RUN：B站小店消息发送尚未启用。")
