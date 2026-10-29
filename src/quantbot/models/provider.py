"""LiteLLM-backed model provider — chat and streaming."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm

litellm.drop_params = True  # ignore unsupported params per model


class LiteLLMProvider:
    """Thin async wrapper around litellm.acompletion for a single model.

    Responsible for one model only — no retry or fallback logic here.
    That lives in ModelRouter (Task 4).
    """

    def __init__(self, model_id: str, api_key: str = "") -> None:
        self._model = model_id
        self._api_key = api_key or None
        self.base_url: str | None = None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> str:
        """Non-streaming chat; returns the full response string."""
        response = await litellm.acompletion(
            model=self._model,
            messages=messages,
            api_key=self._api_key,
            timeout=timeout,
            api_base=self.base_url,
            **kwargs,
        )
        return str(response.choices[0].message.content or "")

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Return an async iterator that yields tokens as they arrive."""
        return self._stream_gen(messages, timeout=timeout, **kwargs)

    async def _stream_gen(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        response = await litellm.acompletion(
            model=self._model,
            messages=messages,
            api_key=self._api_key,
            timeout=timeout,
            stream=True,
            api_base=self.base_url,
            **kwargs,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta
            if hasattr(delta, "content") and delta.content:
                yield delta.content
