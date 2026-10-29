"""Per-model circuit breaker — CLOSED → OPEN → HALF_OPEN state machine."""
from __future__ import annotations

import asyncio
import time

from loguru import logger

from quantbot.models._types import CircuitState


class CircuitBreaker:
    """3-state circuit breaker for a single LLM model endpoint.

    State transitions:
      CLOSED  → OPEN      when failure_count >= threshold
      OPEN    → HALF_OPEN after recovery_seconds have elapsed
      HALF_OPEN → CLOSED  on next success (caller must serialize probes)
      HALF_OPEN → OPEN    on next failure
    """

    def __init__(
        self,
        model_id: str,
        threshold: int = 5,
        recovery_seconds: float = 60.0,
    ) -> None:
        """Initialize circuit breaker for a model.

        Args:
            model_id: The model identifier (e.g., "gpt-4o").
            threshold: Number of failures before opening the circuit.
            recovery_seconds: Seconds to wait in OPEN state before attempting probe.
        """
        self._model_id = model_id
        self._threshold = threshold
        self._recovery = recovery_seconds
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state (read without lock — eventual consistency ok)."""
        return self._state

    def is_available(self) -> bool:
        """Return True if a request should be allowed through.

        Side effect: transitions OPEN → HALF_OPEN when recovery period expires.
        """
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._recovery:
                self._state = CircuitState.HALF_OPEN
                logger.info(f"[CircuitBreaker] {self._model_id}: OPEN → HALF_OPEN (probe)")
                return True
            return False
        # HALF_OPEN: allow the probe
        return True

    async def record_success(self) -> None:
        """Record a successful response — resets failure count and closes circuit."""
        async with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info(
                    f"[CircuitBreaker] {self._model_id}: {self._state.value} → CLOSED"
                )
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    async def record_failure(self) -> None:
        """Record a failed response — may open the circuit."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN or self._failure_count >= self._threshold:
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        f"[CircuitBreaker] {self._model_id}: → OPEN "
                        f"(failures={self._failure_count})"
                    )
                self._state = CircuitState.OPEN
