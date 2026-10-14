from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    """只保存阶段一需要的、非秘密运行配置。"""

    shop_url: str
    browser_channel: str | None
    dry_run: bool
    profile_dir: Path
    state_db: Path
    selector_file: Path
    inspection_dir: Path
    diagnostic_dir: Path
    poll_seconds: float

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            shop_url=os.getenv(
                "BILIBILI_SHOP_URL", "https://shop.bilibili.com/#/db/home"
            ),
            browser_channel=(
                os.getenv("SHOPPILOT_BROWSER_CHANNEL", "msedge").strip() or None
            ),
            dry_run=os.getenv("SHOPPILOT_DRY_RUN", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            profile_dir=_project_path(
                os.getenv("SHOPPILOT_PROFILE_DIR", "edge_bilibili_profile")
            ),
            state_db=_project_path(
                os.getenv("SHOPPILOT_STATE_DB", ".runtime/bilibili_state.db")
            ),
            selector_file=_project_path(
                os.getenv(
                    "SHOPPILOT_SELECTOR_FILE", ".runtime/bilibili_selectors.json"
                )
            ),
            inspection_dir=_project_path(
                os.getenv(
                    "SHOPPILOT_INSPECTION_DIR", ".runtime/bilibili_inspection"
                )
            ),
            diagnostic_dir=_project_path(
                os.getenv(
                    "SHOPPILOT_DIAGNOSTIC_DIR", ".runtime/bilibili_diagnostics"
                )
            ),
            poll_seconds=float(os.getenv("SHOPPILOT_POLL_SECONDS", "2")),
        )


@dataclass(frozen=True)
class BilibiliSelectors:
    """只允许填写从 B站小店真实客服 DOM 验证得到的选择器。"""

    frame_url_contains: str = ""
    login_ready: str = ""
    login_lost: str = ".login__main"
    chat_ready: str = ""
    conversation_items: str = ""
    active_conversation: str = ""
    conversation_name: str = ""
    conversation_id_attribute: str = ""
    unread_badge: str = ""
    active_conversation_name: str = ""
    message_items: str = ""
    message_text: str = ""
    message_time: str = ""
    message_id_attribute: str = ""
    self_message: str = ""
    message_input: str = ""
    send_button: str = ""

    @classmethod
    def load(cls, path: Path) -> "BilibiliSelectors":
        if not path.exists():
            raise FileNotFoundError(
                f"尚未找到选择器配置：{path}\n"
                "请先运行 inspect，并根据真实 DOM 完成 bilibili_selectors.json。"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"selectors.json 含未知字段：{', '.join(unknown)}")
        selectors = cls(**raw)
        required = {
            "chat_ready": selectors.chat_ready,
            "active_conversation": selectors.active_conversation,
            "conversation_name": selectors.conversation_name,
            "message_items": selectors.message_items,
            "message_text": selectors.message_text,
            "self_message": selectors.self_message,
            "message_input": selectors.message_input,
            "send_button": selectors.send_button,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if not selectors.login_ready.strip() and not selectors.login_lost.strip():
            missing.append("login_ready 或 login_lost")
        if missing:
            raise ValueError(
                "以下选择器仍为空，不能安全监听：" + ", ".join(missing)
            )
        return selectors
