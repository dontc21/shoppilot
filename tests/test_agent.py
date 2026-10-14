from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from shoppilotbot.agent import (
    CustomerServiceAgent,
    DecisionStatus,
    DeepSeekAPIError,
)
from shoppilotbot.knowledge import KnowledgeBase, KnowledgeImportError


SAMPLE_PATH = Path(__file__).resolve().parents[1] / "examples" / "sample_knowledge.json"


class FakeClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "response_format": response_format,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_response(query: str, *, limit: int = 5) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "search_knowledge",
                                "arguments": json.dumps(
                                    {"query": query, "limit": limit},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
            }
        ]
    }


def candidate_response(
    reply: str,
    evidence_ids: list[str],
    *,
    review_reason: str = "",
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "candidate_reply": reply,
                            "evidence_ids": evidence_ids,
                            "review_reason": review_reason,
                        },
                        ensure_ascii=False,
                    ),
                },
            }
        ]
    }


def with_usage(
    response: dict[str, Any], *, prompt: int, completion: int
) -> dict[str, Any]:
    response["usage"] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": prompt,
    }
    return response


@pytest.fixture
def knowledge(tmp_path: Path) -> KnowledgeBase:
    database = KnowledgeBase(tmp_path / "knowledge.db")
    assert database.import_json(SAMPLE_PATH) == {
        "products": 1,
        "faqs": 2,
        "policies": 1,
    }
    return database


def test_import_and_search_known_material(knowledge: KnowledgeBase) -> None:
    evidence = knowledge.search_knowledge("星云随行杯 材质")

    assert evidence
    assert any(item.evidence_id == "product:SIM-CUP-001" for item in evidence)
    assert any("304 不锈钢" in item.content for item in evidence)
    assert all(item.is_simulated for item in evidence)


def test_search_uses_parameter_binding(knowledge: KnowledgeBase) -> None:
    assert knowledge.search_knowledge("'; DROP TABLE products; --") == []

    with sqlite3.connect(knowledge.path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    assert count == 1


def test_import_rejects_unversioned_json(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text('{"products": []}', encoding="utf-8")

    with pytest.raises(KnowledgeImportError, match="schema_version"):
        KnowledgeBase(tmp_path / "knowledge.db").import_json(bad_json)


@pytest.mark.parametrize(
    ("question", "query"),
    [
        ("星云随行杯是什么材质？", "星云随行杯 材质"),
        ("这个杯子的杯身是什么做的？", "星云随行杯 材质"),
    ],
)
def test_known_material_becomes_candidate(
    knowledge: KnowledgeBase, question: str, query: str
) -> None:
    client = FakeClient(
        [
            tool_response(query),
            candidate_response(
                "模拟资料显示，杯体为 304 不锈钢。",
                ["product:SIM-CUP-001"],
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer(question)

    assert decision.status is DecisionStatus.AUTO_REPLY_CANDIDATE
    assert decision.evidence_ids == ("product:SIM-CUP-001",)
    assert decision.tool_trace[0].query == query
    assert len(client.calls) == 2
    assert client.calls[0]["tool_choice"] == "required"
    assert client.calls[1]["response_format"] == {"type": "json_object"}


def test_token_usage_is_accumulated(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            with_usage(tool_response("星云随行杯 材质"), prompt=100, completion=20),
            with_usage(
                candidate_response(
                    "模拟资料显示，杯体为 304 不锈钢。",
                    ["product:SIM-CUP-001"],
                ),
                prompt=200,
                completion=30,
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.usage.prompt_tokens == 300
    assert decision.usage.completion_tokens == 50
    assert decision.usage.total_tokens == 350
    assert decision.usage.prompt_cache_miss_tokens == 300


def test_unknown_knowledge_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient([tool_response("不存在的月球材质")])

    decision = CustomerServiceAgent(knowledge, client).answer("杯子用了月球岩石吗？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "没有找到" in decision.review_reason
    assert len(client.calls) == 1


def test_product_hit_does_not_approve_missing_fact(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("星云随行杯 无线充电"),
            candidate_response(
                "现有资料未提及星云随行杯支持无线充电。",
                ["product:SIM-CUP-001"],
                review_reason="证据没有无线充电信息",
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer(
        "星云随行杯支持无线充电吗？"
    )

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "知识不足" in decision.review_reason


@pytest.mark.parametrize("question", ["我要退款", "商品坏了，你们怎么赔偿？"])
def test_high_risk_question_skips_model(
    knowledge: KnowledgeBase, question: str
) -> None:
    client = FakeClient([])

    decision = CustomerServiceAgent(knowledge, client).answer(question)

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "高风险" in decision.review_reason
    assert client.calls == []


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timeout"), DeepSeekAPIError("network")],
)
def test_api_failure_needs_review(
    knowledge: KnowledgeBase, failure: Exception
) -> None:
    decision = CustomerServiceAgent(knowledge, FakeClient([failure])).answer(
        "星云随行杯是什么材质？"
    )

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "失败或超时" in decision.review_reason


def test_missing_tool_call_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "直接回答"},
                    }
                ]
            }
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "没有调用" in decision.review_reason


def test_unsupported_promise_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("星云随行杯 材质"),
            candidate_response(
                "这款杯子提供终身质保。",
                ["product:SIM-CUP-001"],
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "未授权承诺" in decision.review_reason


def test_model_cannot_cite_unreturned_evidence(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("星云随行杯 材质"),
            candidate_response("杯体是不锈钢。", ["product:NOT-RETURNED"]),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "之外的证据" in decision.review_reason


def test_unapproved_knowledge_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("模拟资料使用边界"),
            candidate_response(
                "该资料只用于本地功能测试。",
                ["policy:SIM-POLICY-001"],
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("模拟资料可以怎么使用？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "尚未获准" in decision.review_reason


def test_bad_candidate_json_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("星云随行杯 材质"),
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "not-json"},
                    }
                ]
            },
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "格式无效" in decision.review_reason


def test_unsupported_number_needs_review(knowledge: KnowledgeBase) -> None:
    client = FakeClient(
        [
            tool_response("星云随行杯 材质"),
            candidate_response(
                "杯体为 304 不锈钢，容量是 900 毫升。",
                ["product:SIM-CUP-001"],
            ),
        ]
    )

    decision = CustomerServiceAgent(knowledge, client).answer("杯子是什么材质？")

    assert decision.status is DecisionStatus.NEED_REVIEW
    assert "未支持的数字" in decision.review_reason
