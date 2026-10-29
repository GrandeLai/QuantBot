"""Tests for ModelRouter — retry, fallback, and circuit breaker integration."""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from quantbot.models._types import AllModelsFailedError, ModelConfig
from quantbot.models.router import ModelRouter


def _cfg(model_id: str, priority: int = 1, max_retries: int = 1) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        name=model_id,
        provider="openai",
        priority=priority,
        timeout=5.0,
        max_retries=max_retries,
        retry_base_delay=0.0,  # no sleep in tests
        retry_max_delay=30.0,
        circuit_breaker_threshold=3,
        circuit_breaker_recovery=9999.0,
    )


def _key_loader(provider: str) -> str | None:
    return "test-key"


def _mock_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


class TestModelRouterChat:
    async def test_uses_highest_priority_model_first(self) -> None:
        configs = [_cfg("m2", priority=2), _cfg("m1", priority=1)]
        called: list[str] = []

        async def mock_completion(**kwargs: object) -> MagicMock:
            called.append(str(kwargs["model"]))
            return _mock_response("ok")

        with patch("litellm.acompletion", side_effect=mock_completion):
            router = ModelRouter(configs, key_loader=_key_loader)
            await router.chat([{"role": "user", "content": "hi"}])

        assert called[0] == "m1"

    async def test_falls_back_when_primary_fails(self) -> None:
        configs = [_cfg("m1", priority=1, max_retries=1), _cfg("m2", priority=2)]

        async def mock_completion(**kwargs: object) -> MagicMock:
            if kwargs["model"] == "m1":
                raise RuntimeError("rate limited")
            return _mock_response("from m2")

        with patch("litellm.acompletion", side_effect=mock_completion):
            router = ModelRouter(configs, key_loader=_key_loader)
            result = await router.chat([{"role": "user", "content": "hi"}])

        assert result == "from m2"

    async def test_retries_before_fallback(self) -> None:
        configs = [_cfg("m1", priority=1, max_retries=3), _cfg("m2", priority=2)]
        call_count: dict[str, int] = {}

        async def mock_completion(**kwargs: object) -> MagicMock:
            model = str(kwargs["model"])
            call_count[model] = call_count.get(model, 0) + 1
            if model == "m1":
                raise RuntimeError("error")
            return _mock_response("ok")

        with patch("litellm.acompletion", side_effect=mock_completion):
            router = ModelRouter(configs, key_loader=_key_loader)
            await router.chat([{"role": "user", "content": "hi"}])

        assert call_count["m1"] == 3  # 3 retries before fallback

    async def test_raises_all_models_failed_when_exhausted(self) -> None:
        configs = [_cfg("m1", max_retries=1)]

        async def mock_completion(**kwargs: object) -> MagicMock:
            raise RuntimeError("always fails")

        with patch("litellm.acompletion", side_effect=mock_completion):
            router = ModelRouter(configs, key_loader=_key_loader)
            with pytest.raises(AllModelsFailedError) as exc_info:
                await router.chat([{"role": "user", "content": "hi"}])
        assert "m1" in exc_info.value.errors

    async def test_skips_open_circuit(self) -> None:
        configs = [_cfg("m1", priority=1, max_retries=1), _cfg("m2", priority=2)]
        called: list[str] = []

        async def mock_completion(**kwargs: object) -> MagicMock:
            called.append(str(kwargs["model"]))
            return _mock_response("ok")

        with patch("litellm.acompletion", side_effect=mock_completion):
            router = ModelRouter(configs, key_loader=_key_loader)
            # Force m1 circuit open (threshold=3)
            for _ in range(3):
                await router._circuit_breakers["m1"].record_failure()

            await router.chat([{"role": "user", "content": "hi"}])

        assert "m1" not in called
        assert "m2" in called

    async def test_returns_correct_content(self) -> None:
        configs = [_cfg("m1")]

        with patch("litellm.acompletion", return_value=_mock_response("hello world")):
            router = ModelRouter(configs, key_loader=_key_loader)
            result = await router.chat([{"role": "user", "content": "hi"}])

        assert result == "hello world"


class TestModelRouterStream:
    async def test_stream_yields_tokens_from_primary(self) -> None:
        configs = [_cfg("m1")]

        async def fake_gen() -> AsyncIterator[str]:
            for t in ["hello", " ", "world"]:
                yield t

        with patch(
            "quantbot.models.provider.LiteLLMProvider._stream_gen",
            return_value=fake_gen(),
        ):
            router = ModelRouter(configs, key_loader=_key_loader)
            tokens = []
            async for token in router.stream([{"role": "user", "content": "hi"}]):
                tokens.append(token)

        assert tokens == ["hello", " ", "world"]

    async def test_stream_falls_back_on_connection_error(self) -> None:
        configs = [_cfg("m1", priority=1, max_retries=1), _cfg("m2", priority=2)]
        called: list[str] = []

        async def good_gen() -> AsyncIterator[str]:
            yield "fallback token"

        def patched_stream(
            self_obj: object,
            messages: object,
            *,
            timeout: float,
            **kw: object,
        ) -> AsyncIterator[str]:
            model = getattr(self_obj, "_model", "?")
            called.append(model)
            if model == "m1":
                raise RuntimeError("connection refused")
            return good_gen()

        with patch("quantbot.models.provider.LiteLLMProvider.stream", patched_stream):
            router = ModelRouter(configs, key_loader=_key_loader)
            tokens = []
            async for token in router.stream([{"role": "user", "content": "hi"}]):
                tokens.append(token)

        assert "m1" in called
        assert "m2" in called
        assert tokens == ["fallback token"]
