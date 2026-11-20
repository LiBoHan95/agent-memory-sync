"""DeepSeek Harness 转录解析器（~/.dsh/sessions/<编码路径>/<会话>/session.v*.jsonl.zstd）。

基于 DSH session v3/v4 的实际格式。首行 type=session（id、cwd、createdAt），
之后每行一个事件：
  user/message      data.source.kind == "user" 才是用户亲手输入；runtime-context、
                    time-context、skills 等注入快照跳过。
  assistant/message data.message.content 里只取 text 块，丢掉 reasoning/tool-call。
文件默认是 zstd 压缩，由 base.read_lines 统一处理。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .base import DREAM_WORKDIR_MARK, Message, is_transcript, read_lines

AGENT = "dsh"
MAX_ASSISTANT_CHARS = 2000
NOISE_PREFIXES = ("<system-reminder>", "Current runtime context", "Time sampled")


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


def _iso(value) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            # NaN、无穷大或超出平台时间范围的事件不能终止整轮解析。
            pass
    return str(value or "")


def _is_human(data: dict) -> bool:
    source = data.get("source")
    # 只有显式标记为用户输入的消息才算人类输入；缺字段时按注入内容处理。
    return isinstance(source, dict) and source.get("kind") == "user"


class DshParser:
    agent = AGENT

    def matches(self, path: Path) -> bool:
        return is_transcript(path)

    def parse(self, path: Path, offset: int, *, device: str, rel: str) -> tuple[list[Message], int]:
        lines, new_offset = read_lines(path, offset)
        session = path.parent.name
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
            if kind not in ("user/message", "assistant/message"):
                continue
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            if kind == "user/message":
                if not _is_human(data):
                    continue
                text = (_text_of(data.get("content")) or "").strip()
                role = "user"
            else:
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                text = (_text_of(message.get("content")) or "").strip()
                role = "assistant"
                if len(text) > MAX_ASSISTANT_CHARS:
                    text = text[:MAX_ASSISTANT_CHARS] + " …[截断]"
            if not text or (role == "user" and text.startswith(NOISE_PREFIXES)):
                continue
            if DREAM_WORKDIR_MARK in cwd:
                continue
            out.append(Message(
                role=role, text=text, timestamp=_iso(entry.get("time")),
                session=session, agent=AGENT, device=device, file=rel, cwd=cwd))
        return out, new_offset
