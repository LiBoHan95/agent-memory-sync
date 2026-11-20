"""转录统一格式与解析器接口。

各 agent 的原始转录格式不同，也会随版本变化。解析器把它们转成统一的 Message，
只保留用户亲手输入的消息和 agent 的文字回复，丢掉工具调用、系统注入等噪音。
原始文件永远不改，解析器坏了可以修好重跑。
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


COMPRESSED_SUFFIXES = (".zst", ".zstd")


def is_compressed(path: Path) -> bool:
    return path.suffix.lower() in COMPRESSED_SUFFIXES


def is_transcript(path: Path) -> bool:
    """各 agent 的转录文件：.jsonl 或 zstd 压缩后的 .jsonl.zst/.jsonl.zstd。"""
    name = path.name.lower()
    return name.endswith(".jsonl") or name.endswith(".jsonl.zst") or name.endswith(".jsonl.zstd")


def _zstd_binary() -> str:
    binary = shutil.which("zstd")
    if not binary:
        raise RuntimeError("读取压缩转录需要 zstd，但 PATH 里找不到该命令")
    return binary


def _zstd_decompress(path: Path) -> bytes:
    r = subprocess.run([_zstd_binary(), "-dc", str(path)], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"zstd 解压失败: {path}: {r.stderr[-200:]}")
    return r.stdout


def complete_lines(data: bytes) -> list[bytes]:
    """按换行切出所有行；末尾没有换行时仍保留最后一行。

    压缩文件是快照，最后一行即使没有换行也可能是完整记录；是否算“已消费”
    由 read_lines 再用 JSON 是否完整判断。
    """
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _incomplete_tail(data: bytes) -> bool:
    """末尾没有换行且不是合法 JSON 时，认为最后一行还没写完。

    所有转录都是 JSONL。压缩快照可能正好截在一条记录中间；若把它计入已消费
    行数，之后文件被追加补全时这条消息会永久漏掉。
    """
    if not data or data.endswith(b"\n"):
        return False
    tail = data.rsplit(b"\n", 1)[-1]
    if not tail.strip():
        return True
    try:
        json.loads(tail)
    except ValueError:
        return True
    return False


def plain_sibling(path: Path) -> Path | None:
    """`foo.jsonl.zst` -> `foo.jsonl`；不是压缩文件时返回 None。"""
    for suffix in (".zst", ".zstd"):
        if path.name.lower().endswith(suffix):
            return path.with_name(path.name[:-len(suffix)])
    return None


def plain_rel_sibling(rel: str) -> str | None:
    for suffix in (".zst", ".zstd"):
        if rel.lower().endswith(suffix):
            return rel[:-len(suffix)]
    return None


def transcript_stem(path: Path) -> str:
    """去掉 `.jsonl`/`.jsonl.zst`/`.jsonl.zstd` 后缀，供文件名兜底 session id。"""
    name = path.name
    for suffix in (".jsonl.zstd", ".jsonl.zst", ".jsonl"):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return path.stem


def compressed_line_state(path: Path, byte_offset: int) -> tuple[int, int, int]:
    """把明文时代的字节偏移换算成压缩文件里的行状态。

    返回 (已消费行数, 完整行总数, 解压后字节数)。压缩文件可能被逐帧追加，
    不能再用压缩字节数当偏移，否则每次新增事件都会重读整个会话。
    """
    data = _zstd_decompress(path)
    lines = complete_lines(data)
    end = min(max(byte_offset, 0), len(data))
    consumed = min(data.count(b"\n", 0, end), len(lines))
    return consumed, len(lines), len(data)


def read_lines(path: Path, offset: int) -> tuple[list[bytes], int]:
    """读取转录的完整行。

    未压缩文件按字节偏移增量读。压缩文件的 offset 是**已消费的完整行数**，
    新偏移也是行数：这样 zstd 文件被追加新帧后，只返回新增行。压缩文件被
    替换或变短时 offset 会越界，此时从头读。

    末尾没有换行、又还不是合法 JSON 的行是写了一半的记录，不计入已消费
    行数；等它被追加补全后再解析。
    """
    if not is_compressed(path):
        return read_complete_lines(path, offset)
    data = _zstd_decompress(path)
    lines = complete_lines(data)
    if offset < 0 or offset > len(lines):
        offset = 0
    new_offset = len(lines)
    if _incomplete_tail(data):
        new_offset -= 1
    return lines[offset:], max(new_offset, 0)


def byte_offset_for_lines(path: Path, count: int) -> int:
    """返回明文文件第 count 个完整行之后的字节偏移。

    用于把压缩层已经读过的行数换算回明文的起点；不足 count 行时返回文件
    末尾。只读到第 count 行为止，不把整个文件载入内存。
    """
    if count <= 0:
        return 0
    offset = 0
    seen = 0
    with open(path, "rb") as f:
        while True:
            line = f.readline()
            if not line:
                return offset
            offset += len(line)
            if line.endswith(b"\n"):
                seen += 1
                if seen >= count:
                    return offset


def prefix_matches_lines(plain: Path, compressed: Path, count: int) -> bool:
    """明文前 count 行是否与压缩文件解压后的前 count 行逐字节一致。

    只有一致时才能把压缩层的行进度套到明文上；明文被替换成不同内容时必须
    从头处理，不能跳过前 count 行。
    """
    if count <= 0:
        return True
    proc = subprocess.Popen([_zstd_binary(), "-dc", str(compressed)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    try:
        with open(plain, "rb") as f:
            for _ in range(count):
                zline = proc.stdout.readline()
                if not zline or f.readline() != zline:
                    return False
        return True
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def read_head_lines(path: Path, limit: int = 5) -> list[bytes]:
    """只读文件开头几行，供扫描会话元数据使用，不把大文件整个解压进内存。

    压缩文件读到 EOF 时会检查 zstd 退出码。完全损坏的文件抛 RuntimeError；
    DSH 文件正在写入时可能出现“合法前缀 + 半帧”，这种情况只要已经读到至少
    一行就返回已有内容，不当作损坏。
    """
    if not is_compressed(path):
        with open(path, "rb") as f:
            return [f.readline() for _ in range(limit)]
    proc = subprocess.Popen([_zstd_binary(), "-dc", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        lines = [proc.stdout.readline() for _ in range(limit)]
        if lines and not lines[-1]:
            try:
                _, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"zstd 解压超时: {path}") from None
            if proc.returncode != 0:
                message = err.decode("utf-8", "replace").lower()
                truncated_tail = any(s in message for s in
                                     ("premature", "unexpected end", "truncated"))
                if not (truncated_tail and any(lines)):
                    raise RuntimeError(f"zstd 解压失败: {path}: {err[-200:]}")
        return lines
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
