from functools import lru_cache

from app.config import get_settings
from app.llm.base import StructuredLLM
from app.llm.claude_agent_sdk import ClaudeAgentSDKLLM


@lru_cache
def get_llm() -> StructuredLLM:
    return ClaudeAgentSDKLLM(model=get_settings().llm_model)
