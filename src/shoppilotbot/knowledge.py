"""SQLite 商品知识库与受限检索工具。"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    material TEXT NOT NULL,
    size TEXT NOT NULL,
    description TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_for_reply INTEGER NOT NULL CHECK (approved_for_reply IN (0, 1)),
    is_simulated INTEGER NOT NULL CHECK (is_simulated IN (0, 1))
);

CREATE TABLE IF NOT EXISTS faqs (
    faq_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_for_reply INTEGER NOT NULL CHECK (approved_for_reply IN (0, 1)),
    is_simulated INTEGER NOT NULL CHECK (is_simulated IN (0, 1)),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_for_reply INTEGER NOT NULL CHECK (approved_for_reply IN (0, 1)),
    is_simulated INTEGER NOT NULL CHECK (is_simulated IN (0, 1))
);
"""


@dataclass(frozen=True)
class KnowledgeEvidence:
    """返回给 Agent 的最小知识证据，不包含数据库内部信息。"""

    evidence_id: str
    kind: str
    title: str
    content: str
    source: str
    updated_at: str
    approved_for_reply: bool
    is_simulated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeImportError(ValueError):
    """本地知识 JSON 不符合约定格式。"""


class KnowledgeBase:
    """使用参数化 SQL 管理并检索本地商品知识。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def import_json(self, path: str | Path) -> dict[str, int]:
        """校验并导入本地 JSON；相同 ID 会被明确覆盖更新。"""

        source_path = Path(path)
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeImportError(f"无法读取知识文件：{exc}") from exc

        if not isinstance(payload, dict):
            raise KnowledgeImportError("知识文件根节点必须是 JSON 对象")
        if payload.get("schema_version") != 1:
            raise KnowledgeImportError("仅支持 schema_version=1")

        products = self._require_list(payload, "products")
        faqs = self._require_list(payload, "faqs")
        policies = self._require_list(payload, "policies")

        with self._connect() as connection:
            for item in products:
                values = self._validated_item(
                    item,
                    required=(
                        "id",
                        "name",
                        "material",
                        "size",
                        "description",
                        "source",
                        "updated_at",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO products (
                        product_id, name, material, size, description, source,
                        updated_at, approved_for_reply, is_simulated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(product_id) DO UPDATE SET
                        name=excluded.name,
                        material=excluded.material,
                        size=excluded.size,
                        description=excluded.description,
                        source=excluded.source,
                        updated_at=excluded.updated_at,
                        approved_for_reply=excluded.approved_for_reply,
                        is_simulated=excluded.is_simulated
                    """,
                    (
                        values["id"],
                        values["name"],
                        values["material"],
                        values["size"],
                        values["description"],
                        values["source"],
                        values["updated_at"],
                        int(values["approved_for_reply"]),
                        int(values["is_simulated"]),
                    ),
                )

            for item in faqs:
                values = self._validated_item(
                    item,
                    required=(
                        "id",
                        "product_id",
                        "question",
                        "answer",
                        "source",
                        "updated_at",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO faqs (
                        faq_id, product_id, question, answer, source,
                        updated_at, approved_for_reply, is_simulated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(faq_id) DO UPDATE SET
                        product_id=excluded.product_id,
                        question=excluded.question,
                        answer=excluded.answer,
                        source=excluded.source,
                        updated_at=excluded.updated_at,
                        approved_for_reply=excluded.approved_for_reply,
                        is_simulated=excluded.is_simulated
                    """,
                    (
                        values["id"],
                        values["product_id"],
                        values["question"],
                        values["answer"],
                        values["source"],
                        values["updated_at"],
                        int(values["approved_for_reply"]),
                        int(values["is_simulated"]),
                    ),
                )

            for item in policies:
                values = self._validated_item(
                    item,
                    required=("id", "title", "content", "source", "updated_at"),
                )
                connection.execute(
                    """
                    INSERT INTO policies (
                        policy_id, title, content, source, updated_at,
                        approved_for_reply, is_simulated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(policy_id) DO UPDATE SET
                        title=excluded.title,
                        content=excluded.content,
                        source=excluded.source,
                        updated_at=excluded.updated_at,
                        approved_for_reply=excluded.approved_for_reply,
                        is_simulated=excluded.is_simulated
                    """,
                    (
                        values["id"],
                        values["title"],
                        values["content"],
                        values["source"],
                        values["updated_at"],
                        int(values["approved_for_reply"]),
                        int(values["is_simulated"]),
                    ),
                )

        return {
            "products": len(products),
            "faqs": len(faqs),
            "policies": len(policies),
        }

    @staticmethod
    def _require_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        value = payload.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise KnowledgeImportError(f"{key} 必须是对象数组")
        return value

    @staticmethod
    def _validated_item(
        item: dict[str, Any], *, required: tuple[str, ...]
    ) -> dict[str, Any]:
        missing = [key for key in required if not isinstance(item.get(key), str) or not item[key].strip()]
        if missing:
            raise KnowledgeImportError(f"知识条目缺少有效字段：{', '.join(missing)}")
        oversized = [key for key in required if len(item[key]) > 4_000]
        if oversized:
            raise KnowledgeImportError(f"知识字段超过 4000 字：{', '.join(oversized)}")
        for key in ("approved_for_reply", "is_simulated"):
            if not isinstance(item.get(key), bool):
                raise KnowledgeImportError(f"{key} 必须是布尔值")
        return item

    def search_knowledge(self, query: str, limit: int = 5) -> list[KnowledgeEvidence]:
        """按关键词查询固定表和固定列，模型不能提交任意 SQL。"""

        terms = self._search_terms(query)
        if not terms:
            return []
        safe_limit = max(1, min(int(limit), 8))
        matches: list[tuple[int, KnowledgeEvidence]] = []

        with self._connect() as connection:
            product_rows = self._select_matching(
                connection,
                table="products",
                columns=("product_id", "name", "material", "size", "description"),
                terms=terms,
                limit=safe_limit,
            )
            for row in product_rows:
                content = (
                    f"商品名称：{row['name']}；材质：{row['material']}；"
                    f"尺寸：{row['size']}；介绍：{row['description']}"
                )
                evidence = KnowledgeEvidence(
                    evidence_id=f"product:{row['product_id']}",
                    kind="product",
                    title=row["name"],
                    content=content,
                    source=row["source"],
                    updated_at=row["updated_at"],
                    approved_for_reply=bool(row["approved_for_reply"]),
                    is_simulated=bool(row["is_simulated"]),
                )
                matches.append((self._score(terms, f"{row['name']} {content}"), evidence))

            faq_rows = self._select_matching(
                connection,
                table="faqs",
                columns=("faq_id", "product_id", "question", "answer"),
                terms=terms,
                limit=safe_limit,
            )
            for row in faq_rows:
                content = f"常见问题：{row['question']}；标准答案：{row['answer']}"
                evidence = KnowledgeEvidence(
                    evidence_id=f"faq:{row['faq_id']}",
                    kind="faq",
                    title=row["question"],
                    content=content,
                    source=row["source"],
                    updated_at=row["updated_at"],
                    approved_for_reply=bool(row["approved_for_reply"]),
                    is_simulated=bool(row["is_simulated"]),
                )
                matches.append((self._score(terms, content), evidence))

            policy_rows = self._select_matching(
                connection,
                table="policies",
                columns=("policy_id", "title", "content"),
                terms=terms,
                limit=safe_limit,
            )
            for row in policy_rows:
                content = f"规则：{row['title']}；内容：{row['content']}"
                evidence = KnowledgeEvidence(
                    evidence_id=f"policy:{row['policy_id']}",
                    kind="policy",
                    title=row["title"],
                    content=content,
                    source=row["source"],
                    updated_at=row["updated_at"],
                    approved_for_reply=bool(row["approved_for_reply"]),
                    is_simulated=bool(row["is_simulated"]),
                )
                matches.append((self._score(terms, content), evidence))

        matches.sort(key=lambda item: (-item[0], item[1].evidence_id))
        return [evidence for _, evidence in matches[:safe_limit]]

    @staticmethod
    def _search_terms(query: str) -> list[str]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        parts = [part for part in re.split(r"[\s,，。！？?；;：:、]+", normalized) if part]
        unique: list[str] = []
        for part in parts:
            if part not in unique:
                unique.append(part[:80])
        return unique[:6]

    @staticmethod
    def _select_matching(
        connection: sqlite3.Connection,
        *,
        table: str,
        columns: tuple[str, ...],
        terms: list[str],
        limit: int,
    ) -> list[sqlite3.Row]:
        # table/columns 只由本模块的常量传入；用户内容全部使用参数绑定。
        where_parts: list[str] = []
        parameters: list[Any] = []
        for term in terms:
            for column in columns:
                where_parts.append(f"LOWER({column}) LIKE ?")
                parameters.append(f"%{term}%")
        parameters.append(limit)
        sql = f"SELECT * FROM {table} WHERE {' OR '.join(where_parts)} LIMIT ?"
        return list(connection.execute(sql, parameters))

    @staticmethod
    def _score(terms: list[str], text: str) -> int:
        haystack = text.lower()
        return sum(haystack.count(term) for term in terms)


def search_knowledge(
    database_path: str | Path, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """提供给 Agent 的简单 Python 工具入口。"""

    knowledge = KnowledgeBase(database_path)
    return [item.to_dict() for item in knowledge.search_knowledge(query, limit)]
