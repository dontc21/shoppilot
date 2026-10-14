"""仅处理人工输入模拟问题的本地 Agent 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .agent import CustomerServiceAgent, DeepSeekChatClient
from .knowledge import KnowledgeBase, KnowledgeImportError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / ".runtime" / "knowledge.db"
DEFAULT_SAMPLE = PROJECT_ROOT / "examples" / "sample_knowledge.json"


def _database_path() -> Path:
    configured = os.getenv("SHOPPILOT_KNOWLEDGE_DB")
    return Path(configured) if configured else DEFAULT_DATABASE


def run_import(path: Path) -> int:
    knowledge = KnowledgeBase(_database_path())
    counts = knowledge.import_json(path)
    print("知识导入完成：" + json.dumps(counts, ensure_ascii=False))
    print("数据库位于已忽略的本地运行目录，不会进入 Git。")
    return 0


def run_search(query: str) -> int:
    evidence = KnowledgeBase(_database_path()).search_knowledge(query)
    print(json.dumps([item.to_dict() for item in evidence], ensure_ascii=False, indent=2))
    return 0


def run_ask() -> int:
    knowledge = KnowledgeBase(_database_path())
    client = DeepSeekChatClient.from_environment()
    agent = CustomerServiceAgent(knowledge, client)
    print("仅输入模拟商品问题；不要输入真实买家消息、订单号或个人信息。")
    print("本命令只输出候选回复和审核状态，不连接浏览器、不发送消息。")
    print("输入 exit 退出。")
    while True:
        question = input("\n问题> ").strip()
        if question.lower() in {"exit", "quit"}:
            return 0
        decision = agent.answer(question)
        print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ShopPilotBot 本地知识库与 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="导入本地知识 JSON")
    import_parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_SAMPLE,
        help="默认导入明确标记为模拟数据的示例文件",
    )

    search_parser = subparsers.add_parser("search", help="只查询本地知识库")
    search_parser.add_argument("query")
    subparsers.add_parser("ask", help="用人工输入的模拟问题测试 DeepSeek Agent")
    return parser


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    try:
        if args.command == "import":
            return run_import(args.path)
        if args.command == "search":
            return run_search(args.query)
        return run_ask()
    except (KnowledgeImportError, OSError, ValueError) as exc:
        print(f"无法启动：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已退出，未发送任何客服消息。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
