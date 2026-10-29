"""QuantBot unified LLM agent layer.

Usage:
    from quantbot.models import get_default_agent, ChatMessage

    agent = get_default_agent()
    reply = await agent.chat([ChatMessage(role="user", content="hi")])
"""
from quantbot.models._types import ChatMessage
from quantbot.models.agent import QuantAgent, get_default_agent

__all__ = ["QuantAgent", "get_default_agent", "ChatMessage"]
