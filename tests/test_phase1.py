import json
from pathlib import Path

import pytest

from shoppilotbot.bilibili import BilibiliShopAdapter
from shoppilotbot.browser import (
    BrowserSafetyStop,
    IncomingMessage,
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


def test_safe_url_keeps_bilibili_hash_route_without_parameters() -> None:
    raw = "https://shop.bilibili.com/?token=secret#/customer/chat?access_token=secret"
    assert safe_url(raw) == "https://shop.bilibili.com/#/customer/chat"


def test_verified_chat_url_uses_observed_main_document_path() -> None:
    assert is_verified_chat_url(
        "https://shop.bilibili.com/customer-service/im?temporary=value"
    )
    assert not is_verified_chat_url("https://shop.bilibili.com/#/db/home")


def test_bilibili_adapter_cannot_send_in_phase_one() -> None:
    adapter = BilibiliShopAdapter(
        object(), BilibiliSelectors(), dry_run=True  # type: ignore[arg-type]
    )
    with pytest.raises(BrowserSafetyStop, match="DRY_RUN"):
        adapter.send_message("buyer-1", "测试回复")


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
