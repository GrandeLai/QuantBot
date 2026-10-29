"""Shared structured model gateway for equity and crypto allocations."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import threading

from .models import CodexDecisionProvider, OpenAICompatibleProvider, PriceActionProvider


class RoutedDecisionProvider:
    def __init__(self, config, environ=None, transport=None):
        from quantbot.models._types import ModelConfig
        from quantbot.models.router import ModelRouter

        self.config = config
        self.environ = environ
        self.transport = transport
        self._lock = threading.Lock()
        # Validate the primary connection before constructing a fallback chain.
        OpenAICompatibleProvider(config, environ=environ, transport=transport)
        models = [config["llm"]["model"], *config["llm"].get("fallback_models", [])]
        self.router = ModelRouter(
            [
                ModelConfig(id=m, name=m, provider="openai", priority=i, max_retries=1)
                for i, m in enumerate(models)
            ],
            lambda _: None,
        )

    def decide(self, context, account, config):
        def invoke(model):
            selected = deepcopy(config)
            selected["llm"]["model"] = model.id
            return OpenAICompatibleProvider(selected, self.environ, self.transport).decide(
                context, account, selected
            )

        # FastAPI sync endpoints may run on separate threads; circuit state has
        # one owner at a time across their short-lived asyncio event loops.
        with self._lock:
            return asyncio.run(self.router.invoke(invoke))


def decision_provider(config, *, environ=None):
    if config["strategy"]["mode"] == "price_action":
        return PriceActionProvider()
    if config["llm"].get("backend") == "codex_cli":
        return CodexDecisionProvider(config, environ)
    if config["llm"].get("fallback_models"):
        return RoutedDecisionProvider(config, environ)
    return OpenAICompatibleProvider(config, environ)
