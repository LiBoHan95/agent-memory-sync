"""记忆文件格式：frontmatter 解析与校验、vault 读取、log 追加。

frontmatter 只支持扁平的 `key: value`（不支持嵌套 YAML），规则见 vault 里的 FORMAT.md。
所有读写 vault 的代码都应通过本模块，保证格式一致。
"""
from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 1
MEMORY_FILE = "MEMORY.md"
MEMORY_MAX_LINES = 200
REQUIRED_FIELDS = (
    "schema_version", "name", "description", "type", "status", "source", "created", "updated",
)
NOTE_TYPES = frozenset({"profile", "preference", "rule", "project", "reference"})
STATUSES = frozenset({"active", "superseded"})
LOG_TAGS = frozenset({"preference", "correction", "fact", "candidate-rule", "project"})
NOTE_DIRS = ("profile", "rules", "projects")
IGNORED_DIRS = frozenset({".git", ".stfolder", ".stversions"})
IGNORED_FILES = frozenset({".DS_Store"})

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:[|#][^\[\]]*)?\]\]")
_MDLINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class FormatError(ValueError):
    """frontmatter 无法解析。"""


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    detail: str
    severity: str = "error"  # error | warning

    def key(self) -> tuple[str, str, str]:
        return (self.path, self.code, self.detail)

    def __str__(self) -> str:
        return f"{self.path}: [{self.code}] {self.detail}"


# ---------- frontmatter ----------

def parse_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """返回 (meta, body)。没有 frontmatter 时 meta 为 None；格式错误抛 FormatError。"""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        raise FormatError("frontmatter 没有闭合的 ---")
    meta: dict[str, str] = {}
    for lineno, raw in enumerate(lines[1:end], start=2):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] in " \t-":
            raise FormatError(f"第 {lineno} 行：不支持嵌套或列表，只能写 key: value")
        m = _KEY_RE.match(raw.rstrip())
        if not m:
            raise FormatError(f"第 {lineno} 行：无法解析 {raw.strip()!r}")
        key, value = m.group(1), (m.group(2) or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in meta:
            raise FormatError(f"第 {lineno} 行：字段 {key} 重复")
        meta[key] = value
    return meta, "\n".join(lines[end + 1:])


def is_sync_conflict(rel: str) -> bool:
    return ".sync-conflict-" in posixpath.basename(rel)


def is_note_path(rel: str) -> bool:
    parts = rel.split("/")
    return (
        len(parts) >= 2 and parts[0] in NOTE_DIRS
        and rel.endswith(".md") and not is_sync_conflict(rel)
    )


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_note(rel: str, text: str) -> tuple[dict[str, str] | None, list[Issue]]:
    """校验单个笔记文件（profile/ rules/ projects/ 下的 .md）。只看文件本身，不看引用。"""
    issues: list[Issue] = []
    try:
        meta, _ = parse_frontmatter(text)
    except FormatError as e:
        return None, [Issue(rel, "frontmatter-invalid", str(e))]
    if meta is None:
        return None, [Issue(rel, "frontmatter-missing", "笔记必须有 frontmatter")]
    for f in REQUIRED_FIELDS:
        if not meta.get(f):
            issues.append(Issue(rel, "field-missing", f"缺少字段 {f}"))
    sv = meta.get("schema_version", "")
    if sv and sv != str(SCHEMA_VERSION):
        issues.append(Issue(rel, "schema-version", f"schema_version={sv}，当前只支持 {SCHEMA_VERSION}"))
    if meta.get("type") and meta["type"] not in NOTE_TYPES:
        issues.append(Issue(rel, "type-invalid", f"type={meta['type']}"))
    status = meta.get("status", "")
    if status and status not in STATUSES:
        issues.append(Issue(rel, "status-invalid", f"status={status}"))
    if status == "superseded" and not meta.get("superseded_by"):
        issues.append(Issue(rel, "superseded-by-missing", "status=superseded 时必须填 superseded_by"))
    for f in ("created", "updated"):
        if meta.get(f) and not _valid_date(meta[f]):
            issues.append(Issue(rel, "date-invalid", f"{f}={meta[f]}，应为 YYYY-MM-DD"))
    if meta.get("name") and not _IDENT_RE.match(meta["name"]):
        issues.append(Issue(rel, "name-invalid", f"name={meta['name']!r} 只能含字母数字 . _ -"))
    if meta.get("source") and "@" not in meta["source"]:
        issues.append(Issue(rel, "source-invalid", f"source={meta['source']!r} 应为 agent@设备"))
    return meta, issues


# ---------- vault ----------

def iter_files(root: Path) -> dict[str, bytes]:
    """返回 vault 内所有文件 {相对路径(posix): 内容}，跳过 .git 等同步/版本目录。
    符号链接不跟随，作为一个特殊条目记录（闸门会拒绝）。"""
    out: dict[str, bytes] = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                rel = Path(dirpath, d).relative_to(root).as_posix()
                out[rel] = SYMLINK_MARK
                dirnames.remove(d)
        for fn in sorted(filenames):
            if fn in IGNORED_FILES:
                continue
            p = Path(dirpath, fn)
            rel = p.relative_to(root).as_posix()
            out[rel] = SYMLINK_MARK if p.is_symlink() else p.read_bytes()
    return out


SYMLINK_MARK = b"\x00amem-symlink\x00"


def decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_links(body: str) -> tuple[list[str], list[str]]:
    """返回 ([[name]] 列表, 相对路径 markdown 链接列表)。跳过 http(s)/mailto/锚点和代码块。"""
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"`[^`\n]*`", "", body)
    wikis = [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]
    paths = []
    for m in _MDLINK_RE.finditer(body):
        target = m.group(1).split("#", 1)[0]
        if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue
        paths.append(target)
    return wikis, paths


