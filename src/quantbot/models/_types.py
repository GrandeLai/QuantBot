"""Agent layer shared types — error hierarchy, enums, config dataclasses."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

# ── Error hierarchy ───────────────────────────────────────────────────────────


class AgentError(Exception):
    """Base error for the agent layer."""


class ModelUnavailableError(AgentError):
    """A specific model is unavailable (circuit open, timeout, API error)."""

    def __init__(self, model_id: str, reason: str) -> None:
        super().__init__(f"Model {model_id!r} unavailable: {reason}")
        self.model_id = model_id
        self.reason = reason


class AllModelsFailedError(AgentError):
    """All models in the fallback chain were exhausted."""

    def __init__(self, errors: dict[str, str]) -> None:
        lines = "\n".join(f"  {m}: {e}" for m, e in errors.items())
        super().__init__(f"All models failed:\n{lines}")
        self.errors = errors


# ── Circuit breaker state ─────────────────────────────────────────────────────


class CircuitState(Enum):
    """Health state of a model's circuit breaker."""

    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Requests blocked after too many failures
    HALF_OPEN = "half_open"  # One probe allowed to test recovery


# ── Config dataclasses ────────────────────────────────────────────────────────


@dataclass
class ModelConfig:
    """Configuration for a single model in the registry."""

    id: str
    name: str
    provider: str  # "openai" | "anthropic" | etc.
    priority: int = 1           # Lower = higher priority
    timeout: float = 30.0       # Seconds per request
    max_retries: int = 3        # Per-model retry attempts
    retry_base_delay: float = 0.5    # Exponential backoff base (seconds)
    retry_max_delay: float = 30.0    # Backoff cap (seconds)
    circuit_breaker_threshold: int = 5       # Failures before OPEN
    circuit_breaker_recovery: float = 60.0  # Seconds before HALF_OPEN


@dataclass
class AgentConfig:
    """Full agent configuration loaded from TOML."""

    models: list[ModelConfig] = field(default_factory=list)

    @classmethod
    def from_toml(cls, path: Path) -> AgentConfig:
        """Load config from a TOML file; return defaults if file absent."""
        if not path.exists():
            return cls._default()
        with open(path, "rb") as f:
            data = tomllib.load(f)
        agent_defaults = data.get("agent", {})
        models: list[ModelConfig] = []
        for m in data.get("models", []):
            models.append(
                ModelConfig(
                    id=m["id"],
                    name=m.get("name", m["id"]),
                    provider=m.get("provider", "openai"),
                    priority=int(m.get("priority", agent_defaults.get("priority", 1))),
                    timeout=float(m.get("timeout", agent_defaults.get("timeout", 30.0))),
                    max_retries=int(m.get("max_retries", agent_defaults.get("max_retries", 3))),
                    retry_base_delay=float(
                        m.get("retry_base_delay", agent_defaults.get("retry_base_delay", 0.5))
                    ),
                    retry_max_delay=float(
                        m.get("retry_max_delay", agent_defaults.get("retry_max_delay", 30.0))
                    ),
                    circuit_breaker_threshold=int(
                        m.get(
                            "circuit_breaker_threshold",
                            agent_defaults.get("circuit_breaker_threshold", 5),
                        )
                    ),
                    circuit_breaker_recovery=float(
                        m.get(
                            "circuit_breaker_recovery",
                            agent_defaults.get("circuit_breaker_recovery", 60.0),
                        )
                    ),
                )
            )
        return cls(models=models)

    @classmethod
    def _default(cls) -> AgentConfig:
        return cls(
            models=[
                ModelConfig(id="gpt-4o", name="GPT-4o", provider="openai", priority=1),
                ModelConfig(
                    id="claude-sonnet-4-6",
                    name="Claude Sonnet 4.6",
                    provider="anthropic",
                    priority=2,
                ),
                ModelConfig(id="gpt-4o-mini", name="GPT-4o Mini", provider="openai", priority=3),
                ModelConfig(
                    id="deepseek/deepseek-chat",
                    name="DeepSeek Chat",
                    provider="deepseek",
                    priority=4,
                ),
            ]
        )


# ── Chat message ──────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """Chat message exchanged with the agent."""

    role: str    # "user" | "assistant" | "system"
    content: str
