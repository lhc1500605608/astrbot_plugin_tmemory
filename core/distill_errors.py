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
    # ── 默认不降级路径的失败分类（TMEAAA-513）──
    NO_PROVIDER = "no_provider"             # 无法解析出可用的 LLM provider
    LLM_ERROR = "llm_error"                 # LLM 调用抛异常（code 记录异常类名/HTTP 状态码）
    UNPARSEABLE = "unparseable"             # LLM 返回无法解析或为空


class DistillErrorRecord:
    """单次 LLM 蒸馏调用的结构化错误记录。

    设计为轻量数据载体，可序列化存入 distill_history.errors JSON 字段。
    """

    __slots__ = ("category", "pipeline", "user_id", "message", "detail", "code")

    def __init__(
        self,
        category: DistillErrorCategory,
        pipeline: str,
        user_id: str = "",
        message: str = "",
        detail: str = "",
        code: str = "",
    ):
        self.category = category
        self.pipeline = pipeline   # "profile_extraction" | "consolidation" | "flat_distill"
        self.user_id = user_id
        self.message = message     # 人类可读摘要
        self.detail = detail       # 原始异常信息（截断）
        self.code = code           # llm_error 的细分码（异常类名 / HTTP 状态码）

    def category_label(self) -> str:
        """日志/展示用的分类标签；llm_error 附带 code，如 ``llm_error(RuntimeError)``。"""
        if self.category == DistillErrorCategory.LLM_ERROR and self.code:
            return f"llm_error({self.code})"
        return self.category.value

    def to_dict(self) -> Dict[str, str]:
        return {
            "category": self.category.value,
            "pipeline": self.pipeline,
            "user_id": self.user_id,
            "message": self.message,
            "detail": self.detail[:500],
            "code": self.code,
        }

    def log(self) -> None:
        """根据严重级别写入日志。"""
        if self.category in (
            DistillErrorCategory.PROVIDER_FAILURE,
            DistillErrorCategory.FALLBACK,
            DistillErrorCategory.NO_PROVIDER,
            DistillErrorCategory.LLM_ERROR,
            DistillErrorCategory.UNPARSEABLE,
        ):
            logger.warning(
                "[tmemory] distill error: category=%s pipeline=%s user=%s — %s | %s",
                self.category_label(), self.pipeline, self.user_id,
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


def exception_code(exception: Exception) -> str:
    """提取异常的细分码：优先 HTTP 状态码，其次异常类名。"""
    for attr in ("status_code", "status", "code"):
        val = getattr(exception, attr, None)
        if isinstance(val, int) and not isinstance(val, bool):
            return str(val)
    response = getattr(exception, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return str(status)
    return type(exception).__name__


def make_no_provider_record(
    pipeline: str,
    user_id: str = "",
    reason: str = "",
) -> DistillErrorRecord:
    """无法解析出可用 LLM provider 的错误记录（默认不降级）。"""
    return DistillErrorRecord(
        category=DistillErrorCategory.NO_PROVIDER,
        pipeline=pipeline,
        user_id=user_id,
        message=reason or "无法确定 LLM provider",
    )


def make_llm_error_record(
    exception: Exception,
    pipeline: str,
    user_id: str = "",
    reason: str = "",
) -> DistillErrorRecord:
    """LLM 调用抛异常的错误记录，携带细分 code。"""
    code = exception_code(exception)
    exc_name = type(exception).__name__
    return DistillErrorRecord(
        category=DistillErrorCategory.LLM_ERROR,
        pipeline=pipeline,
        user_id=user_id,
        message=reason or f"LLM 调用异常: {exc_name}",
        detail=f"{exc_name}: {str(exception)[:300]}",
        code=code,
    )


def make_unparseable_record(
    pipeline: str,
    user_id: str = "",
    reason: str = "",
) -> DistillErrorRecord:
    """LLM 返回无法解析或为空时的错误记录（默认不降级）。"""
    return DistillErrorRecord(
        category=DistillErrorCategory.UNPARSEABLE,
        pipeline=pipeline,
        user_id=user_id,
        message=reason or "LLM 返回无法解析或为空",
    )


def errors_to_json(errors: List[DistillErrorRecord]) -> List[Dict[str, str]]:
    """将错误记录列表序列化为 JSON 兼容列表。"""
    return [e.to_dict() for e in errors]


def errors_from_json(data: list) -> List[DistillErrorRecord]:
    """从 JSON 反序列化错误记录列表。

    兼容旧值：``category`` 可能带细分码（如 ``llm_error(RuntimeError)``）或
    历史遗留的未知分类，统一降级为枚举基类，不抛异常。
    """
    result = []
    for d in data:
        if isinstance(d, dict):
            raw_cat = str(d.get("category", "unknown"))
            code = str(d.get("code", "") or "")
            base_cat = raw_cat.split("(", 1)[0]
            if not code and "(" in raw_cat and raw_cat.endswith(")"):
                code = raw_cat[raw_cat.index("(") + 1 : -1]
            try:
                category = DistillErrorCategory(base_cat)
            except ValueError:
                category = DistillErrorCategory.UNKNOWN
            result.append(
                DistillErrorRecord(
                    category=category,
                    pipeline=str(d.get("pipeline", "")),
                    user_id=str(d.get("user_id", "")),
                    message=str(d.get("message", "")),
                    detail=str(d.get("detail", "")),
                    code=code,
                )
            )
    return result
