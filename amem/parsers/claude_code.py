"""Claude Code 转录解析器（~/.claude/projects/<项目>/<session>.jsonl）。

基于 Claude Code 2.1.x 的实际格式。用户亲手输入的消息：
  type == "user"，message.content 是字符串，或含 text 块的列表。
需要排除的噪音：tool_result、压缩摘要、isMeta、任务通知/peer 消息、系统注入的 prompt、
斜杠命令回显、本地命令输出、"[Request interrupted by user" 提示、子 agent（isSidechain）。
assistant 只取 text 块（不要 thinking 和 tool_use）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import DREAM_WORKDIR_MARK, Message, is_transcript, read_lines

AGENT = "claude-code"
NOISE_PREFIXES = ("<command-name>", "<command-message>", "<command-args>",
                  "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                  "[Request interrupted by user", "<system-reminder>", "<task-notification>")
NOISE_ORIGINS = frozenset({"task-notification", "peer", "system"})
MAX_ASSISTANT_CHARS = 2000


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts) if parts else None


def _is_human(entry: dict) -> bool:
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") in NOISE_ORIGINS:
        return False
    if entry.get("promptSource") == "system":
        return False
    return True


def to_message(entry: dict, *, device: str, rel: str) -> Message | None:
    kind = entry.get("type")
    msg = entry.get("message")
    if kind not in ("user", "assistant") or not isinstance(msg, dict):
        return None
    if DREAM_WORKDIR_MARK in str(entry.get("cwd", "")):
        return None
    origin = "human"
    if kind == "user":
        if not _is_human(entry):
            return None
        text = _text_of(msg.get("content"))
        if text is None:
            return None
        text = text.strip()
        if not text or text.startswith(NOISE_PREFIXES):
            return None
        if entry.get("promptSource") == "sdk" or entry.get("entrypoint") == "sdk-cli":
            origin = "automated"
    else:
        if entry.get("isSidechain"):
            return None
        text = (_text_of(msg.get("content")) or "").strip()
        if not text:
            return None
        if len(text) > MAX_ASSISTANT_CHARS:
            text = text[:MAX_ASSISTANT_CHARS] + " …[截断]"
    return Message(role=kind, text=text, timestamp=str(entry.get("timestamp", "")),
                   session=str(entry.get("sessionId", "")), agent=AGENT, device=device,
                   file=rel, cwd=str(entry.get("cwd", "")), origin=origin)


class ClaudeCodeParser:
    agent = AGENT

    def matches(self, path: Path) -> bool:
        return is_transcript(path)

    def parse(self, path: Path, offset: int, *, device: str, rel: str) -> tuple[list[Message], int]:
        lines, new_offset = read_lines(path, offset)
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and (m := to_message(entry, device=device, rel=rel)):
                out.append(m)
        return out, new_offset
