"""
agents/orchestrator.py — Pluggable LLM provider interface.

Nothing in this project is tightly coupled to one AI vendor: any provider
just needs to implement this interface and return an LLMResponse. If no
provider is configured, the rest of the application must keep working —
callers should treat get_provider() returning None as "AI analysis
unavailable," never crash.

This module (and this interface) never imports scanner/, api/, or
frontend code, and nothing here can invoke a brokerage order — Claude's
role in this project is reasoning/interpretation over data Python already
computed, never execution.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class LLMResponse:
    """
    Provider-agnostic result from analyze()/chat(). Token usage fields are
    None for providers that don't report them — callers must not assume
    they're always populated.
    """

    text: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None


class LLMProvider(ABC):
    """Minimal interface an AI provider must implement to plug into this agent."""

    @abstractmethod
    def analyze(self, prompt: str, system_prompt: str, max_tokens: int = 1400) -> LLMResponse:
        """
        Run a single structured-analysis prompt and return the response +
        usage. `system_prompt` is required (not defaulted here) so every
        caller is explicit about which agent's instructions apply — a
        provider must never silently reuse another agent's system prompt.
        """
        raise NotImplementedError

    @abstractmethod
    def chat(self, message: str, history: Optional[List[dict]] = None) -> LLMResponse:
        """Free-form chat turn without tool use (not currently used by anything)."""
        raise NotImplementedError

    @abstractmethod
    def run_tool_loop(
        self,
        user_message: str,
        system_prompt: str,
        tools: List[Dict[str, Any]],
        execute_tool: Callable[[str, dict], Dict[str, Any]],
        max_iterations: int = 5,
    ) -> LLMResponse:
        """
        Run an agentic tool-use loop: send `user_message`, and whenever the
        model requests a tool call, invoke `execute_tool(name, input)` (which
        must return a JSON-serializable dict) and feed the result back, until
        the model produces a final text answer or `max_iterations` is hit.

        `tools` uses the common {"name", "description", "input_schema"} shape
        shared by most providers' function/tool-calling APIs — this keeps
        callers (agents/chat_agent.py) provider-agnostic; only the concrete
        provider implementation below knows how its own API wants that shaped.
        """
        raise NotImplementedError
