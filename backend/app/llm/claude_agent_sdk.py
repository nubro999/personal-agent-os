"""Claude Agent SDK provider. 로그인된 Claude Code 인증(개인 구독)을 쓴다.

ANTHROPIC_API_KEY가 환경에 있으면 SDK가 구독 대신 그 키로 조용히 과금하므로 아예 실행하지 않는다.
"""

import os

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from pydantic import ValidationError

from app.llm.base import LLMError, M


class ClaudeAgentSDKLLM:
    def __init__(self, model: str) -> None:
        self._model = model

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M:
        if os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError(
                "ANTHROPIC_API_KEY is set; unset it to use the Claude subscription login"
            )
        options = ClaudeAgentOptions(
            model=self._model,
            system_prompt=system,
            # 도구, 사용자 설정, CLAUDE.md, 플러그인 없이 텍스트 추출만 한다.
            tools=[],
            setting_sources=[],
            max_turns=3,
            output_format={"type": "json_schema", "schema": output.model_json_schema()},
            # 호출마다 ~/.claude/projects에 세션 파일이 쌓이지 않게 한다.
            extra_args={"no-session-persistence": None},
        )
        result: ResultMessage | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None or result.structured_output is None:
            detail = result.errors if result else "no result"
            raise LLMError(f"Claude returned no structured output: {detail}")
        try:
            return output.model_validate(result.structured_output)
        except ValidationError as exc:
            raise LLMError(f"Claude output did not match {output.__name__}") from exc
