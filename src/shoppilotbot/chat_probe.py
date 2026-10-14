from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Frame, Page

from .browser import BrowserSafetyStop, safe_url


VERIFIED_CHAT_HOST = "shop.bilibili.com"
VERIFIED_CHAT_PATH = "/customer-service/im"


def is_verified_chat_url(raw_url: str) -> bool:
    """只使用此前诊断实际观察到的客服主文档地址。"""
    parts = urlsplit(raw_url)
    return (
        parts.scheme == "https"
        and parts.netloc == VERIFIED_CHAT_HOST
        and parts.path.rstrip("/") == VERIFIED_CHAT_PATH
    )


def find_verified_chat_page(context: BrowserContext) -> Page | None:
    """从 context.pages 中寻找已打开的客服页，不假设首页就是客服页。"""
    matches = [
        page
        for page in context.pages
        if not page.is_closed() and is_verified_chat_url(page.url)
    ]
    if len(matches) > 1:
        raise BrowserSafetyStop("发现多个已打开的客服标签页，无法安全选择目标页。")
    return matches[0] if matches else None


def _matching_nodes(frame: Frame, target_text: str) -> list[dict[str, object]]:
    # 只匹配店主明确提供的测试文本；不返回页面中的其他聊天正文。
    return frame.evaluate(
        r"""
        (targetText) => {
          const normalized = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const safeToken = (value) => {
            const text = (value || '').slice(0, 80);
            return /^[A-Za-z_-][A-Za-z0-9_-]*$/.test(text) && !/\d{4}/.test(text)
              ? text : '';
          };
          const describe = (el) => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return {
              tag: el.tagName.toLowerCase(),
              id: safeToken(el.id),
              classes: Array.from(el.classList || []).map(safeToken).filter(Boolean).slice(0, 12),
              role: safeToken(el.getAttribute('role') || ''),
              attribute_names: Array.from(el.attributes || [])
                .map((attribute) => attribute.name)
                .filter((name) => !['style', 'value', 'href', 'src'].includes(name))
                .slice(0, 24),
              child_index: el.parentElement
                ? Array.prototype.indexOf.call(el.parentElement.children, el)
                : 0,
              child_count: el.children.length,
              visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden',
              rect: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
              }
            };
          };
          const candidates = Array.from(document.querySelectorAll('body *')).filter((el) => {
            if (normalized(el.innerText) !== targetText) return false;
            return Array.from(el.children).every(
              (child) => normalized(child.innerText) !== targetText
            );
          });
          return candidates.slice(-20).map((candidate) => {
            const ancestors = [];
            let current = candidate;
            for (let depth = 0; current && depth < 10; depth += 1) {
              ancestors.push(describe(current));
              current = current.parentElement;
            }
            return { ancestors };
          });
        }
        """,
        target_text,
    )


def target_match_count(page: Page, target_text: str) -> int:
    """统计测试文本的可见叶节点，不记录任何其他消息正文。"""
    total = 0
    for frame in page.frames:
        try:
            candidates = _matching_nodes(frame, target_text)
        except Exception:
            continue
        total += sum(
            bool(candidate.get("ancestors"))
            and bool(candidate["ancestors"][0].get("visible"))
            for candidate in candidates
        )
    return total


def capture_chat_probe(page: Page, target_text: str, output_dir: Path) -> Path:
    """保存围绕测试文本的脱敏 DOM 路径，不保存聊天正文或属性值。"""
    frames: list[dict[str, object]] = []
    for index, frame in enumerate(page.frames):
        try:
            candidates = _matching_nodes(frame, target_text)
            error = ""
        except Exception as exc:
            candidates = []
            error = type(exc).__name__
        frames.append(
            {
                "index": index,
                "url": safe_url(frame.url),
                "error": error,
                "matching_candidates": candidates,
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "chat-test-message-structure.json"
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "page_url": safe_url(page.url),
        "target_sha256": hashlib.sha256(target_text.encode("utf-8")).hexdigest(),
        "privacy_note": "未保存聊天正文、属性值、Cookie、Token 或截图。",
        "frames": frames,
    }
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
