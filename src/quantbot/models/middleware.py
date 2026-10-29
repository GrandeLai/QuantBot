"""Middleware protocol and built-in implementations."""
from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class Middleware(Protocol):
    """Hook interface called around every agent request.

    Extension point: implement this protocol to add RAG, tracing, auth, etc.
    All methods are async; implementations may be no-ops.
    """

    async def before_request(
        self,
        messages: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        """Called before the LLM request is dispatched.

        Args:
            messages: Full message list (system prompt + history).
            context: Mutable dict shared across all hooks for this request.
        """
        ...

    async def after_response(
        self,
        response: str,
        context: dict[str, Any],
    ) -> str:
        """Called after a successful response.

        Returns the (possibly modified) response string.
        """
        ...

    async def on_error(
        self,
        error: Exception,
        context: dict[str, Any],
    ) -> None:
        """Called when the router raises an error (after all fallbacks exhausted)."""
        ...


class LoggingMiddleware:
    """Logs request/response metadata at INFO level."""

    async def before_request(
        self,
        messages: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        context["_start_time"] = time.monotonic()
        logger.info(f"[Agent] request: {len(messages)} messages")

    async def after_response(
        self,
        response: str,
        context: dict[str, Any],
    ) -> str:
        elapsed = time.monotonic() - context.get("_start_time", time.monotonic())
        logger.info(f"[Agent] response: {len(response)} chars in {elapsed:.2f}s")
        return response

    async def on_error(
        self,
        error: Exception,
        context: dict[str, Any],
    ) -> None:
        logger.error(f"[Agent] error: {error}")