def _resolve(base_rel: str, target: str) -> str | None:
    """把相对链接解析为 vault 内路径；越出 vault 返回 None。"""
    if target.startswith("/") or target.startswith("~"):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_rel), target))
    if joined == ".." or joined.startswith("../"):
        return None
    return joined


def validate_vault(files: Mapping[str, bytes]) -> list[Issue]:
    """校验整个 vault（任意 {相对路径: 内容} 映射，可以是磁盘上的，也可以是闸门里的候选状态）。
    error 级问题会让闸门拒绝变更；warning 只进报告。"""
    issues: list[Issue] = []
    notes: dict[str, dict[str, str]] = {}  # name -> meta
    note_paths: dict[str, str] = {}        # name -> rel
    texts: dict[str, str] = {}
    for rel, data in files.items():
        if not rel.endswith(".md"):
            continue
        text = decode(data)
        if text is None:
            issues.append(Issue(rel, "not-utf8", "Markdown 文件必须是 UTF-8"))
            continue
        texts[rel] = text
        if is_sync_conflict(rel):
            issues.append(Issue(rel, "sync-conflict", "Syncthing 冲突副本，需要人工合并", "warning"))
            continue
        if not is_note_path(rel):
            continue
        meta, errs = validate_note(rel, text)
        issues.extend(errs)
        if meta and meta.get("name"):
            name = meta["name"]
            if name in note_paths:
                issues.append(Issue(rel, "name-duplicate", f"name={name} 与 {note_paths[name]} 重复"))
            else:
                notes[name], note_paths[name] = meta, rel

    for name, meta in notes.items():
        target = meta.get("superseded_by")
        if target and target not in notes:
            issues.append(Issue(note_paths[name], "superseded-by-broken", f"superseded_by={target} 不存在"))
        elif target == name:
            issues.append(Issue(note_paths[name], "superseded-by-self", "superseded_by 指向自己"))

    for rel, text in texts.items():
        if rel.startswith("log/") or is_sync_conflict(rel):
            continue
        _, body = _safe_body(text)
        wikis, paths = extract_links(body)
        for w in wikis:
            if w not in notes:
                issues.append(Issue(rel, "link-broken", f"[[{w}]] 不存在", "warning"))
        for p in paths:
            resolved = _resolve(rel, p)
            if resolved is None:
                issues.append(Issue(rel, "link-outside", f"链接 {p} 指向 vault 之外"))
            elif resolved not in files:
                sev = "error" if rel == MEMORY_FILE else "warning"
                issues.append(Issue(rel, "link-broken", f"链接 {p} 不存在", sev))

    if MEMORY_FILE not in files:
        issues.append(Issue(MEMORY_FILE, "memory-missing", "缺少索引文件"))
    elif MEMORY_FILE in texts:
        n = texts[MEMORY_FILE].count("\n") + (0 if texts[MEMORY_FILE].endswith("\n") else 1)
        if n > MEMORY_MAX_LINES:
            issues.append(Issue(MEMORY_FILE, "memory-too-long", f"{n} 行，上限 {MEMORY_MAX_LINES}"))
    return issues


