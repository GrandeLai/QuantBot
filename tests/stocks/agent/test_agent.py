"""Tests for QuantAgent — middleware hooks, chat/stream delegation, singleton."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from quantbot.models._types import AllModelsFailedError, ChatMessage, ModelConfig
from quantbot.models.agent import QuantAgent, get_default_agent
from quantbot.models.router import ModelRouter


def _make_router_with_response(content: str) -> ModelRouter:
    cfg = ModelConfig(
        id="gpt-4o",
        name="GPT-4o",
        provider="openai",
        priority=1,
        timeout=5.0,
        max_retries=1,
        retry_base_delay=0.0,
        retry_max_delay=30.0,
        circuit_breaker_threshold=3,
        circuit_breaker_recovery=9999.0,
    )
    router = ModelRouter([cfg], key_loader=lambda p: "test-key")
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=content)
    router._make_provider = MagicMock(return_value=provider)
    return router


class TestQuantAgentChat:
    async def test_chat_returns_string(self) -> None:
        router = _make_router_with_response("hello from agent")
        agent = QuantAgent(router=router, middlewares=[])
        result = await agent.chat([ChatMessage(role="user", content="hi")])
        assert result == "hello from agent"

    async def test_chat_prepends_system_prompt(self) -> None:
        router = _make_router_with_response("ok")
        captured: list[list[dict[str, object]]] = []

        original_chat = router.chat

        async def capture_chat(messages: list[dict[str, object]]) -> Any:
            captured.append(messages)
            return await original_chat(messages)

        router.chat = capture_chat  # type: ignore[method-assign]
        agent = QuantAgent(router=router, middlewares=[])
        await agent.chat([ChatMessage(role="user", content="hi")])

        assert len(captured) == 1
        assert captured[0][0]["role"] == "system"

    async def test_chat_raises_on_empty_history(self) -> None:
        router = _make_router_with_response("ok")
        agent = QuantAgent(router=router, middlewares=[])
        with pytest.raises(ValueError, match="消息列表"):
            await agent.chat([])

    async def test_middleware_before_after_called(self) -> None:
        router = _make_router_with_response("resp")
        before_called: list[bool] = []
        after_called: list[str] = []

        class TrackingMiddleware:
            async def before_request(
                self, messages: list[dict[str, object]], context: dict[str, object]
            ) -> None:
                before_called.append(True)

            async def after_response(
                self, response: str, context: dict[str, object]
            ) -> str:
                after_called.append(response)
                return response

            async def on_error(
                self, error: Exception, context: dict[str, object]
            ) -> None:
                pass

        agent = QuantAgent(router=router, middlewares=[TrackingMiddleware()])
        await agent.chat([ChatMessage(role="user", content="hi")])

        assert before_called == [True]
        assert after_called == ["resp"]

    async def test_on_error_called_when_router_fails(self) -> None:
        cfg = ModelConfig(
            id="bad", name="bad", provider="openai", priority=1,
            timeout=5.0, max_retries=1, retry_base_delay=0.0, retry_max_delay=30.0,
            circuit_breaker_threshold=3, circuit_breaker_recovery=9999.0,
        )
        router = ModelRouter([cfg], key_loader=lambda p: "k")
        provider = MagicMock()
        provider.chat = AsyncMock(side_effect=RuntimeError("boom"))
        router._make_provider = MagicMock(return_value=provider)

        error_caught: list[Exception] = []

        class ErrorMiddleware:
            async def before_request(self, *_: object, **__: object) -> None:
                pass

            async def after_response(self, response: str, *_: object, **__: object) -> str:
                return response

            async def on_error(self, error: Exception, *_: object, **__: object) -> None:
                error_caught.append(error)

        agent = QuantAgent(router=router, middlewares=[ErrorMiddleware()])
        with pytest.raises(AllModelsFailedError):
            await agent.chat([ChatMessage(role="user", content="hi")])

        assert len(error_caught) == 1


class TestQuantAgentStream:
    async def test_stream_yields_tokens(self) -> None:
        async def fake_stream(messages: list[dict[str, object]]) -> AsyncIterator[str]:
            for t in ["a", "b", "c"]:
                yield t

        cfg = ModelConfig(
            id="m1", name="m1", provider="openai", priority=1,
            timeout=5.0, max_retries=1, retry_base_delay=0.0, retry_max_delay=30.0,
            circuit_breaker_threshold=3, circuit_breaker_recovery=9999.0,
        )
        router = ModelRouter([cfg], key_loader=lambda p: "k")
        router.stream = fake_stream  # type: ignore[method-assign]

        agent = QuantAgent(router=router, middlewares=[])
        tokens = []
        async for t in agent.stream([ChatMessage(role="user", content="hi")]):
            tokens.append(t)

        assert tokens == ["a", "b", "c"]

    async def test_stream_raises_on_empty_history(self) -> None:
        router = _make_router_with_response("ok")
        agent = QuantAgent(router=router, middlewares=[])
        with pytest.raises(ValueError, match="消息列表"):
            async for _ in agent.stream([]):
                pass

    async def test_stream_calls_before_request_middleware(self) -> None:
        async def fake_stream(messages: list[dict[str, object]]) -> AsyncIterator[str]:
            yield "token"

        cfg = ModelConfig(
            id="m1", name="m1", provider="openai", priority=1,
            timeout=5.0, max_retries=1, retry_base_delay=0.0, retry_max_delay=30.0,
            circuit_breaker_threshold=3, circuit_breaker_recovery=9999.0,
        )
        router = ModelRouter([cfg], key_loader=lambda p: "k")
        router.stream = fake_stream  # type: ignore[method-assign]

        before_called: list[bool] = []

        class TrackMiddleware:
            async def before_request(
                self, messages: list[dict[str, object]], context: dict[str, object]
            ) -> None:
                before_called.append(True)

            async def after_response(self, response: str, context: dict[str, object]) -> str:
                return response

            async def on_error(self, error: Exception, context: dict[str, object]) -> None:
                pass

        agent = QuantAgent(router=router, middlewares=[TrackMiddleware()])
        async for _ in agent.stream([ChatMessage(role="user", content="hi")]):
            pass

        assert before_called == [True]

    async def test_stream_yields_error_token_and_calls_on_error_on_failure(self) -> None:
        cfg = ModelConfig(
            id="m1", name="m1", provider="openai", priority=1,
            timeout=5.0, max_retries=1, retry_base_delay=0.0, retry_max_delay=30.0,
            circuit_breaker_threshold=3, circuit_breaker_recovery=9999.0,
        )
        router = ModelRouter([cfg], key_loader=lambda p: "k")

        async def failing_stream(messages: list[dict[str, object]]) -> AsyncIterator[str]:
            raise AllModelsFailedError({"m1": "all failed"})
            yield  # make it an async generator

        router.stream = failing_stream  # type: ignore[method-assign]

        on_error_called: list[Exception] = []

        class ErrorMiddleware:
            async def before_request(self, *_: object, **__: object) -> None:
                pass

            async def after_response(self, response: str, *_: object, **__: object) -> str:
                return response

            async def on_error(self, error: Exception, *_: object, **__: object) -> None:
                on_error_called.append(error)

        agent = QuantAgent(router=router, middlewares=[ErrorMiddleware()])
        tokens = []
        async for t in agent.stream([ChatMessage(role="user", content="hi")]):
            tokens.append(t)

        assert len(on_error_called) == 1
        assert isinstance(on_error_called[0], AllModelsFailedError)
        assert any("[错误]" in t for t in tokens)


class TestGetDefaultAgent:
    def test_returns_quant_agent_instance(self, monkeypatch) -> None:
        monkeypatch.setenv("QUANTBOT_LLM_MODEL", "fixture-model")
        import quantbot.models.agent as agent_module
        agent_module._DEFAULT_AGENT = None  # reset singleton

        agent = get_default_agent()
        assert isinstance(agent, QuantAgent)

    def test_returns_same_instance_on_second_call(self, monkeypatch) -> None:
        monkeypatch.setenv("QUANTBOT_LLM_MODEL", "fixture-model")
        import quantbot.models.agent as agent_module
        agent_module._DEFAULT_AGENT = None  # reset singleton

        a1 = get_default_agent()
        a2 = get_default_agent()
        assert a1 is a2
