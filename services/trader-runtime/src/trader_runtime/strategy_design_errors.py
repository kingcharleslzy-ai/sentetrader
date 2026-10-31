"""Small, credential-free error contract for persisted strategy design jobs."""
from __future__ import annotations

from pydantic import ValidationError


class DesignError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def design_failure(error: Exception, phase: str) -> dict:
    code, message, retryable, action = "design_failed", "策略设计未完成，请重试；若仍失败可查看任务编号排查。", True, "retry"
    value = str(error)
    if value.endswith("_api_key_required"):
        code, message, retryable, action = "model_auth_required", "请先配置当前模型的 API Key，再继续设计。", False, "settings"
    elif isinstance(error, ValidationError):
        code, message = "invalid_model_output", "模型返回的策略格式不完整，未保存为可运行策略；请重新生成。"
    elif isinstance(error, TimeoutError):
        code, message = "model_timeout", f"{phase}超时，尚未取得完整策略；原文已保留，可以重试。"
    elif isinstance(error, DesignError):
        code, message = error.code, value
        if code == "model_unavailable":
            retryable, action = False, "settings"
    elif value == "model_auth_failed":
        code, message, retryable, action = "model_auth_required", "模型认证失败，请检查当前模型的连接或密钥。", False, "settings"
    elif value == "model_rate_limited":
        code, message = "model_rate_limited", "模型额度不足或请求过于频繁，请稍后重试或检查模型额度。"
    elif value in {"model_request_failed", "model_stream_interrupted"}:
        code, message = "model_request_failed", f"模型未接受请求（{phase}），请检查模型配置后重试。"
    elif isinstance(error, (ConnectionError, OSError)):
        code, message = "model_disconnected", f"{phase}时模型连接中断，请重新连接后重试。"
    return {"code": code, "message": message, "phase": phase,
            "retryable": retryable, "action": action,
            "exceptionType": type(error).__name__}
