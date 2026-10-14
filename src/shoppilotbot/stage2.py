from __future__ import annotations

import argparse
import secrets
import time
from datetime import datetime

from playwright.sync_api import BrowserContext, sync_playwright

from .bilibili import (
    BilibiliShopAdapter,
    OneTimeTestSendPermit,
    SendStatus,
)
from .browser import (
    BrowserSafetyStop,
    OutgoingMessageStore,
    ProcessedMessageStore,
    launch_browser,
)
from .config import BilibiliSelectors, Settings


TEST_REPLY = "ShopPilotBot 测试回复，请忽略。"


def confirmation_matches(typed: str, expected: str) -> bool:
    """compare_digest 的 str 模式仅支持 ASCII；中文短语需比较 UTF-8 字节。"""
    return secrets.compare_digest(typed.encode("utf-8"), expected.encode("utf-8"))


def _close_context_safely(context: BrowserContext) -> None:
    try:
        context.close()
    except Exception:
        pass


def _pump_browser_events(context: BrowserContext, milliseconds: int = 500) -> None:
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        raise BrowserSafetyStop("所有 Edge 页面均已关闭。")
    pages[0].wait_for_timeout(milliseconds)


def _open_start_page(context: BrowserContext, url: str) -> None:
    page = context.pages[0] if context.pages else context.new_page()
    if page.url == "about:blank":
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)


def _find_adapter(
    context: BrowserContext, selectors: BilibiliSelectors, *, dry_run: bool
) -> BilibiliShopAdapter | None:
    candidates: list[BilibiliShopAdapter] = []
    for page in context.pages:
        if page.is_closed():
            continue
        adapter = BilibiliShopAdapter(page, selectors, dry_run=dry_run)
        if adapter.matches_chat_page():
            candidates.append(adapter)
    if len(candidates) > 1:
        raise BrowserSafetyStop("多个标签页同时匹配客服 DOM，无法安全确定目标页。")
    return candidates[0] if candidates else None


def _wait_for_adapter(
    context: BrowserContext, settings: Settings, selectors: BilibiliSelectors
) -> BilibiliShopAdapter:
    adapter = _find_adapter(context, selectors, dry_run=settings.dry_run)
    if adapter is None:
        _open_start_page(context, settings.shop_url)
        print("请在当前 Edge 中手动进入 B站小店客服页；程序不会刷新或发送。")
    while adapter is None:
        _pump_browser_events(context)
        adapter = _find_adapter(context, selectors, dry_run=settings.dry_run)
    return adapter


def run_direction_probe(settings: Settings) -> int:
    """重启后核对历史去重，并观察新增买家/商家消息方向。"""
    if not settings.dry_run:
        raise BrowserSafetyStop("方向验证必须保持 SHOPPILOT_DRY_RUN=true。")
    selectors = BilibiliSelectors.load(settings.selector_file)
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        try:
            adapter = _wait_for_adapter(context, settings, selectors)
            store = ProcessedMessageStore(settings.state_db)
            try:
                messages = adapter.read_messages()
                known = 0
                added_to_baseline = 0
                for message in messages:
                    if store.add_if_new(message):
                        added_to_baseline += 1
                    else:
                        known += 1
                print(
                    "重启去重核验："
                    f"当前可见 {len(messages)} 条，SQLite 已识别 {known} 条，"
                    f"新加入基线 {added_to_baseline} 条。",
                    flush=True,
                )
                if messages and added_to_baseline == 0:
                    print("真实跨重启去重验证通过：历史消息未再次作为新消息输出。")
                else:
                    print("真实跨重启去重尚不能判定；请检查当前会话和可见历史范围。")
                print(
                    "方向观察已就绪。请先由测试买家发送消息，再由商家在网页中手动回复。",
                    flush=True,
                )
                while True:
                    for message in adapter.read_messages():
                        if not store.add_if_new(message):
                            continue
                        observed_at = datetime.now().astimezone().strftime(
                            "%Y-%m-%d %H:%M:%S %z"
                        )
                        received_at = message.time_text or f"本地观察 {observed_at}"
                        direction = "商家自己发送" if message.is_self else "买家发送"
                        print("\n识别到方向验证消息")
                        print(f"会话：{message.conversation}")
                        print(f"方向：{direction}")
                        print(f"时间：{received_at}")
                        print(f"内容：{message.text}", flush=True)
                    time.sleep(settings.poll_seconds)
            finally:
                store.close()
        finally:
            _close_context_safely(context)


def run_test_send(settings: Settings) -> int:
    """预览固定回复，并在一次性挑战确认后只点击一次发送。"""
    if not settings.dry_run:
        raise BrowserSafetyStop(
            "专用测试发送要求全局 DRY_RUN 保持开启；真实发送只由一次性授权放行。"
        )
    selectors = BilibiliSelectors.load(settings.selector_file)
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        try:
            adapter = _wait_for_adapter(context, settings, selectors)
            conversation_id = adapter.current_conversation_id()
            preview = adapter.send_message(conversation_id, TEST_REPLY)
            print("测试发送预览")
            print(f"目标：{preview.conversation}")
            print(f"正文：{TEST_REPLY}")
            print(f"状态：{preview.status}")
            print(preview.detail)

            challenge = secrets.token_hex(3).upper()
            confirmation = f"SEND TEST {preview.conversation} {challenge}"
            print("\n一次性授权短语如下；只有完全一致才会放行一次点击：")
            print(confirmation, flush=True)
            typed = input("请输入一次性授权短语（直接回车则取消）：").strip()
            if not confirmation_matches(typed, confirmation):
                print("授权未通过，未填写输入框、未点击发送。")
                return 0

            outgoing = OutgoingMessageStore(settings.state_db)
            request_key = ""
            try:
                request_key, reserved = outgoing.reserve(conversation_id, TEST_REPLY)
                if not reserved:
                    status = outgoing.status(request_key) or "UNKNOWN"
                    raise BrowserSafetyStop(
                        f"相同会话和正文已有发送记录（{status}），禁止重复点击。"
                    )
                permit = OneTimeTestSendPermit.issue(conversation_id, TEST_REPLY)
                adapter.arm_test_send(permit)
                try:
                    result = adapter.send_message(conversation_id, TEST_REPLY)
                except Exception:
                    outgoing.mark(request_key, "ABORTED")
                    raise
                if result.status == SendStatus.SENT:
                    outgoing.mark(request_key, "SENT")
                elif result.status == SendStatus.UNKNOWN:
                    outgoing.mark(request_key, "UNKNOWN")
                else:
                    outgoing.mark(request_key, "ABORTED")
                print(f"发送结果：{result.status}")
                print(result.detail)
                _, duplicate_allowed = outgoing.reserve(conversation_id, TEST_REPLY)
                print(
                    "重复发送保护："
                    + ("失败" if duplicate_allowed else "已阻止相同会话和正文再次发送"),
                    flush=True,
                )
            finally:
                outgoing.close()

            print("浏览器将保持打开，请在测试买家端确认。按 Ctrl+C 才退出。")
            while True:
                _pump_browser_events(context, milliseconds=1_000)
        finally:
            _close_context_safely(context)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShopPilotBot 第二阶段安全验收")
    parser.add_argument("command", choices=("verify", "send-test"))
    args = parser.parse_args(argv)
    settings = Settings.load()
    try:
        if args.command == "verify":
            return run_direction_probe(settings)
        return run_test_send(settings)
    except KeyboardInterrupt:
        print("\n已安全停止。")
        return 0
    except BrowserSafetyStop as exc:
        print(f"安全停止：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
