from typing import Protocol, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class StructuredLLM(Protocol):
    """스키마에 맞는 구조화 출력 하나를 돌려주는 LLM.

    출력은 신뢰하지 않고 도메인에서 다시 검증한다.
    """

    async def generate(self, *, system: str, prompt: str, output: type[M]) -> M: ...
