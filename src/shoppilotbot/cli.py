from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import asdict

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .bilibili import BilibiliShopAdapter
from .browser import BrowserSafetyStop, inspect_page, launch_browser, listen
from .chat_probe import (
    capture_chat_probe,
    find_verified_chat_page,
    target_match_count,
)
from .config import BilibiliSelectors, Settings
from .diagnostics import EntryDiagnosticRecorder, find_entry_page


def _open_start_page(context, url: str):
    page = context.pages[0] if context.pages else context.new_page()
    if page.url == "about:blank":
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    return page


def _close_context_safely(context) -> None:
    """Ctrl+C 可能先终止浏览器驱动；此时关闭失败不应打印长堆栈。"""
    try:
        context.close()
    except Exception:
        # 这里只清理已经结束的浏览器进程，不掩盖主流程中的页面错误。
        pass


def _write_selector_template(settings: Settings) -> None:
    template = settings.inspection_dir / "selectors.template.json"
    template.write_text(
        json.dumps(asdict(BilibiliSelectors()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not settings.selector_file.exists():
        settings.selector_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, settings.selector_file)


def _find_chat_adapter(context, selectors, *, dry_run: bool):
    candidates = []
    for page in context.pages:
        if page.is_closed():
            continue
        adapter = BilibiliShopAdapter(page, selectors, dry_run=dry_run)
        if adapter.matches_chat_page():
            candidates.append(adapter)
    if len(candidates) > 1:
        raise BrowserSafetyStop("多个标签页同时匹配客服 DOM，无法安全确定目标会话页。")
    return candidates[0] if candidates else None


def _capture_open_pages(context, settings: Settings) -> None:
    pages = [open_page for open_page in context.pages if not open_page.is_closed()]
    if not pages:
        raise BrowserSafetyStop("没有可检查的浏览器页面。")
    for index, open_page in enumerate(pages):
        filename = "dom.json" if len(pages) == 1 else f"dom-tab-{index}.json"
        dom_path, captured_url = inspect_page(
            open_page, settings.inspection_dir, filename=filename
        )
        print(f"记录标签页 {index}：{captured_url}")
        print(f"DOM 结构：{dom_path}")
    _write_selector_template(settings)
    print(f"选择器配置：{settings.selector_file}")


def run_inspect(settings: Settings) -> int:
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        try:
            _open_start_page(context, settings.shop_url)
            print("\n独立 Microsoft Edge 已打开。")
            print("请手动登录 B站小店商家后台，并进入客服聊天页面。")
            while True:
                input(
                    "页面完全显示后，回到此终端按 Enter 采集脱敏 DOM；"
                    "采集后浏览器会继续保持打开……"
                )
                _capture_open_pages(context, settings)
                print("DOM 检测完成；未保存截图，浏览器仍保持打开。")
                print(
                    "可继续浏览到其他页面后按 Enter 再次采集；"
                    "需要退出请按 Ctrl+C。",
                    flush=True,
                )
        finally:
            _close_context_safely(context)


def run_listen(settings: Settings) -> int:
    selectors = BilibiliSelectors.load(settings.selector_file)
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        try:
            _open_start_page(context, settings.shop_url)
            print("请在当前 Edge 中进入 B站小店客服页；程序不会刷新页面。")
            print("正在等待已验证的客服 DOM……", flush=True)
            adapter = None
            while adapter is None:
                adapter = _find_chat_adapter(
                    context, selectors, dry_run=settings.dry_run
                )
                if adapter is None:
                    time.sleep(settings.poll_seconds)
            print("B站小店只读监听已启动；阶段一不会发送消息。按 Ctrl+C 停止。")
            listen(adapter, settings)
            return 0
        finally:
            _close_context_safely(context)


def _wait_for_verified_selectors(context, settings: Settings):
    """允许在浏览器保持打开时更新定位配置，避免再次重启。"""
    last_problem = ""
    while True:
        try:
            selectors = BilibiliSelectors.load(settings.selector_file)
            adapter = _find_chat_adapter(
                context, selectors, dry_run=settings.dry_run
            )
            if adapter is not None:
                return adapter
            problem = "选择器已读取，但尚未唯一匹配当前客服页。"
        except (FileNotFoundError, ValueError) as exc:
            problem = str(exc).splitlines()[0]
        if problem != last_problem:
            print(f"等待已验证定位器：{problem}", flush=True)
            last_problem = problem
        _pump_browser_events(context, milliseconds=500)


def run_probe_chat(settings: Settings) -> int:
    """用一条店主发送的测试消息采集必要结构，再原进程切换为监听。"""
    target_text = "你好"
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        try:
            chat_page = find_verified_chat_page(context)
            if chat_page is None:
                _open_start_page(context, settings.shop_url)
                print("未发现已恢复的客服标签页。")
                print("请在当前 Edge 中手动进入客服系统；程序不会刷新或重复点击。")
            print("正在从 context.pages 等待已验证的客服页面……", flush=True)
            while chat_page is None:
                _pump_browser_events(context, milliseconds=500)
                chat_page = find_verified_chat_page(context)

            print("已定位客服主页面；未读取 Cookie、Token 或其他聊天正文。")
            baseline_count = target_match_count(chat_page, target_text)
            print(f"测试文本现有可见匹配数：{baseline_count}")
            print(
                "结构探测已就绪。现在请用自己的买家测试账号发送一条“你好”。",
                flush=True,
            )
            current_count = baseline_count
            while current_count <= baseline_count:
                _pump_browser_events(context, milliseconds=500)
                if chat_page.is_closed():
                    raise BrowserSafetyStop("客服标签页已关闭。")
                current_count = target_match_count(chat_page, target_text)

            probe_path = capture_chat_probe(
                chat_page, target_text, settings.inspection_dir
            )
            dom_path, _ = inspect_page(
                chat_page, settings.inspection_dir, filename="chat-dom.json"
            )
            _write_selector_template(settings)
            print(f"已捕获新增测试消息的脱敏结构：{probe_path}")
            print(f"已捕获不含正文的客服 DOM 骨架：{dom_path}")
            print("Edge 将保持打开；定位器确认后会在本进程内切换为只读监听。")

            adapter = _wait_for_verified_selectors(context, settings)
            print("定位器已通过当前客服页校验，开始建立当前会话基线。")
            listen(adapter, settings)
            return 0
        finally:
            _close_context_safely(context)


def _pump_browser_events(context, milliseconds: int = 250) -> None:
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        raise BrowserSafetyStop("所有 Edge 页面均已关闭。")
    pages[0].wait_for_timeout(milliseconds)


def run_diagnose_entry(settings: Settings) -> int:
    """在店主手动点击前布置客服入口的只读事件监听。"""
    with sync_playwright() as playwright:
        context = launch_browser(playwright, settings)
        recorder = EntryDiagnosticRecorder(settings.diagnostic_dir)
        try:
            _open_start_page(context, settings.shop_url)
            recorder.attach_context(context)
            print(f"脱敏诊断日志：{recorder.path}")
            print("正在等待商家首页出现“客服系统”入口；不会自动点击或刷新。")
            entry_page = None
            entry_count = 0
            while entry_page is None:
                entry_page, entry_count = find_entry_page(context)
                if entry_page is None:
                    _pump_browser_events(context)

            observed_count = recorder.inspect_entry(entry_page)
            if observed_count != entry_count:
                raise BrowserSafetyStop("客服入口在检查期间发生变化，请保持页面稳定。")
            recorder.arm_click_observer(entry_page)
            print("事件监听已就绪：popup、新页面、导航、console、pageerror、")
            print("requestfailed 和 document/xhr/fetch 响应。")
            print("请现在只手动点击一次“客服系统”，程序会自动捕获事件。", flush=True)

            while not recorder.trigger_observed:
                _pump_browser_events(context)
            print("已观察到客服入口点击或新页面创建，继续收集 10 秒……", flush=True)
            for _ in range(40):
                _pump_browser_events(context)
            recorder.snapshot_pages(context)
            print(
                "诊断摘要：" + json.dumps(recorder.summary(), ensure_ascii=False),
                flush=True,
            )
            print("诊断完成，Edge 保持打开；按 Ctrl+C 后才退出。", flush=True)
            while True:
                _pump_browser_events(context, milliseconds=500)
        finally:
            _close_context_safely(context)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ShopPilotBot 阶段一")
    parser.add_argument(
        "command", choices=("inspect", "probe-chat", "listen", "diagnose-entry")
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.load()
    try:
        if not settings.dry_run:
            raise ValueError("阶段一强制要求 SHOPPILOT_DRY_RUN=true，已拒绝启动。")
        if args.command == "inspect":
            return run_inspect(settings)
        if args.command == "probe-chat":
            return run_probe_chat(settings)
        if args.command == "listen":
            return run_listen(settings)
        return run_diagnose_entry(settings)
    except KeyboardInterrupt:
        print("\n已安全停止。")
        return 0
    except (BrowserSafetyStop, FileNotFoundError, ValueError) as exc:
        print(f"安全停止：{exc}", file=sys.stderr)
        return 2
    except PlaywrightError as exc:
        print(f"浏览器错误，未进行任何发送：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
