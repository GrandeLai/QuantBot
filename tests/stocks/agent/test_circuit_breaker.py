"""Tests for per-model circuit breaker state machine."""
from __future__ import annotations

from quantbot.models._types import CircuitState
from quantbot.models.circuit_breaker import CircuitBreaker


class TestCircuitBreakerInitial:
    def test_starts_closed(self) -> None:
        cb = CircuitBreaker("m1", threshold=3, recovery_seconds=60.0)
        assert cb.state == CircuitState.CLOSED

    def test_starts_available(self) -> None:
        cb = CircuitBreaker("m1", threshold=3, recovery_seconds=60.0)
        assert cb.is_available()


class TestCircuitBreakerFailures:
    async def test_below_threshold_stays_closed(self) -> None:
        cb = CircuitBreaker("m1", threshold=3, recovery_seconds=60.0)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.is_available()

    async def test_opens_at_threshold(self) -> None:
        cb = CircuitBreaker("m1", threshold=3, recovery_seconds=60.0)
        for _ in range(3):
            await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.is_available()

    async def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker("m1", threshold=3, recovery_seconds=60.0)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        assert cb.state == CircuitState.CLOSED
        # After reset, needs threshold failures to open again
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == CircuitState.CLOSED  # still closed, not yet at threshold


class TestCircuitBreakerRecovery:
    async def test_open_transitions_to_half_open_after_recovery(self) -> None:
        cb = CircuitBreaker("m1", threshold=1, recovery_seconds=0.0)
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # With recovery_seconds=0, is_available() immediately probes
        assert cb.is_available()
        state_after = cb.state  # re-read to break mypy's literal narrowing
        assert state_after == CircuitState.HALF_OPEN

    async def test_open_stays_open_before_recovery(self) -> None:
        cb = CircuitBreaker("m1", threshold=1, recovery_seconds=9999.0)
        await cb.record_failure()
        assert not cb.is_available()
        assert cb.state == CircuitState.OPEN


class TestCircuitBreakerHalfOpen:
    async def test_half_open_success_closes(self) -> None:
        cb = CircuitBreaker("m1", threshold=1, recovery_seconds=0.0)
        await cb.record_failure()
        cb.is_available()  # → HALF_OPEN
        await cb.record_success()
        assert cb.state == CircuitState.CLOSED

    async def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker("m1", threshold=1, recovery_seconds=0.0)
        await cb.record_failure()
        cb.is_available()  # → HALF_OPEN
        await cb.record_failure()
        assert cb.state == CircuitState.OPEN

    async def test_half_open_is_available(self) -> None:
        cb = CircuitBreaker("m1", threshold=1, recovery_seconds=0.0)
        await cb.record_failure()
        cb.is_available()  # trigger transition
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.is_available()  # HALF_OPEN allows probe
