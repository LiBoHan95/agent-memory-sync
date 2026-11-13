"""筛选阶段：解析之后、提炼之前，挑出值得交给大模型看的消息。

默认实现全部放行。可以接入小模型（如 TypeSafe Jev）做初筛以降低成本，约定：
- 只能减少消息，不能改写内容；
- 筛选结果不影响安全：密钥扫描和分级始终由 gate.py 负责；
- 实现要固定模型版本，并在 name 里体现（如 "jev-1.13.0"），报告里会记录。
"""
from __future__ import annotations

from typing import Protocol, Sequence

from .parsers.base import Message


class Screener(Protocol):
    name: str

    def screen(self, messages: Sequence[Message]) -> list[Message]: ...


class PassThrough:
    name = "pass-through"

    def screen(self, messages: Sequence[Message]) -> list[Message]:
        return list(messages)