def _safe_body(text: str) -> tuple[dict[str, str] | None, str]:
    try:
        return parse_frontmatter(text)
    except FormatError:
        return None, text


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


# ---------- log ----------

_LOG_NAME_RE = re.compile(r"^log/(\d{4}-\d{2})\.([A-Za-z0-9][A-Za-z0-9._-]*)\.md$")
_LOG_HEAD_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (\S+@\S+)\s*$")
_LOG_ITEM_RE = re.compile(r"^- \[([a-z-]+)\] (.+)$")


@dataclass(frozen=True)
class LogItem:
    file: str
    when: str     # "YYYY-MM-DD HH:MM"
    source: str   # agent@设备
    tag: str
    text: str


def log_path(device: str, when: datetime | None = None) -> str:
    if not _IDENT_RE.match(device):
        raise ValueError(f"设备名不合法: {device!r}")
    when = when or datetime.now()
    return f"log/{when:%Y-%m}.{device}.md"


def is_log_path(rel: str) -> bool:
    return bool(_LOG_NAME_RE.match(rel))


def format_log_entry(source: str, items: list[tuple[str, str]], when: datetime | None = None) -> str:
    """items: [(tag, text)]。text 里的换行会被压成空格，保证一条一行。"""
    if "@" not in source or any(c.isspace() for c in source):
        raise ValueError(f"source 应为 agent@设备: {source!r}")
    if not items:
        raise ValueError("至少一条")
    when = when or datetime.now()
    lines = [f"## {when:%Y-%m-%d %H:%M} · {source}"]
    for tag, text in items:
        if tag not in LOG_TAGS:
            raise ValueError(f"未知标签: {tag}")
        text = " ".join(text.split())
        if not text:
            raise ValueError("内容为空")
        lines.append(f"- [{tag}] {text}")
    return "\n".join(lines) + "\n"


def append_log(vault: Path, device: str, source: str, items: list[tuple[str, str]],
               when: datetime | None = None) -> str:
    """追加一段 log，返回相对路径。只用追加模式打开，带文件锁。"""
    import fcntl
    when = when or datetime.now()
    rel = log_path(device, when)
    entry = format_log_entry(source, items, when)
    p = Path(vault, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            prefix = ""
            if size:
                f.seek(size - 1)
                prefix = "\n" if f.read(1) == "\n" else "\n\n"
                f.seek(0, os.SEEK_END)
            f.write(prefix + entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return rel


def parse_log(rel: str, text: str) -> tuple[list[LogItem], list[Issue]]:
    """解析 log 文件。格式不对的行记 warning，不丢弃整份文件。"""
    items: list[LogItem] = []
    issues: list[Issue] = []
    head: tuple[str, str] | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if m := _LOG_HEAD_RE.match(line):
            head = (m.group(1), m.group(2))
        elif (m := _LOG_ITEM_RE.match(line)) and head:
            tag = m.group(1)
            if tag not in LOG_TAGS:
                issues.append(Issue(rel, "log-tag", f"第 {lineno} 行未知标签 {tag}", "warning"))
            items.append(LogItem(rel, head[0], head[1], tag, m.group(2).strip()))
        elif line.startswith("#") and not line.startswith("## "):
            continue  # 文件标题
        else:
            issues.append(Issue(rel, "log-line", f"第 {lineno} 行格式不对: {line[:60]!r}", "warning"))
    return items, issues
