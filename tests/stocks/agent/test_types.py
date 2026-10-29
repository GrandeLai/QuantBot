"""Tests for agent type definitions and config loading."""
from __future__ import annotations

from pathlib import Path

from quantbot.models._types import (
    AgentConfig,
    AgentError,
    AllModelsFailedError,
    ChatMessage,
    CircuitState,
    ModelConfig,
    ModelUnavailableError,
)


class TestErrors:
    def test_agent_error_is_exception(self) -> None:
        err = AgentError("test")
        assert isinstance(err, Exception)

    def test_model_unavailable_error_has_model_id(self) -> None:
        err = ModelUnavailableError("gpt-4o", "rate limited")
        assert err.model_id == "gpt-4o"
        assert "gpt-4o" in str(err)
        assert "rate limited" in str(err)

    def test_all_models_failed_error_lists_models(self) -> None:
        err = AllModelsFailedError({"gpt-4o": "timeout", "claude": "401"})
        assert "gpt-4o" in str(err)
        assert "claude" in str(err)


class TestCircuitState:
    def test_three_states_exist(self) -> None:
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


class TestModelConfig:
    def test_defaults(self) -> None:
        cfg = ModelConfig(id="gpt-4o", name="GPT-4o", provider="openai")
        assert cfg.priority == 1
        assert cfg.timeout == 30.0
        assert cfg.max_retries == 3
        assert cfg.retry_base_delay == 0.5
        assert cfg.retry_max_delay == 30.0
        assert cfg.circuit_breaker_threshold == 5
        assert cfg.circuit_breaker_recovery == 60.0


class TestAgentConfig:
    def test_from_toml_loads_models(self, tmp_path: Path) -> None:
        toml = tmp_path / "llm_agents.toml"
        toml.write_text(
            '[agent]\nretry_base_delay = 1.0\n\n'
            '[[models]]\nid = "gpt-4o"\nname = "GPT-4o"\nprovider = "openai"\npriority = 1\n'
            '[[models]]\nid = "claude-sonnet-4-6"\nname = "Claude"\nprovider = "anthropic"\npriority = 2\n',
            encoding="utf-8",
        )
        config = AgentConfig.from_toml(toml)
        assert len(config.models) == 2
        assert config.models[0].id == "gpt-4o"
        assert config.models[1].id == "claude-sonnet-4-6"

    def test_from_toml_agent_defaults_propagate(self, tmp_path: Path) -> None:
        toml = tmp_path / "llm_agents.toml"
        toml.write_text(
            '[agent]\nretry_base_delay = 2.0\n\n'
            '[[models]]\nid = "gpt-4o"\nname = "GPT-4o"\nprovider = "openai"\npriority = 1\n',
            encoding="utf-8",
        )
        config = AgentConfig.from_toml(toml)
        assert config.models[0].retry_base_delay == 2.0

    def test_from_toml_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        config = AgentConfig.from_toml(tmp_path / "nonexistent.toml")
        assert len(config.models) > 0
        assert config.models[0].id == "gpt-4o"

    def test_model_level_overrides_agent_default(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        toml.write_text(
            '[agent]\nretry_base_delay = 9.9\n\n'
            '[[models]]\nid = "x"\nname = "X"\nprovider = "openai"\npriority = 1\nretry_base_delay = 1.1\n',
            encoding="utf-8",
        )
        config = AgentConfig.from_toml(toml)
        assert config.models[0].retry_base_delay == 1.1

    def test_from_toml_all_merge_fields_propagate(self, tmp_path: Path) -> None:
        toml = tmp_path / "t.toml"
        toml.write_text(
            '[agent]\n'
            'retry_base_delay = 2.0\n'
            'retry_max_delay = 60.0\n'
            'circuit_breaker_threshold = 10\n'
            'circuit_breaker_recovery = 120.0\n\n'
            '[[models]]\nid = "x"\nname = "X"\nprovider = "openai"\npriority = 1\n',
            encoding="utf-8",
        )
        config = AgentConfig.from_toml(toml)
        m = config.models[0]
        assert m.retry_base_delay == 2.0
        assert m.retry_max_delay == 60.0
        assert m.circuit_breaker_threshold == 10
        assert m.circuit_breaker_recovery == 120.0


class TestChatMessage:
    def test_user_message(self) -> None:
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
