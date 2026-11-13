"""转录解析器注册表。按归档目录名（<设备>/<agent>/）选解析器。"""
from __future__ import annotations

from .base import Message, Parser
from .claude_code import ClaudeCodeParser

PARSERS: dict[str, Parser] = {p.agent: p for p in (ClaudeCodeParser(),)}


def get(agent: str) -> Parser | None:
    return PARSERS.get(agent)


__all__ = ["Message", "Parser", "PARSERS", "get"]
