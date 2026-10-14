from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum

from playwright.sync_api import Frame, Locator, Page

from .browser import (
    BrowserSafetyStop,
    IncomingMessage,
    LoginRequired,
    normalize_text,
)
from .config import BilibiliSelectors


class SendStatus(StrEnum):
    DRY_RUN = "DRY_RUN"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    conversation: str
    detail: str


@dataclass
class OneTimeTestSendPermit:
    """只授权一个确定会话和一段确定文本，消费后不可复用。"""

    conversation_hash: str
    text_hash: str
    consumed: bool = False

    @classmethod
    def issue(cls, conversation_id: str, text: str) -> "OneTimeTestSendPermit":
        return cls(
            conversation_hash=hashlib.sha256(conversation_id.encode("utf-8")).hexdigest(),
            text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def consume(self, conversation_id: str, text: str) -> None:
        conversation_hash = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.consumed:
            raise BrowserSafetyStop("一次性测试发送授权已经使用，禁止重复发送。")
        if not secrets.compare_digest(self.conversation_hash, conversation_hash):
            raise BrowserSafetyStop("测试发送授权与当前会话不一致。")
        if not secrets.compare_digest(self.text_hash, text_hash):
            raise BrowserSafetyStop("测试发送授权与待发送正文不一致。")
        self.consumed = True


class BilibiliShopAdapter:
    """B站小店客服网页适配器；阶段一只读取当前已选会话。"""

    def __init__(
        self, page: Page, selectors: BilibiliSelectors, *, dry_run: bool
    ) -> None:
        self.page = page
        self.selectors = selectors
        self.dry_run = dry_run
        self._test_send_permit: OneTimeTestSendPermit | None = None

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

    def _active_conversation(self, scope: Page | Frame) -> Locator:
        active = scope.locator(self.selectors.active_conversation)
        if active.count() != 1 or not active.first.is_visible():
            raise BrowserSafetyStop(
                "无法唯一确认当前买家会话；请保持一个会话处于选中状态。"
            )
        return active.first

    def current_conversation_id(self) -> str:
        """返回当前会话的平台 data-mid，仅在内存中用于强一致校验。"""
        scope = self._choose_scope()
        active = self._active_conversation(scope)
        attribute = self.selectors.conversation_id_attribute
        if not attribute:
            raise BrowserSafetyStop("未配置稳定会话 ID 属性，禁止发送。")
        conversation_id = active.get_attribute(attribute) or ""
        if not conversation_id:
            raise BrowserSafetyStop("当前会话缺少稳定 data-mid，禁止发送。")
        return conversation_id

    @staticmethod
    def masked_conversation_id(conversation_id: str) -> str:
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return f"会话-{digest[:10]}"

    def arm_test_send(self, permit: OneTimeTestSendPermit) -> None:
        """仅供专用测试命令注入一次性授权；普通监听不会调用。"""
        if not self.dry_run:
            raise BrowserSafetyStop("全局 DRY_RUN 必须保持开启，禁止测试授权。")
        if self._test_send_permit is not None:
            raise BrowserSafetyStop("已有测试发送授权，禁止重复授权。")
        self._test_send_permit = permit

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
        active = self._active_conversation(scope)
        conversation_name = self._descendant_text(
            active, self.selectors.conversation_name
        )
        conversation_key = self._conversation_key(active, conversation_name)

        messages = scope.locator(self.selectors.message_items)
        if messages.count() == 0:
            raise BrowserSafetyStop("当前会话没有可识别消息；客服 DOM 可能已变化。")
        return self._read_current_messages(scope, conversation_key)

    def _count_exact_self_messages(self, scope: Page | Frame, text: str) -> int:
        expected = normalize_text(text)
        count = 0
        nodes = scope.locator(self.selectors.message_items)
        for index in range(nodes.count()):
            node = nodes.nth(index)
            if not node.is_visible():
                continue
            if not self._matches_or_contains(node, self.selectors.self_message):
                continue
            if self._descendant_text(node, self.selectors.message_text) == expected:
                count += 1
        return count

    def send_message(self, conversation_id: str, text: str) -> SendResult:
        """校验会话并单击一次发送；无法确认结果时返回 UNKNOWN，绝不重试。"""
        text = text.strip()
        if not text:
            raise BrowserSafetyStop("测试回复不能为空。")
        if len(text) > 500:
            raise BrowserSafetyStop("测试回复超过 500 字，已拒绝发送。")

        current_id = self.current_conversation_id()
        if not secrets.compare_digest(
            current_id.encode("utf-8"), conversation_id.encode("utf-8")
        ):
            raise BrowserSafetyStop("当前会话与目标 data-mid 不一致，禁止发送。")
        masked = self.masked_conversation_id(current_id)

        permit = self._test_send_permit
        if permit is None:
            return SendResult(
                SendStatus.DRY_RUN,
                masked,
                "只完成会话校验和回复预览；未填写输入框、未点击发送。",
            )
        self._test_send_permit = None
        permit.consume(current_id, text)

        scope = self._choose_scope()
        message_input = scope.locator(self.selectors.message_input)
        send_button = scope.locator(self.selectors.send_button)
        if message_input.count() != 1 or not message_input.first.is_visible():
            raise BrowserSafetyStop("无法唯一确认可见消息输入框，禁止发送。")
        if send_button.count() != 1 or not send_button.first.is_visible():
            raise BrowserSafetyStop("无法唯一确认可见发送按钮，禁止发送。")
        baseline = self._count_exact_self_messages(scope, text)
        message_input.first.fill(text)
        if message_input.first.input_value() != text:
            raise BrowserSafetyStop("输入框内容校验失败，禁止点击发送。")
        if not secrets.compare_digest(
            self.current_conversation_id().encode("utf-8"),
            conversation_id.encode("utf-8"),
        ):
            raise BrowserSafetyStop("填写后当前会话发生变化，禁止点击发送。")
        if not send_button.first.is_enabled():
            raise BrowserSafetyStop("填写后发送按钮仍不可用，禁止点击发送。")

        try:
            send_button.first.click(timeout=5_000)
        except Exception as exc:
            return SendResult(
                SendStatus.UNKNOWN,
                masked,
                f"点击阶段发生 {type(exc).__name__}；禁止自动重试。",
            )

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if not secrets.compare_digest(
                    self.current_conversation_id().encode("utf-8"),
                    conversation_id.encode("utf-8"),
                ):
                    return SendResult(
                        SendStatus.UNKNOWN,
                        masked,
                        "点击后会话发生变化；发送结果未知，禁止自动重试。",
                    )
                if self._count_exact_self_messages(scope, text) > baseline:
                    return SendResult(
                        SendStatus.SENT,
                        masked,
                        "页面出现新增的同文本商家消息。",
                    )
                self.page.wait_for_timeout(250)
            except Exception as exc:
                return SendResult(
                    SendStatus.UNKNOWN,
                    masked,
                    f"发送后校验发生 {type(exc).__name__}；禁止自动重试。",
                )
        return SendResult(
            SendStatus.UNKNOWN,
            masked,
            "10 秒内未观察到新增商家消息；禁止自动重试。",
        )
