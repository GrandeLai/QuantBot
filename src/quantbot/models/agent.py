"""QuantAgent — unified LLM interface with middleware, system prompt, and singleton factory."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any


from quantbot.models._types import ChatMessage
from quantbot.models.middleware import LoggingMiddleware, Middleware
from quantbot.models.router import ModelRouter
from quantbot.ai.codex import CodexChatRouter

SYSTEM_PROMPT = """你是 QuantBot 的 AI 量化分析助手，具备以下能力：
- 分析股票/加密货币技术面（K线形态、技术指标）
- 解读基本面数据和财务指标
- 提供策略建议和风险分析
- 解释调用方提供的实际数据

请用简洁专业的中文回答。只依据提供的数据；没有数据时说明缺口。研究文字不构成交易指令。"""

_DEFAULT_AGENT: QuantAgent | None = None
_DEFAULT_AGENT_LOCK = threading.Lock()
_SHARED_CONFIG: dict | None = None


class QuantAgent:
    """Primary LLM interface for QuantBot.

    Wraps ModelRouter with:
    - System prompt injection
    - Middleware chain (before_request / after_response / on_error)
    - Extension stubs: rag_retriever, tool_executor, memory_store (Protocol-ready)
    """

    def __init__(
        self,
        router: ModelRouter | CodexChatRouter,
        middlewares: list[Middleware] | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._router = router
        self._middlewares: list[Middleware] = (
            middlewares if middlewares is not None else [LoggingMiddleware()]
        )
        self._system_prompt = system_prompt

    def _build_messages(self, history: list[ChatMessage]) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt}]
        msgs.extend({"role": m.role, "content": m.content} for m in history)
        return msgs

    async def chat(self, history: list[ChatMessage]) -> str:
        """Non-streaming chat with fallback and middleware hooks."""
        if not history:
            raise ValueError("消息列表不能为空")

        messages = self._build_messages(history)
        context: dict[str, Any] = {}

        for mw in self._middlewares:
            await mw.before_request(messages, context)

        try:
            result = await self._router.chat(messages)
        except Exception as exc:
            for mw in self._middlewares:
                await mw.on_error(exc, context)
            raise

        for mw in self._middlewares:
            result = await mw.after_response(result, context)

        return result

    async def stream(self, history: list[ChatMessage]) -> AsyncIterator[str]:
        """Streaming chat with fallback and error middleware hook.

        Note: `after_response` middleware is NOT called for streams since the full
        response is never assembled as a single string. Use `before_request` or
        `on_error` for streaming-compatible hooks.
        """
        if not history:
            raise ValueError("消息列表不能为空")

        messages = self._build_messages(history)
        context: dict[str, Any] = {}

        for mw in self._middlewares:
            await mw.before_request(messages, context)

        try:
            async for token in self._router.stream(messages):
                yield token
        except Exception as exc:
            for mw in self._middlewares:
                await mw.on_error(exc, context)
            yield f"[错误] {exc}"


def configure_default_agent(config: dict) -> None:
    """The workbench supplies the same configuration used by trading decisions."""
    from copy import deepcopy

    global _SHARED_CONFIG, _DEFAULT_AGENT
    with _DEFAULT_AGENT_LOCK:
        _SHARED_CONFIG = deepcopy(config)
        _DEFAULT_AGENT = None


def get_default_agent() -> QuantAgent:
    """Use the same explicit model configuration as the allocation gateway."""
    import os
    from quantbot.ai.config import load_config, validate_config
    from quantbot.models._types import ModelConfig

    global _DEFAULT_AGENT
    if _DEFAULT_AGENT is None:
        with _DEFAULT_AGENT_LOCK:
            if _DEFAULT_AGENT is None:
                path = Path(os.environ.get("QUANTBOT_CONFIG", "config/quantbot.toml"))
                config = _SHARED_CONFIG or load_config(path if path.is_file() else None)
                from copy import deepcopy

                config = deepcopy(config)
                llm = config["llm"]
                llm["model"] = os.environ.get("QUANTBOT_LLM_MODEL", llm["model"])
                llm["base_url"] = os.environ.get("QUANTBOT_LLM_BASE_URL", llm["base_url"])
                llm["backend"] = os.environ.get("QUANTBOT_LLM_BACKEND", llm.get("backend", "openai_compatible"))
                validate_config(config)
                if not llm["model"].strip():
                    raise ValueError("未配置大模型：填写统一 llm.model 或 QUANTBOT_LLM_MODEL")
                if llm["backend"] == "codex_cli":
                    _DEFAULT_AGENT = QuantAgent(router=CodexChatRouter(config))
                    return _DEFAULT_AGENT
                names = [llm["model"], *llm.get("fallback_models", [])]
                router = ModelRouter(
                    [
                        ModelConfig(
                            id="openai/" + n,
                            name=n,
                            provider="openai",
                            priority=i,
                            timeout=llm["timeout_seconds"],
                        )
                        for i, n in enumerate(names)
                    ],
                    key_loader=lambda _: os.environ.get(llm["api_key_env"]),
                )
                router.base_url = llm["base_url"]
                _DEFAULT_AGENT = QuantAgent(router=router)
    return _DEFAULT_AGENT
