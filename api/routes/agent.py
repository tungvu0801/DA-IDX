"""
api/routes/agent.py — AI usage reporting and the AI chat endpoint.

GET  /api/ai/usage    today's AI call/token/cost tracking (agents/usage_tracker.py)
POST /api/agent/chat  real Claude tool-use chat (agents/chat_agent.py) — user-initiated,
                       so it bypasses the automatic Attention Score threshold, but the
                       AI cache and hourly/daily call limits still apply.
"""
import logging

from fastapi import APIRouter

from agents.chat_agent import run_chat
from agents.usage_tracker import usage_tracker
from models.schemas import AIUsageResponse, ChatRequest, ChatResponse, ChatToolCallResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent"])


@router.get("/api/ai/usage", response_model=AIUsageResponse)
def get_ai_usage() -> AIUsageResponse:
    return AIUsageResponse(**usage_tracker.summary_today(), budgets=usage_tracker.budgets())


@router.post("/api/agent/chat", response_model=ChatResponse)
def post_agent_chat(body: ChatRequest) -> ChatResponse:
    try:
        result = run_chat(body.message)
    except Exception as exc:  # noqa: BLE001 - chat must never 500 the whole app
        logger.error("Chat endpoint failed unexpectedly: %s", exc)
        return ChatResponse(
            answer="Chat is temporarily unavailable. The scanner and dashboard are unaffected.",
            tool_calls=[],
            ai_available=False,
        )

    return ChatResponse(
        answer=result.answer,
        tool_calls=[
            ChatToolCallResponse(tool=tc.tool, input=tc.input, available=tc.available) for tc in result.tool_calls
        ],
        ai_available=result.ai_available,
    )
