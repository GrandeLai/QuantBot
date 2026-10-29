"""ModelRouter — priority-ordered fallback with per-model retry and circuit breaker."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, TYPE_CHECKING

from loguru import logger

from quantbot.models._types import (
    AllModelsFailedError,
    CircuitState,
    ModelConfig,
    ModelUnavailableError,
)
from quantbot.models.circuit_breaker import CircuitBreaker


if TYPE_CHECKING:
    from quantbot.models.provider import LiteLLMProvider


class ModelRouter:
    """Routes LLM requests across a priority-ordered model list.

    For each request:
      1. Iterate models by ascending priority, skip models with open circuits.
      2. Per model: retry up to max_retries with exponential backoff.
      3. On success: record_success, return result.
      4. On all retries exhausted: record_failure, try next model.
      5. If all models fail: raise AllModelsFailedError.
    """

    def __init__(
        self,
        configs: list[ModelConfig],
        key_loader: Callable[[str], str | None],
    ) -> None:
        self._configs = sorted(configs, key=lambda m: m.priority)
        self._key_loader = key_loader
        self._circuit_breakers: dict[str, CircuitBreaker] = {
            cfg.id: CircuitBreaker(
                cfg.id,
                threshold=cfg.circuit_breaker_threshold,
                recovery_seconds=cfg.circuit_breaker_recovery,
            )
            for cfg in configs
        }

    def _make_provider(self, cfg: ModelConfig) -> LiteLLMProvider:
        from quantbot.models.provider import LiteLLMProvider
        api_key = self._key_loader(cfg.provider) or ""
        provider = LiteLLMProvider(cfg.id, api_key)
        if getattr(self, "base_url", None):
            provider.base_url = self.base_url
        return provider

    async def invoke(self, callback):
        """Reuse the model registry and circuit state for structured allocation.

        Allocation validation errors terminate the request; only provider failures
        are eligible for fallback. There are no automatic retries for decisions.
        """
        from quantbot.ai.transport import ProviderError
        errors = {}
        for cfg in self._configs:
            cb = self._circuit_breakers[cfg.id]
            if not cb.is_available():
                errors[cfg.id] = "circuit open"
                continue
            try:
                result = await asyncio.to_thread(callback, cfg)
            except ProviderError:
                await cb.record_failure()
                errors[cfg.id] = "provider unavailable"
                continue
            await cb.record_success()
            return result
        raise ProviderError("全部配置模型不可用；未生成新订单")

    async def _try_chat_with_retry(
        self,
        cfg: ModelConfig,
        messages: list[dict[str, Any]],
    ) -> str:
        """Attempt chat for one model with exponential-backoff retries.

        Raises ModelUnavailableError if all retries exhausted or circuit open.
        """
        cb = self._circuit_breakers[cfg.id]
        if not cb.is_available():
            raise ModelUnavailableError(cfg.id, "circuit open")

        # In HALF_OPEN state, only one probe attempt is allowed
        max_attempts = 1 if cb.state == CircuitState.HALF_OPEN else cfg.max_retries

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            if attempt > 0:
                delay = min(
                    cfg.retry_base_delay * (2 ** (attempt - 1)),
                    cfg.retry_max_delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                provider = self._make_provider(cfg)
                result = await provider.chat(messages, timeout=cfg.timeout)
                await cb.record_success()
                return result
            except Exception as exc:
                last_error = exc
                logger.debug(
                    f"[Router] {cfg.id} attempt {attempt + 1}/{max_attempts} failed: {exc}"
                )

        await cb.record_failure()
        raise ModelUnavailableError(cfg.id, str(last_error)) from last_error

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        """Chat with automatic fallback across models."""
        errors: dict[str, str] = {}
        for cfg in self._configs:
            try:
                return await self._try_chat_with_retry(cfg, messages)
            except ModelUnavailableError as exc:
                errors[cfg.id] = exc.reason
                logger.warning(f"[Router] {cfg.id} unavailable, trying next model")
        raise AllModelsFailedError(errors)

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        """Stream tokens with automatic fallback across models.

        Validates connection by awaiting first token before committing to a model.
        Falls back to next model if first token raises.

        Note: record_success() is called when the first token arrives. Exceptions
        raised mid-stream propagate directly to the caller — no fallback is
        attempted and the circuit breaker is not updated for mid-stream failures.
        Unlike chat(), no per-model retry is performed; first-token failure
        immediately triggers fallback to the next model.
        """
        errors: dict[str, str] = {}
        for cfg in self._configs:
            cb = self._circuit_breakers[cfg.id]
            if not cb.is_available():
                errors[cfg.id] = "circuit open"
                continue

            first: str | None = None
            gen = None
            success = False

            try:
                provider = self._make_provider(cfg)
                gen = provider.stream(messages, timeout=cfg.timeout)
                # Peek first token — validates the connection before committing
                try:
                    first = await asyncio.wait_for(gen.__anext__(), timeout=cfg.timeout)
                    success = True
                except StopAsyncIteration:
                    # Empty stream — still a successful call
                    await cb.record_success()
                    return
            except Exception as exc:
                await cb.record_failure()
                errors[cfg.id] = str(exc)
                logger.warning(f"[Router] stream {cfg.id} failed: {exc}, trying next")
                continue

            if success and gen is not None:
                await cb.record_success()
                if first is not None:
                    yield first
                async for token in gen:
                    yield token
                return

        raise AllModelsFailedError(errors)
