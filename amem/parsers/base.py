"""转录统一格式与解析器接口。

各 agent 的原始转录格式不同，也会随版本变化。解析器把它们转成统一的 Message，
只保留用户亲手输入的消息和 agent 的文字回复，丢掉工具调用、系统注入等噪音。
原始文件永远不改，解析器坏了可以修好重跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# dream.py 运行 headless agent 时用的临时目录前缀。解析器必须跳过这些会话，
# 否则 dreaming 自己的转录会被归档后再次读入，形成循环。
DREAM_WORKDIR_MARK = "amem-dream-"


@dataclass(frozen=True)
class Message:
    role: str          # user | assistant
    text: str
    timestamp: str     # ISO 8601，原样保留
    session: str
    agent: str         # claude-code | codex | pi | ...
    device: str
    file: str          # 来源文件（相对归档根目录）
    cwd: str = ""
    origin: str = "human"   # human：用户亲手输入 | automated：脚本/SDK 发来的提示词，只作上下文


class Parser(Protocol):
    agent: str         # 与归档目录名一致

    def matches(self, path: Path) -> bool:
        """这个文件是否归本解析器处理。"""

    def parse(self, path: Path, offset: int, *, device: str, rel: str) -> tuple[list[Message], int]:
        """从字节偏移 offset 处继续解析，返回 (消息, 新偏移)。
        只消费完整的行/记录；文件比 offset 短（被截断或替换）时从 0 重新开始。"""


def read_complete_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """读 offset 之后的完整行，返回 (行, 新偏移)。最后一行没写完就留到下次。"""
    size = path.stat().st_size
    if size < offset:
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return data[:end].split(b"\n"), offset + end + 1
