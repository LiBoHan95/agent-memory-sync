"""Pi 转录解析器（~/.pi/agent/sessions/<编码路径>/<时间戳>_<uuid>.jsonl）。

基于 Pi 1.5.x 的实际格式。首行 type=session（id、cwd），之后每行一个事件：
  {"type": "message", "message": {"role": user|assistant|toolResult, "content": [...]}}
content 是块列表：text / thinking / toolCall / image。只取 user 和 assistant 的 text 块，
丢掉思考、工具调用和工具结果。model_change、compaction、custom 等事件忽略。
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import DREAM_WORKDIR_MARK, Message, is_transcript, read_lines, transcript_stem

AGENT = "pi"
MAX_ASSISTANT_CHARS = 2000
NOISE_PREFIXES = ("[pi-",)


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else None


def _session_from_name(path: Path) -> str:
    """<时间戳>_<uuid>.jsonl → uuid。增量读取时 session 行已过，用文件名兜底。"""
    stem = transcript_stem(path)
    return stem.rsplit("_", 1)[-1] if "_" in stem else stem


class PiParser:
    agent = AGENT

    def matches(self, path: Path) -> bool:
        return is_transcript(path)

    def parse(self, path: Path, offset: int, *, device: str, rel: str) -> tuple[list[Message], int]:
        lines, new_offset = read_lines(path, offset)
        session = _session_from_name(path)
        cwd = ""
        out = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "session":
                sid = entry.get("id")
                if sid:
                    session = str(sid)
                cwd = str(entry.get("cwd") or "")
                if DREAM_WORKDIR_MARK in cwd:
                    return [], new_offset
                continue
            if kind != "message":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = (_text_of(message.get("content")) or "").strip()
            if not text:
                continue
            if role == "user" and text.startswith(NOISE_PREFIXES):
                continue
            if role == "assistant" and len(text) > MAX_ASSISTANT_CHARS:
                text = text[:MAX_ASSISTANT_CHARS] + " …[截断]"
            out.append(Message(
                role=role, text=text, timestamp=str(entry.get("timestamp", "")),
                session=session, agent=AGENT, device=device, file=rel, cwd=cwd))
        return out, new_offset
