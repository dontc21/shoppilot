"""DeepSeek Tool Calling 与独立回复资格判断。"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlparse

from .knowledge import KnowledgeBase, KnowledgeEvidence


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "查询本地商品、FAQ 和售后规则。回答商品事实前必须调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "商品名称加待查询属性，例如：星云随行杯 材质",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回的证据数，范围 1 到 5",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "required": ["query", "limit"],
            "additionalProperties": False,
        },
    },
}


class DecisionStatus(StrEnum):
    AUTO_REPLY_CANDIDATE = "AUTO_REPLY_CANDIDATE"
    NEED_REVIEW = "NEED_REVIEW"


@dataclass(frozen=True)
class ToolTrace:
    name: str
    query: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            prompt_cache_hit_tokens=(
                self.prompt_cache_hit_tokens + other.prompt_cache_hit_tokens
            ),
            prompt_cache_miss_tokens=(
                self.prompt_cache_miss_tokens + other.prompt_cache_miss_tokens
            ),
        )


@dataclass(frozen=True)
class AgentDecision:
    candidate_reply: str
    status: DecisionStatus
    evidence_ids: tuple[str, ...]
    review_reason: str
    tool_trace: tuple[ToolTrace, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class ChatModelClient(Protocol):
    """便于用本地假模型测试完整 Agent 流程。"""

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...


class DeepSeekAPIError(RuntimeError):
    """DeepSeek 请求或响应无法安全使用。"""


class DeepSeekChatClient:
    """最小 DeepSeek REST 客户端，不记录密钥和请求正文。"""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 20,
        max_tokens: int = 500,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY 未配置")
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.deepseek.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DEEPSEEK_BASE_URL 必须是 DeepSeek 官方 HTTPS 地址")
        if not model.strip():
            raise ValueError("DEEPSEEK_MODEL 不能为空")
        self._api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 60.0))
        self.max_tokens = max(64, min(int(max_tokens), 800))

    @classmethod
    def from_environment(cls) -> "DeepSeekChatClient":
        return cls(
            os.getenv("DEEPSEEK_API_KEY", ""),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20")),
            max_tokens=int(os.getenv("DEEPSEEK_MAX_TOKENS", "500")),
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "thinking": {"type": "disabled"},
        }
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_bytes = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise DeepSeekAPIError(f"DeepSeek HTTP 状态异常：{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise DeepSeekAPIError("DeepSeek 网络请求失败或超时") from exc

        if len(response_bytes) > 2_000_000:
            raise DeepSeekAPIError("DeepSeek 响应超过大小限制")
        try:
            result = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeepSeekAPIError("DeepSeek 返回的不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise DeepSeekAPIError("DeepSeek 响应结构错误")
        return result


class DecisionRules:
    """模型之外的确定性门禁；这里只授予候选资格，不执行发送。"""

    HIGH_RISK_TERMS = (
        "退款",
        "退货",
        "换货",
        "投诉",
        "举报",
        "赔偿",
        "补偿",
        "订单变更",
        "修改订单",
        "修改地址",
        "改地址",
        "取消订单",
        "发票",
        "催发货",
        "物流异常",
        "质保",
    )
    UNSAFE_REPLY_TERMS = (
        "无条件退款",
        "无条件退货",
        "赔偿",
        "补偿",
        "终身质保",
        "免费换新",
        "保证退款",
        "保证赔付",
        "一定退款",
        "百分百",
    )
    INSUFFICIENT_KNOWLEDGE_TERMS = (
        "未提及",
        "没有提及",
        "暂无资料",
        "没有资料",
        "无法确认",
        "不确定",
        "未说明",
        "没有说明",
        "未找到",
    )

    @classmethod
    def high_risk_reason(cls, question: str) -> str | None:
        matched = next((term for term in cls.HIGH_RISK_TERMS if term in question), None)
        if matched:
            return f"问题涉及高风险事项“{matched}”，必须人工审核。"
        return None

    @classmethod
    def evaluate(
        cls,
        *,
        question: str,
        candidate_reply: str,
        cited_ids: list[str],
        evidence: list[KnowledgeEvidence],
    ) -> tuple[DecisionStatus, str]:
        risk_reason = cls.high_risk_reason(question)
        if risk_reason:
            return DecisionStatus.NEED_REVIEW, risk_reason
        if not candidate_reply or len(candidate_reply) > 300:
            return DecisionStatus.NEED_REVIEW, "候选回复为空或超过 300 字。"
        if not cited_ids:
            return DecisionStatus.NEED_REVIEW, "模型没有引用知识证据。"

        evidence_by_id = {item.evidence_id: item for item in evidence}
        if len(cited_ids) != len(set(cited_ids)):
            return DecisionStatus.NEED_REVIEW, "模型返回了重复的知识证据 ID。"
        if any(item_id not in evidence_by_id for item_id in cited_ids):
            return DecisionStatus.NEED_REVIEW, "模型引用了本次工具结果之外的证据。"
        if any(not evidence_by_id[item_id].approved_for_reply for item_id in cited_ids):
            return DecisionStatus.NEED_REVIEW, "引用的知识尚未获准生成候选回复。"

        insufficient = next(
            (
                term
                for term in cls.INSUFFICIENT_KNOWLEDGE_TERMS
                if term in candidate_reply
            ),
            None,
        )
        if insufficient:
            return (
                DecisionStatus.NEED_REVIEW,
                f"候选回复表明知识不足（“{insufficient}”），必须人工审核。",
            )

        unsafe = next((term for term in cls.UNSAFE_REPLY_TERMS if term in candidate_reply), None)
        if unsafe:
            return DecisionStatus.NEED_REVIEW, f"候选回复包含未授权承诺“{unsafe}”。"

        cited_text = " ".join(evidence_by_id[item_id].content for item_id in cited_ids)
        reply_numbers = set(re.findall(r"\d+(?:\.\d+)?", candidate_reply))
        unsupported_numbers = sorted(number for number in reply_numbers if number not in cited_text)
        if unsupported_numbers:
            return DecisionStatus.NEED_REVIEW, "候选回复包含知识证据未支持的数字。"
        return DecisionStatus.AUTO_REPLY_CANDIDATE, "通过独立规则校验；仅为候选回复，禁止自动发送。"


class CustomerServiceAgent:
    """执行一次知识工具调用，再让模型基于结果生成候选回复。"""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        client: ChatModelClient,
        *,
        max_tool_calls: int = 1,
    ) -> None:
        self.knowledge = knowledge
        self.client = client
        self.max_tool_calls = max(1, min(max_tool_calls, 1))

    def answer(self, question: str) -> AgentDecision:
        clean_question = question.strip()
        if not clean_question:
            return self._review("问题为空。")
        if len(clean_question) > 500:
            return self._review("问题超过 500 字，未发送到外部模型。")
        risk_reason = DecisionRules.high_risk_reason(clean_question)
        if risk_reason:
            return self._review(risk_reason)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "你是商品知识检索助手。第一步必须调用 search_knowledge，"
                    "不要猜测资料或请求其他工具。收到工具结果后，只能依据结果"
                    "输出 JSON 对象，字段必须是 candidate_reply、evidence_ids、"
                    "review_reason。evidence_ids 只能引用工具结果中的 evidence_id；"
                    "不要承诺退款、赔偿、质保或其他未明确记载的权益。"
                    "如果证据没有明确回答用户问题，候选回复必须明确写“无法确认”。"
                ),
            },
            {"role": "user", "content": clean_question},
        ]
        usage = TokenUsage()
        try:
            first = self.client.complete(
                messages,
                tools=[SEARCH_TOOL],
                tool_choice="required",
            )
            usage = usage.plus(self._extract_usage(first))
            assistant_message = self._extract_message(first)
            tool_calls = assistant_message.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                return self._review("模型没有调用知识库工具。", usage=usage)
            if len(tool_calls) > self.max_tool_calls:
                return self._review("模型工具调用次数超过限制。", usage=usage)

            tool_call = tool_calls[0]
            call_id, query, limit = self._parse_tool_call(tool_call)
            evidence = self.knowledge.search_knowledge(query, limit)
            trace = ToolTrace(
                name="search_knowledge",
                query=query,
                evidence_ids=tuple(item.evidence_id for item in evidence),
            )
            if not evidence:
                return self._review(
                    "本地知识库没有找到可靠资料。",
                    trace=(trace,),
                    usage=usage,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        [item.to_dict() for item in evidence],
                        ensure_ascii=False,
                    ),
                }
            )
            second = self.client.complete(
                messages,
                response_format={"type": "json_object"},
            )
            usage = usage.plus(self._extract_usage(second))
            result = self._parse_candidate(self._extract_message(second))
            status, reason = DecisionRules.evaluate(
                question=clean_question,
                candidate_reply=result["candidate_reply"],
                cited_ids=result["evidence_ids"],
                evidence=evidence,
            )
            if status is DecisionStatus.NEED_REVIEW and result["review_reason"]:
                reason = f"{reason} 模型备注：{result['review_reason'][:120]}"
            return AgentDecision(
                candidate_reply=result["candidate_reply"],
                status=status,
                evidence_ids=tuple(result["evidence_ids"]),
                review_reason=reason,
                tool_trace=(trace,),
                usage=usage,
            )
        except (DeepSeekAPIError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            return self._review(self._safe_error_reason(exc), usage=usage)

    @staticmethod
    def _extract_usage(response: dict[str, Any]) -> TokenUsage:
        raw = response.get("usage")
        if not isinstance(raw, dict):
            return TokenUsage()

        def non_negative_int(name: str) -> int:
            value = raw.get(name, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            return 0

        return TokenUsage(
            prompt_tokens=non_negative_int("prompt_tokens"),
            completion_tokens=non_negative_int("completion_tokens"),
            total_tokens=non_negative_int("total_tokens"),
            prompt_cache_hit_tokens=non_negative_int("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=non_negative_int("prompt_cache_miss_tokens"),
        )

    @staticmethod
    def _extract_message(response: dict[str, Any]) -> dict[str, Any]:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("模型响应 choices 结构错误")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") == "length":
            raise ValueError("模型响应不完整")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("模型响应缺少 message")
        return message

    @staticmethod
    def _parse_tool_call(tool_call: Any) -> tuple[str, str, int]:
        if not isinstance(tool_call, dict) or not isinstance(tool_call.get("id"), str):
            raise ValueError("工具调用缺少 ID")
        function = tool_call.get("function")
        if not isinstance(function, dict) or function.get("name") != "search_knowledge":
            raise ValueError("模型请求了未授权工具")
        arguments_raw = function.get("arguments")
        if not isinstance(arguments_raw, str):
            raise ValueError("工具参数不是 JSON 字符串")
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("工具参数不是有效 JSON") from exc
        if not isinstance(arguments, dict) or set(arguments) != {"query", "limit"}:
            raise ValueError("工具参数字段不符合白名单")
        query = arguments.get("query")
        limit = arguments.get("limit")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("工具查询词无效")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise ValueError("工具 limit 必须在 1 到 5 之间")
        return tool_call["id"], query.strip(), limit

    @staticmethod
    def _parse_candidate(message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("模型候选回复为空")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("模型候选回复不是有效 JSON") from exc
        if not isinstance(result, dict) or set(result) != {
            "candidate_reply",
            "evidence_ids",
            "review_reason",
        }:
            raise ValueError("模型候选回复字段不符合约定")
        if not isinstance(result["candidate_reply"], str):
            raise ValueError("candidate_reply 类型错误")
        if not isinstance(result["review_reason"], str):
            raise ValueError("review_reason 类型错误")
        evidence_ids = result["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) for item in evidence_ids
        ):
            raise ValueError("evidence_ids 类型错误")
        return result

    @staticmethod
    def _safe_error_reason(exc: Exception) -> str:
        if isinstance(exc, (DeepSeekAPIError, TimeoutError)):
            return "模型请求失败或超时，已安全转人工。"
        return f"模型或工具返回格式无效，已安全转人工：{str(exc)[:100]}"

    @staticmethod
    def _review(
        reason: str,
        *,
        trace: tuple[ToolTrace, ...] = (),
        usage: TokenUsage | None = None,
    ) -> AgentDecision:
        return AgentDecision(
            candidate_reply="",
            status=DecisionStatus.NEED_REVIEW,
            evidence_ids=(),
            review_reason=reason,
            tool_trace=trace,
            usage=usage or TokenUsage(),
        )
