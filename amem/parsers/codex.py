"""Codex 转录解析器（~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl）。

基于 Codex 0.16x/0.17x 的实际格式。每行一个事件：
  {"timestamp": ..., "type": "session_meta" | "response_item" | "event_msg", "payload": {...}}
用户和 assistant 的文字在 response_item / payload.type == "message" 里：
  role=user 的 content 是 input_text 块；role=assistant 是 output_text 块。
需要排除：developer 角色（系统/技能指令）、AGENTS.md 注入、工具调用/结果、推理、
事件与用量记录、子代理会话（session_meta 的 thread_source == "subagent"）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import DREAM_WORKDIR_MARK, Message, read_complete_lines

AGENT = "codex"
MAX_ASSISTANT_CHARS = 2000
NOISE_PREFIXES = (
    "# AGENTS.md instructions",
    "<skills_instructions>",
    "<user_instructions>",
    "<environment_context>",
    "<INSTRUCTIONS>",
)


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else None


def _session_from_name(path: Path) -> str:
    """rollout-2026-10-16T15-14-39-<uuid>.jsonl → uuid。增量读取时 session_meta 已过，用文件名兜底。"""
    stem = path.stem
    return stem[-36:] if len(stem) >= 36 else stem


class CodexParser:
    agent = AGENT

    def matches(self, path: Path) -> bool:
        return path.suffix == ".jsonl"

    def parse(self, path: Path, offset: int, *, device: str, rel: str) -> tuple[list[Message], int]:
        lines, new_offset = read_complete_lines(path, offset)
        session = _session_from_name(path)
        cwd = ""
        is_subagent = False
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
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if kind == "session_meta":
                sid = payload.get("session_id") or payload.get("id")
                if sid:
                    session = str(sid)
                cwd = str(payload.get("cwd") or "")
                source = payload.get("source")
                if payload.get("thread_source") == "subagent" or (
                    isinstance(source, dict) and "subagent" in source
                ):
                    is_subagent = True
                continue
            if is_subagent:
                continue
            if kind != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = (_text_of(payload.get("content")) or "").strip()
            if not text:
                continue
            if role == "user" and text.startswith(NOISE_PREFIXES):
                continue
            if DREAM_WORKDIR_MARK in cwd:
                continue
            if role == "assistant" and len(text) > MAX_ASSISTANT_CHARS:
                text = text[:MAX_ASSISTANT_CHARS] + " …[截断]"
            out.append(Message(
                role=role, text=text, timestamp=str(entry.get("timestamp", "")),
                session=session, agent=AGENT, device=device, file=rel, cwd=cwd))
        if is_subagent:
            out = []
        return out, new_offset
