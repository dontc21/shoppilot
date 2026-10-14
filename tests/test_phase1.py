import json
from pathlib import Path

import pytest

from shoppilotbot.bilibili import (
    BilibiliShopAdapter,
    OneTimeTestSendPermit,
    SendStatus,
)
from shoppilotbot.browser import (
    BrowserSafetyStop,
    IncomingMessage,
    OutgoingMessageStore,
    ProcessedMessageStore,
    message_key,
    normalize_text,
    safe_url,
)
from shoppilotbot.config import BilibiliSelectors
from shoppilotbot.chat_probe import is_verified_chat_url
from shoppilotbot.diagnostics import (
    diagnostic_url,
    sanitize_diagnostic_text,
    sanitize_diagnostic_value,
)
from shoppilotbot.stage2 import TEST_REPLY, confirmation_matches


def make_message(*, conversation_key: str = "buyer-a") -> IncomingMessage:
    return IncomingMessage(
        conversation="测试买家",
        conversation_key=conversation_key,
        message_id="message-1",
        text="你好",
        time_text="10:00",
        is_self=False,
    )


def test_normalize_text() -> None:
    assert normalize_text("  你\n 好  ") == "你 好"


def test_message_key_prefers_platform_id() -> None:
    first = message_key("buyer-a", "id-1", "旧正文", "10:00", False)
    second = message_key("buyer-a", "id-1", "新正文", "11:00", True)
    assert first == second


def test_same_message_id_in_different_conversations_is_not_mixed() -> None:
    first = message_key("buyer-a", "id-1", "你好", "10:00", False)
    second = message_key("buyer-b", "id-1", "你好", "10:00", False)
    assert first != second


def test_fallback_key_uses_occurrence_not_only_message_text() -> None:
    first = message_key("buyer-a", "", "你好", "10:00", False, "1")
    second = message_key("buyer-a", "", "你好", "10:00", False, "2")
    assert first != second


def test_sqlite_store_rejects_duplicate(tmp_path: Path) -> None:
    store = ProcessedMessageStore(tmp_path / "state.db")
    try:
        message = make_message()
        assert store.add_if_new(message) is True
        assert store.add_if_new(message) is False
    finally:
        store.close()


def test_sqlite_store_keeps_dedup_state_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_store = ProcessedMessageStore(path)
    first_store.add_if_new(make_message())
    first_store.close()

    second_store = ProcessedMessageStore(path)
    try:
        assert second_store.add_if_new(make_message()) is False
    finally:
        second_store.close()


def test_listener_requires_a_login_state_marker(tmp_path: Path) -> None:
    selector_path = tmp_path / "selectors.json"
    selector_path.write_text(
        json.dumps(
            {
                "login_ready": "",
                "login_lost": "",
                "conversation_items": ".conversation",
                "conversation_name": ".name",
                "message_items": ".message",
                "message_text": ".text",
                "self_message": ".from-shop",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="login_ready 或 login_lost"):
        BilibiliSelectors.load(selector_path)


def test_verified_selector_example_is_loadable() -> None:
    selector_path = Path(__file__).parents[1] / "config" / "selectors.example.json"
    selectors = BilibiliSelectors.load(selector_path)

    assert selectors.conversation_id_attribute == "data-mid"
    assert selectors.message_items
    assert selectors.self_message
    assert selectors.message_input == "textarea#chat-textarea"
    assert selectors.send_button


def test_safe_url_keeps_bilibili_hash_route_without_parameters() -> None:
    raw = "https://shop.bilibili.com/?token=secret#/customer/chat?access_token=secret"
    assert safe_url(raw) == "https://shop.bilibili.com/#/customer/chat"


def test_verified_chat_url_uses_observed_main_document_path() -> None:
    assert is_verified_chat_url(
        "https://shop.bilibili.com/customer-service/im?temporary=value"
    )
    assert not is_verified_chat_url("https://shop.bilibili.com/#/db/home")


def test_test_send_permit_is_bound_and_one_time() -> None:
    permit = OneTimeTestSendPermit.issue("conversation-1", "测试回复")
    permit.consume("conversation-1", "测试回复")

    with pytest.raises(BrowserSafetyStop, match="已经使用"):
        permit.consume("conversation-1", "测试回复")


def test_test_send_permit_rejects_other_conversation() -> None:
    permit = OneTimeTestSendPermit.issue("conversation-1", "测试回复")

    with pytest.raises(BrowserSafetyStop, match="会话不一致"):
        permit.consume("conversation-2", "测试回复")


def test_outgoing_store_blocks_duplicate_and_persists_status(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = OutgoingMessageStore(path)
    request_key, reserved = store.reserve("conversation-1", "测试回复")
    assert reserved is True
    store.mark(request_key, "SENT")
    store.close()

    reopened = OutgoingMessageStore(path)
    try:
        same_key, reserved_again = reopened.reserve("conversation-1", "测试回复")
        assert same_key == request_key
        assert reserved_again is False
        assert reopened.status(request_key) == "SENT"
        stored = reopened.connection.execute(
            "SELECT conversation_key, text_hash FROM outgoing_messages"
        ).fetchone()
        assert stored is not None
        assert "conversation-1" not in stored
        assert "测试回复" not in stored
    finally:
        reopened.close()


def test_send_without_one_time_permit_is_dry_run() -> None:
    adapter = BilibiliShopAdapter.__new__(BilibiliShopAdapter)
    adapter._test_send_permit = None
    adapter.current_conversation_id = lambda: "conversation-1"  # type: ignore[method-assign]

    result = adapter.send_message("conversation-1", "测试回复")

    assert result.status == SendStatus.DRY_RUN
    assert "未填写输入框" in result.detail


def test_stage_two_uses_a_fixed_short_test_reply() -> None:
    assert TEST_REPLY == "ShopPilotBot 测试回复，请忽略。"
    assert len(TEST_REPLY) <= 500


def test_non_ascii_confirmation_phrase_can_be_compared() -> None:
    expected = "SEND TEST 会话-123 ABCDEF"

    assert confirmation_matches(expected, expected) is True
    assert confirmation_matches("SEND TEST 会话-456 ABCDEF", expected) is False


def test_diagnostic_url_removes_parameters_and_masks_ids() -> None:
    raw = "https://example.com/api/shop/123456789/message?token=secret"
    assert diagnostic_url(raw) == "https://example.com/api/shop/<redacted>/message"


def test_diagnostic_text_redacts_token_and_url_query() -> None:
    value = "request https://example.com/open?id=42 failed token=very-secret-value"
    cleaned = sanitize_diagnostic_text(value)
    assert "id=42" not in cleaned
    assert "very-secret-value" not in cleaned


def test_diagnostic_value_redacts_nested_strings() -> None:
    cleaned = sanitize_diagnostic_value(
        {"classes": ["message", "token=very-secret-value"]}
    )
    assert cleaned == {"classes": ["message", "token=<redacted>"]}
