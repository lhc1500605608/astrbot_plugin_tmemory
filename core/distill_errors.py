"""结构化蒸馏错误分类模块。

为 profile_extraction、flat_distill、consolidation 三条链路的 LLM 调用
提供统一的错误分类与可观测性，替代裸 except Exception 的静默丢弃模式。

TMEAAA-331: 画像提取/蒸馏运行时硬化
"""

from __future__ import annotations

import enum
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("astrbot")


class DistillErrorCategory(str, enum.Enum):
    """蒸馏链路的统一错误分类。"""

    PROVIDER_FAILURE = "provider_failure"   # LLM provider 调用失败（网络、超时、认证等）
    PARSE_FAILURE = "parse_failure"         # LLM 返回了内容但无法解析为合法 JSON
    EMPTY_RESULT = "empty_result"           # LLM 返回解析成功但 memories/profile_items 为空
    FALLBACK = "fallback"                   # 因无可用 provider 触发的规则回退
    TIMEOUT = "timeout"                     # 异步调用超时
    VALIDATION_FAILURE = "validation_failure"  # 解析成功但所有条目被校验器裁剪
    UNKNOWN = "unknown"                     # 未分类异常


class DistillErrorRecord:
    """单次 LLM 蒸馏调用的结构化错误记录。

    设计为轻量数据载体，可序列化存入 distill_history.errors JSON 字段。
    """

    __slots__ = ("category", "pipeline", "user_id", "message", "detail")

    def __init__(
        self,
        category: DistillErrorCategory,
        pipeline: str,
        user_id: str = "",
        message: str = "",
        detail: str = "",
    ):
        self.category = category
        self.pipeline = pipeline   # "profile_extraction" | "consolidation" | "flat_distill"
        self.user_id = user_id
        self.message = message     # 人类可读摘要
        self.detail = detail       # 原始异常信息（截断）

    def to_dict(self) -> Dict[str, str]:
        return {
            "category": self.category.value,
            "pipeline": self.pipeline,
            "user_id": self.user_id,
            "message": self.message,
            "detail": self.detail[:500],
        }

    def log(self) -> None:
        """根据严重级别写入日志。"""
        if self.category in (DistillErrorCategory.PROVIDER_FAILURE, DistillErrorCategory.FALLBACK):
            logger.warning(
                "[tmemory] distill error: category=%s pipeline=%s user=%s — %s | %s",
                self.category.value, self.pipeline, self.user_id,
                self.message, self.detail,
            )
        elif self.category == DistillErrorCategory.PARSE_FAILURE:
            logger.warning(
                "[tmemory] distill parse failure: pipeline=%s user=%s — %s",
                self.pipeline, self.user_id, self.message,
            )
        elif self.category == DistillErrorCategory.TIMEOUT:
            logger.warning(
                "[tmemory] distill timeout: pipeline=%s user=%s — %s",
                self.pipeline, self.user_id, self.message,
            )
        else:
            logger.info(
                "[tmemory] distill skip: category=%s pipeline=%s user=%s — %s",
                self.category.value, self.pipeline, self.user_id, self.message,
            )


def classify_llm_error(
    exception: Exception,
    pipeline: str,
    user_id: str = "",
    context_message: str = "",
) -> DistillErrorRecord:
    """根据异常类型自动分类为结构化错误。

    用于替代现有的裸 except Exception: logger.warning(...) 模式。
    """
    import asyncio

    exc_name = type(exception).__name__
    exc_msg = str(exception)[:300]

    if isinstance(exception, asyncio.TimeoutError):
        return DistillErrorRecord(
            category=DistillErrorCategory.TIMEOUT,
            pipeline=pipeline,
            user_id=user_id,
            message=context_message or "异步调用超时",
            detail=f"{exc_name}: {exc_msg}",
        )

    # 网络/连接类异常
    if exc_name in (
        "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
        "TimeoutError", "HTTPError", "ClientError", "ServerError",
        "APIConnectionError", "APITimeoutError", "AuthenticationError",
    ):
        return DistillErrorRecord(
            category=DistillErrorCategory.PROVIDER_FAILURE,
            pipeline=pipeline,
            user_id=user_id,
            message=context_message or f"LLM provider 调用失败: {exc_name}",
            detail=f"{exc_name}: {exc_msg}",
        )

    # JSON 解析异常
    if exc_name in ("JSONDecodeError", "ValueError") and (
        "json" in exc_msg.lower() or "parse" in exc_msg.lower() or "expect" in exc_msg.lower()
    ):
        return DistillErrorRecord(
            category=DistillErrorCategory.PARSE_FAILURE,
            pipeline=pipeline,
            user_id=user_id,
            message=context_message or "LLM 输出解析失败",
            detail=f"{exc_name}: {exc_msg}",
        )

    # 通用异常归为 provider_failure（LLM 调用策略是重试还是不重试？默认不重试）
    return DistillErrorRecord(
        category=DistillErrorCategory.PROVIDER_FAILURE,
        pipeline=pipeline,
        user_id=user_id,
        message=context_message or f"LLM 调用异常: {exc_name}",
        detail=f"{exc_name}: {exc_msg}",
    )


def make_empty_result_record(
    pipeline: str,
    user_id: str = "",
) -> DistillErrorRecord:
    """创建空结果的标准化错误记录。"""
    return DistillErrorRecord(
        category=DistillErrorCategory.EMPTY_RESULT,
        pipeline=pipeline,
        user_id=user_id,
        message="LLM 返回空结果",
    )


def make_fallback_record(
    pipeline: str,
    user_id: str = "",
    reason: str = "",
) -> DistillErrorRecord:
    """创建因无 provider 而使用规则回退的标准化错误记录。"""
    return DistillErrorRecord(
        category=DistillErrorCategory.FALLBACK,
        pipeline=pipeline,
        user_id=user_id,
        message=reason or "无可用的 LLM provider，使用规则回退",
    )


def make_validation_failure_record(
    pipeline: str,
    user_id: str = "",
    reason: str = "",
) -> DistillErrorRecord:
    """创建校验失败的错误记录。"""
    return DistillErrorRecord(
        category=DistillErrorCategory.VALIDATION_FAILURE,
        pipeline=pipeline,
        user_id=user_id,
        message=reason or "蒸馏输出全部未通过校验",
    )


def errors_to_json(errors: List[DistillErrorRecord]) -> List[Dict[str, str]]:
    """将错误记录列表序列化为 JSON 兼容列表。"""
    return [e.to_dict() for e in errors]


def errors_from_json(data: list) -> List[DistillErrorRecord]:
    """从 JSON 反序列化错误记录列表。"""
    result = []
    for d in data:
        if isinstance(d, dict):
            cat = d.get("category", "unknown")
            try:
                category = DistillErrorCategory(cat)
            except ValueError:
                category = DistillErrorCategory.UNKNOWN
            result.append(
                DistillErrorRecord(
                    category=category,
                    pipeline=str(d.get("pipeline", "")),
                    user_id=str(d.get("user_id", "")),
                    message=str(d.get("message", "")),
                    detail=str(d.get("detail", "")),
                )
            )
    return result
