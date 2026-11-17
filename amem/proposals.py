"""提案解析与用户决定。

dreaming 把 profile/ rules/ 的变更写成 proposals/ 下的补丁文件。本模块负责：

  - 解析提案里的每一条变更（标题 + unified diff）；
  - 把补丁应用到当前 vault 内容，冲突时拒绝而不是硬写；
  - 用户批准后写入目标文件、把标题状态改为 accepted 并提交；
    用户驳回时只改状态，不碰目标文件。

写操作只允许落在 profile/ 与 rules/ 下的 .md 文件，其它路径一律拒绝。
"""
from __future__ import annotations

import os
import posixpath
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import format as fmt
from .gate import RegexScanner

PENDING, ACCEPTED, REJECTED = "pending", "accepted", "rejected"
STATUSES = frozenset({PENDING, ACCEPTED, REJECTED})
MAX_FILE_BYTES = 256 * 1024

_TARGET_RE = re.compile(r"^(profile|rules)/[^/]+\.md$")
_SECTION_RE = re.compile(
    r"^##\s+(\d+)\.\s+(新增|修改)\s+`([^`]+)`\s+·\s+(pending|accepted|rejected)\s*$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FENCE_RE = re.compile(r"^`{4,}\s*diff\s*$")

# 同一进程里的 Web 请求串行处理提案写回；跨进程（dreaming）靠补丁上下文比对兜底。
_LOCK = threading.Lock()
_SCANNER = RegexScanner()


class ProposalError(Exception):
    """提案无法处理。status 供 Web 层映射 HTTP 状态码。"""
    status = 400


class ProposalNotFound(ProposalError):
    status = 404


class ProposalConflict(ProposalError):
    status = 409


@dataclass(frozen=True)
class Section:
    index: int
    kind: str              # add | modify
    target: str
    status: str
    heading: int           # 标题在整个提案文本里的行号（0 起）
    diff: tuple[str, ...] | None

    @property
    def label(self) -> str:
        return "新增" if self.kind == "add" else "修改"


@dataclass(frozen=True)
class Result:
    action: str
    proposal: str
    index: int
    target: str


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: tuple[str, ...]


def _clean_name(name: str) -> bool:
    return (bool(name) and name == posixpath.basename(name)
            and name.endswith(".md") and not name.startswith(".")
            and not fmt.is_sync_conflict(name)
            and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in name))


def parse_sections(text: str) -> list[Section]:
    """解析 `## N. 新增|修改 \`path\` · status` 及其后的 ````diff 代码块。"""
    lines = text.splitlines()
    out: list[Section] = []
    i = 0
    while i < len(lines):
        m = _SECTION_RE.match(lines[i])
        if not m:
            i += 1
            continue
        index, kind_label, target, status = m.groups()
        heading = i
        diff: tuple[str, ...] | None = None
        j = i + 1
        while j < len(lines) and not _FENCE_RE.match(lines[j].strip()):
            j += 1
        if j < len(lines):
            k = j + 1
            body: list[str] = []
            while k < len(lines) and lines[k].strip() != "````":
                body.append(lines[k])
                k += 1
            if k < len(lines):
                diff = tuple(body)
                i = k
        out.append(Section(
            index=int(index),
            kind="add" if kind_label == "新增" else "modify",
            target=target,
            status=status,
            heading=heading,
            diff=diff,
        ))
        i += 1
    return out


def section_summary(sec: Section) -> str:
    return f"{sec.label} {sec.target}"


def _parse_hunks(diff: tuple[str, ...]) -> list[_Hunk]:
    raw_hunks: list[tuple[int, int, int, int, list[str]]] = []
    for raw in diff:
        m = _HUNK_RE.match(raw)
        if m:
            raw_hunks.append((int(m.group(1)), int(m.group(2) or 1),
                              int(m.group(3)), int(m.group(4) or 1), []))
            continue
        if raw == "":
            continue
        if not raw_hunks:
            # difflib 的补丁在第一个 @@ 前有 ---/+++（以及可选的 diff/index）文件头。
            if raw.startswith(("--- ", "+++ ", "diff ", "index ")):
                continue
            raise ProposalError("补丁缺少 @@ 头")
        raw_hunks[-1][4].append(raw)
    if not raw_hunks:
        raise ProposalError("补丁为空")
    out = []
    for old_start, old_len, new_start, new_len, lines in raw_hunks:
        old_seen = sum(1 for line in lines if line[:1] in (" ", "-"))
        new_seen = sum(1 for line in lines if line[:1] in (" ", "+"))
        if old_seen != old_len or new_seen != new_len:
            raise ProposalError("补丁行数与 @@ 头不一致")
        out.append(_Hunk(old_start, old_len, new_start, new_len, tuple(lines)))
    return out


def apply_diff(old: str | None, sec: Section) -> str:
    """把提案补丁应用到 old（None 表示新增文件），返回新内容。"""
    if sec.diff is None:
        raise ProposalError("这条提案没有可应用的补丁")
    hunks = _parse_hunks(sec.diff)
    if sec.kind == "add":
        if old is not None:
            raise ProposalConflict("目标文件已存在，无法按新增应用")
        lines: list[str] = []
        for h in hunks:
            if h.old_len or h.old_start:
                raise ProposalError("新增补丁格式不正确")
            for line in h.lines:
                tag = line[:1]
                if tag == "+":
                    lines.append(line[1:])
                elif tag == "\\":
                    continue
                else:
                    raise ProposalError("新增补丁里出现了非新增行")
        return "\n".join(lines) + ("\n" if lines else "")
    if old is None:
        raise ProposalConflict("目标文件不存在，无法按修改应用")

    old_lines = old.splitlines()
    new_lines: list[str] = []
    pos = 0
    for h in hunks:
        start = h.old_start - 1 if h.old_len else h.old_start
        if start < pos or start > len(old_lines):
            raise ProposalConflict("文件内容与提案生成时不一致")
        new_lines.extend(old_lines[pos:start])
        pos = start
        for line in h.lines:
            tag, content = line[:1], line[1:]
            if tag == "\\":
                continue
            if tag not in (" ", "+", "-"):
                raise ProposalError("补丁格式错误")
            if tag in (" ", "-"):
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise ProposalConflict("文件内容与提案生成时不一致")
                pos += 1
            if tag in (" ", "+"):
                new_lines.append(content)
    new_lines.extend(old_lines[pos:])
    out = "\n".join(new_lines)
    if old.endswith("\n") and new_lines:
        out += "\n"
    return out


def _target_path(vault: Path, rel: str) -> Path:
    parts = rel.split("/")
    if not _TARGET_RE.match(rel) or ".." in parts or any(not p for p in parts):
        raise ProposalError("只支持 profile/ 与 rules/ 下的 .md 文件")
    current = vault
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ProposalError("目标路径包含符号链接")
    try:
        current.resolve().relative_to(vault.resolve())
    except ValueError:
        raise ProposalError("目标路径越界") from None
    return current


def _read_target(vault: Path, sec: Section) -> str | None:
    path = _target_path(vault, sec.target)
    if sec.kind == "add":
        if path.exists():
            raise ProposalConflict("目标文件已存在，无法按新增应用")
        return None
    if not path.is_file():
        raise ProposalConflict("目标文件不存在，无法按修改应用")
    try:
        data = path.read_bytes()
    except OSError:
        raise ProposalError("目标文件无法读取") from None
    if len(data) > MAX_FILE_BYTES:
        raise ProposalError(f"目标文件超过 {MAX_FILE_BYTES} 字节")
    text = fmt.decode(data)
    if text is None:
        raise ProposalError("目标文件不是 UTF-8")
    return text


def preview(vault: Path, sec: Section) -> tuple[str | None, str | None]:
    """返回 (应用补丁后的内容, 冲突或错误说明)。不写盘。"""
    try:
        old = _read_target(vault, sec)
        return apply_diff(old, sec), None
    except ProposalError as e:
        return None, str(e)
    except OSError:
        return None, "读取目标文件失败"


def _validate_target(rel: str, text: str) -> None:
    _, errors = fmt.validate_note(rel, text)
    if errors:
        raise ProposalError("格式校验失败: " + "; ".join(i.detail for i in errors))
    findings = _SCANNER.scan({rel: text})
    if findings:
        rules = ", ".join(sorted({f.rule for f in findings}))
        raise ProposalError(f"内容疑似包含密钥（{rules}），拒绝写入")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.amem-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _mark_status(text: str, sec: Section, status: str) -> str:
    lines = text.split("\n")
    if sec.heading >= len(lines):
        raise ProposalError("提案内容已变化，请刷新后重试")
    lines[sec.heading] = _SECTION_RE.sub(
        lambda m: f"## {m.group(1)}. {m.group(2)} `{m.group(3)}` · {status}",
        lines[sec.heading])
    return "\n".join(lines)


def _commit(vault: Path, message: str, paths: list[str]) -> None:
    """写回已经完成，提交失败不算失败；autocommit 会兜底。"""
    try:
        subprocess.run(["git", "-C", str(vault), "add", "--", *paths],
                       capture_output=True, timeout=10)
        subprocess.run(["git", "-C", str(vault), "commit", "-q", "-m", message, "--", *paths],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _resolve_proposal(vault: Path, name: str) -> Path:
    if not _clean_name(name):
        raise ProposalNotFound("提案不存在")
    root = vault / "proposals"
    if root.is_symlink():
        raise ProposalNotFound("提案不存在")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ProposalNotFound("提案不存在")
    return path


def decide(vault: Path, name: str, index: int, action: str) -> Result:
    """批准或驳回提案里的某一条。写盘前先复核补丁能干净应用。"""
    vault = Path(vault)
    if action not in ("accept", "reject"):
        raise ProposalError("未知操作")
    with _LOCK:
        path = _resolve_proposal(vault, name)
        try:
            text = fmt.decode(path.read_bytes())
        except OSError:
            raise ProposalNotFound("提案无法读取") from None
        if text is None:
            raise ProposalError("提案不是 UTF-8")
        sections = parse_sections(text)
        sec = next((s for s in sections if s.index == index), None)
        if sec is None:
            raise ProposalNotFound("提案里没有这一条")
        if sec.status != PENDING:
            raise ProposalConflict("这条已经处理过")
        rel_proposal = posixpath.join("proposals", name)

        if action == "reject":
            _write(path, _mark_status(text, sec, REJECTED).encode("utf-8"))
            _commit(vault, f"web: 用户驳回提案 {name} #{index}", [rel_proposal])
            return Result("reject", name, index, sec.target)

        target = _target_path(vault, sec.target)
        old = _read_target(vault, sec)
        old_bytes = target.read_bytes() if old is not None else None
        new_text = apply_diff(old, sec)
        if len(new_text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ProposalError(f"新内容超过 {MAX_FILE_BYTES} 字节")
        _validate_target(sec.target, new_text)

        _write(target, new_text.encode("utf-8"))
        try:
            _write(path, _mark_status(text, sec, ACCEPTED).encode("utf-8"))
        except OSError:
            # 提案没标上就回滚目标文件，保证还能重试。
            if old_bytes is None:
                try:
                    target.unlink()
                except OSError:
                    pass
            else:
                _write(target, old_bytes)
            raise
        _commit(vault, f"web: 用户批准提案 {name} #{index} {sec.target}",
                [sec.target, rel_proposal])
        return Result("accept", name, index, sec.target)
