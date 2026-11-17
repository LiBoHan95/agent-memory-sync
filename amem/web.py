"""服务器 Web：查看各设备活动、转录归档状态和记忆文件。

默认只读；唯一的写路径是「批准 / 驳回提案」：把 proposals/ 里的补丁应用到
profile/ 与 rules/ 下的目标文件。所有路由只接受 GET/HEAD，提案决定用 POST。
默认只监听 127.0.0.1，通过 Tailscale 或 SSH 隧道访问。

用法:
    python3 -m amem.web --vault ~/memory-vault --archive ~/transcript-archive
"""
from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import os
import posixpath
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import format as fmt
from . import proposals as propmod
from .render import render_markdown

MAX_FILE_BYTES = 512 * 1024
MAX_DIFF_BYTES = 256 * 1024
ARCHIVE_CACHE_SECONDS = 60
HTML_TYPE = "text/html; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"

_LOG_DEVICE_RE = re.compile(r"^log/\d{4}-\d{2}\.(.+)\.md$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_DECISION_RE = re.compile(r"^/proposals/([^/]+)/decision$")
_PROPOSAL_STATUSES = frozenset({"pending", "accepted", "rejected"})
_COMMIT_FIELDS = 5


@dataclass(frozen=True)
class WebConfig:
    vault: Path
    archive: Path
    reports: Path
    host: str = "127.0.0.1"
    port: int = 8787
    token: str | None = None

    @classmethod
    def from_env(cls) -> "WebConfig":
        vault = Path(os.environ.get("AMEM_VAULT", "~/memory-vault")).expanduser()
        reports_env = os.environ.get("AMEM_REPORTS")
        return cls(
            vault=vault,
            archive=Path(os.environ.get("AMEM_ARCHIVE", "~/transcript-archive")).expanduser(),
            reports=Path(reports_env).expanduser() if reports_env else vault.parent / "dream-reports",
            host=(os.environ.get("AMEM_WEB_HOST", "").strip() or "127.0.0.1"),
            port=int(os.environ.get("AMEM_WEB_PORT", "8787")),
            token=os.environ.get("AMEM_WEB_TOKEN") or None,
        )


@dataclass
class Note:
    rel: str
    meta: dict[str, str]
    body: str

    @property
    def title(self) -> str:
        return _first_heading(self.body) or self.meta.get("description") or Path(self.rel).stem


@dataclass
class ArchiveStat:
    agent: str
    files: int = 0
    size: int = 0
    latest: float | None = None


@dataclass
class Device:
    name: str
    logs: list[fmt.LogItem] = field(default_factory=list)
    archive: list[ArchiveStat] = field(default_factory=list)

    @property
    def last_log(self) -> str:
        return max((i.when for i in self.logs), default="")

    @property
    def archive_files(self) -> int:
        return sum(a.files for a in self.archive)

    @property
    def archive_size(self) -> int:
        return sum(a.size for a in self.archive)

    @property
    def archive_latest(self) -> float | None:
        values = [a.latest for a in self.archive if a.latest is not None]
        return max(values) if values else None

    @property
    def last_seen(self) -> str:
        return self.last_log or _fmt_ts(self.archive_latest)


@dataclass
class ReportFile:
    name: str
    mtime: float
    size: int


@dataclass
class Proposal:
    name: str
    title: str
    pending: int
    total: int
    mtime: float
    items: list[str] = field(default_factory=list)


@dataclass
class Commit:
    full: str
    short: str
    author: str
    date: str
    subject: str


# ---------- 数据加载 ----------

_archive_cache: dict[str, tuple[float, dict[str, list[ArchiveStat]]]] = {}
_archive_lock = threading.Lock()
_issues_cache: dict[str, tuple[float, tuple[int, int, bool]]] = {}
_issues_lock = threading.Lock()


def clear_caches() -> None:
    with _archive_lock:
        _archive_cache.clear()
    with _issues_lock:
        _issues_cache.clear()


def _decode_prefix(data: bytes, *, may_be_truncated: bool = False) -> str | None:
    """解码 UTF-8；只有确实因字节上限被截断时，才裁掉末尾最多 3 个残字节。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if not may_be_truncated:
            return None
    for cut in range(1, 4):
        if cut >= len(data):
            break
        try:
            return data[:-cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    if path.is_symlink():
        return None
    try:
        with path.open("rb") as fh:
            data = fh.read(limit)
            truncated = bool(fh.read(1))
    except OSError:
        return None
    return _decode_prefix(data, may_be_truncated=truncated)


def _read_text_tail(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    """读文件尾部。log 是追加写，最新条目在末尾，不能只读前 limit 字节。"""
    if path.is_symlink():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            truncated = size > limit
            if truncated:
                start = size - limit
                fh.seek(start)
                data = fh.read(limit)
                # 只有起点落在行中间时才丢弃第一段；恰好落在行首则保留整行。
                fh.seek(start - 1)
                if fh.read(1) != b"\n":
                    cut = data.find(b"\n")
                    data = data[cut + 1:] if cut >= 0 else b""
            else:
                fh.seek(0)
                data = fh.read()
    except OSError:
        return None
    return _decode_prefix(data, may_be_truncated=truncated)


def _is_truncated(path: Path, limit: int = MAX_FILE_BYTES) -> bool:
    try:
        return not path.is_symlink() and path.stat().st_size > limit
    except OSError:
        return False


def _truncation_note(path: Path) -> str:
    if not _is_truncated(path):
        return ""
    return (f'<p class="sub">文件超过 {MAX_FILE_BYTES // 1024} KB，'
            "只显示开头部分。</p>")


def _has_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


_STATUS_LABELS = {
    "active": "生效", "draft": "草稿", "superseded": "已替代",
    "archived": "已归档", "pending": "待处理",
    "accepted": "已接受", "rejected": "已拒绝",
}
_META_LABELS = {
    "name": "名称", "description": "说明", "status": "状态",
    "updated": "更新时间", "superseded_by": "替代为", "source": "来源",
    "tags": "标签",
}


def _status_label(value: str) -> str:
    return _STATUS_LABELS.get(value.strip().lower(), value)


def _walk_md(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".")
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if name.endswith(".md") and not name.startswith("."):
                yield Path(dirpath, name)


def load_notes(vault: Path) -> dict[str, Note]:
    notes: dict[str, Note] = {}
    for top in ("profile", "rules", "projects"):
        root = vault / top
        if root.is_symlink() or not root.is_dir():
            continue
        for path in _walk_md(root):
            rel = path.relative_to(vault).as_posix()
            if not fmt.is_note_path(rel):
                continue
            text = _read_text(path)
            if text is None:
                continue
            try:
                meta, body = fmt.parse_frontmatter(text)
            except fmt.FormatError:
                meta, body = None, text
            notes[rel] = Note(rel=rel, meta=meta or {}, body=body)
    return notes


def load_memory(vault: Path) -> str:
    return _read_text(vault / fmt.MEMORY_FILE) or ""


def load_logs(vault: Path) -> dict[str, list[fmt.LogItem]]:
    out: dict[str, list[fmt.LogItem]] = {}
    root = vault / "log"
    if root.is_symlink() or not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(dirpath, name)
            rel = path.relative_to(vault).as_posix()
            if not fmt.is_log_path(rel) or fmt.is_sync_conflict(rel):
                continue
            m = _LOG_DEVICE_RE.match(rel)
            if not m:
                continue
            text = _read_text_tail(path)
            if text is None:
                continue
            items, _ = fmt.parse_log(rel, text)
            out.setdefault(m.group(1), []).extend(items)
    for items in out.values():
        items.sort(key=lambda i: i.when, reverse=True)
    return out


def _scan_archive(archive: Path) -> dict[str, list[ArchiveStat]]:
    out: dict[str, list[ArchiveStat]] = {}
    if not archive.is_dir():
        return out
    try:
        devices = sorted(archive.iterdir())
    except OSError:
        return out
    for device in devices:
        if device.is_symlink() or not device.is_dir() or not _DEVICE_RE.match(device.name):
            continue
        stats: list[ArchiveStat] = []
        try:
            agents = sorted(device.iterdir())
        except OSError:
            continue
        for agent in agents:
            if agent.is_symlink() or not agent.is_dir():
                continue
            stat = ArchiveStat(agent.name)
            for dirpath, dirnames, filenames in os.walk(agent, followlinks=False):
                dirnames[:] = [d for d in dirnames
                               if not os.path.islink(os.path.join(dirpath, d))]
                for name in filenames:
                    path = Path(dirpath, name)
                    if path.is_symlink():
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    stat.files += 1
                    stat.size += st.st_size
                    if stat.latest is None or st.st_mtime > stat.latest:
                        stat.latest = st.st_mtime
            stats.append(stat)
        out[device.name] = stats
    return out


def load_archive(archive: Path) -> dict[str, list[ArchiveStat]]:
    key = str(archive)
    now = time.time()
    with _archive_lock:
        cached = _archive_cache.get(key)
        if cached and now - cached[0] < ARCHIVE_CACHE_SECONDS:
            return cached[1]
    result = _scan_archive(archive)
    with _archive_lock:
        _archive_cache[key] = (now, result)
    return result


def build_devices(logs: dict[str, list[fmt.LogItem]],
                  archive: dict[str, list[ArchiveStat]]) -> list[Device]:
    devices: dict[str, Device] = {}
    for name, items in logs.items():
        devices.setdefault(name, Device(name)).logs = items
    for name, stats in archive.items():
        devices.setdefault(name, Device(name)).archive = stats

    def sort_key(device: Device):
        return (device.last_seen, device.archive_latest or 0, device.name)

    return sorted(devices.values(), key=sort_key, reverse=True)


def load_reports(reports: Path, limit: int = 100) -> list[ReportFile]:
    if not reports.is_dir():
        return []
    out: list[ReportFile] = []
    try:
        paths = list(reports.iterdir())
    except OSError:
        return out
    for path in paths:
        if (path.is_symlink() or not path.is_file() or path.suffix != ".md"
                or _is_hidden_name(path.name)):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        out.append(ReportFile(path.name, st.st_mtime, st.st_size))
    out.sort(key=lambda r: (r.mtime, r.name), reverse=True)
    return out[:limit]


def _proposal_marks(text: str) -> list[str]:
    """只认标题行末尾的 `· pending/accepted/rejected`，正文提及不算。"""
    marks = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.startswith("#") or "·" not in line:
            continue
        status = line.rsplit("·", 1)[1].strip().lower()
        if status in _PROPOSAL_STATUSES:
            marks.append(status)
    return marks


def load_proposals(vault: Path) -> list[Proposal]:
    root = vault / "proposals"
    if root.is_symlink() or not root.is_dir():
        return []
    out: list[Proposal] = []
    try:
        paths = sorted(root.iterdir())
    except OSError:
        return out
    for path in paths:
        if (path.is_symlink() or not path.is_file() or path.suffix != ".md"
                or _is_hidden_name(path.name) or fmt.is_sync_conflict(path.name)):
            continue
        text = _read_text(path) or ""
        marks = _proposal_marks(text)
        pending = marks.count("pending") if marks else 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        sections = propmod.parse_sections(text)
        out.append(Proposal(path.name, _first_heading(text) or path.stem,
                            pending, len(marks) or 1, mtime,
                            [propmod.section_summary(s) for s in sections]))
    out.sort(key=lambda p: (p.mtime, p.name), reverse=True)
    return out


def load_state(vault: Path) -> dict:
    path = vault / ".dream-state"
    if path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vault_issues(vault: Path) -> tuple[int, int, bool]:
    """返回 (错误数, 警告数, 是否成功校验)；结果缓存 60 秒。"""
    key = str(vault.resolve())
    now = time.monotonic()
    with _issues_lock:
        cached = _issues_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    try:
        # 只校验 512 KB 以内的文件，避免超大 log 拖慢仪表盘。
        issues = fmt.validate_vault(fmt.iter_files(vault, max_bytes=MAX_FILE_BYTES))
    except OSError:
        result = (0, 0, False)
    else:
        errors = len(fmt.errors(issues))
        result = (errors, len(issues) - errors, True)
    with _issues_lock:
        _issues_cache[key] = (now + ARCHIVE_CACHE_SECONDS, result)
    return result


class GitTimeout(Exception):
    """git 命令超时（与提交不存在区分）。"""


def _git(vault: Path, *args: str, timeout: int = 10,
         raise_on_timeout: bool = False) -> str | None:
    try:
        result = subprocess.run(["git", "--no-pager", "-C", str(vault), *args],
                                capture_output=True, text=True, errors="replace",
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        if raise_on_timeout:
            raise GitTimeout() from None
        return None
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def load_commits(vault: Path, limit: int = 50) -> list[Commit]:
    if not (vault / ".git").exists():
        return []
    out = _git(vault, "log", "-n", str(limit), "--date=iso-strict",
               "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s")
    if not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == _COMMIT_FIELDS:
            commits.append(Commit(*parts))
    return commits


def load_commit(vault: Path, rev: str) -> tuple[str, bool] | None:
    if not _COMMIT_RE.match(rev or ""):
        return None
    out = _git(vault, "show", "--no-color", "--format=medium", "--stat", "--patch", rev,
               raise_on_timeout=True)
    if out is None:
        return None
    raw = out.encode("utf-8")
    if len(raw) > MAX_DIFF_BYTES:
        return raw[:MAX_DIFF_BYTES].decode("utf-8", "ignore"), True
    return out, False


# ---------- 格式化 ----------

def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _same_secret(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return ""
        rest = value[end + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        value = value[1:end]
    elif value.count(":") == 1:
        host, port = value.split(":", 1)
        if port and not port.isdigit():
            return ""
        value = host
    return value.rstrip(".")


def _is_loopback_host(value: str) -> bool:
    name = _host_name(value)
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        pass
    # 浏览器接受 127.1 这类 IPv4 简写；严格按 1-4 段、每段 0-255 解析，
    # 不接受 127.999 或 127.0.0.0.1 这类非法地址。
    parts = name.split(".")
    if not 2 <= len(parts) <= 4 or parts[0] != "127":
        return False
    for part in parts:
        if not part.isdigit() or (len(part) > 1 and part[0] == "0"):
            return False
        if int(part) > 255:
            return False
    return True


def _is_loopback_origin(value: str) -> bool:
    """POST 的 Origin 必须是本机页面；没有令牌时用它防跨站提交。"""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and _is_loopback_host(parsed.hostname))


def _valid_token(token: str) -> bool:
    # RFC 6265 cookie-octet：保证各种浏览器都能原样回传 Set-Cookie 的值。
    for ch in token:
        code = ord(ch)
        if not (code == 0x21 or 0x23 <= code <= 0x2B or 0x2D <= code <= 0x3A
                or 0x3C <= code <= 0x5B or 0x5D <= code <= 0x7E):
            return False
    return True


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _href(*parts: str) -> str:
    return "/" + "/".join(quote(p, safe="/") for p in parts)


# ---------- 页面 ----------

_CSS = """
:root{
  --bg:#f6f5f1; --panel:#ffffff; --ink:#1f2328; --muted:#6b7280; --line:#d6d3cc;
  --accent:#2563eb; --tag:#0f8a80; --warn:#a16207; --bad:#b91c1c; --code:#f0efea;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#16171a; --panel:#1f2125; --ink:#e7e7e4; --muted:#9aa0a8; --line:#3a3d43;
    --accent:#6b9cff; --tag:#3cc4b7; --warn:#facc15; --bad:#f87171; --code:#26282d;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.6}
header{background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
.wrap{max-width:1100px;margin:0 auto;padding:0 20px}
header .wrap{display:flex;align-items:center;gap:24px;min-height:54px;flex-wrap:wrap}
.brand{font-weight:700;color:var(--ink);text-decoration:none;font-size:16px}
nav{display:flex;gap:16px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;font-size:14px;padding:4px 0;border-bottom:2px solid transparent}
nav a:hover{color:var(--ink)}
nav a.on{color:var(--accent);border-bottom-color:var(--accent)}
main{max-width:1100px;margin:0 auto;padding:28px 20px 52px}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:17px;margin:28px 0 10px}
h3{font-size:15px;margin:20px 0 8px}
.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px;min-width:0}
.card .num{font-size:24px;font-weight:700;line-height:1.3;overflow-wrap:anywhere;
  font-variant-numeric:tabular-nums}
.card .label{color:var(--muted);font-size:13px;margin-top:4px}
.statusline{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:12px;padding:10px 14px;
  color:var(--muted);font-size:13px;background:var(--panel);border:1px solid var(--line);
  border-radius:10px}
.statusline strong{color:var(--ink);font-weight:600}
@media (max-width:760px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:460px){.cards{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}
tr:last-child td{border-bottom:0}
a{color:var(--accent)}
.muted{color:var(--muted)}
.badge{display:inline-block;font-size:12px;padding:1px 7px;border-radius:99px;
  border:1px solid var(--line);color:var(--muted);margin-left:6px;vertical-align:1px}
.badge.pending{color:var(--warn);border-color:var(--warn)}
.badge.active{color:var(--tag);border-color:var(--tag)}
.badge.bad{color:var(--bad);border-color:var(--bad)}
.tag{display:inline-block;font-size:12px;padding:1px 6px;border-radius:4px;
  background:var(--code);color:var(--tag);margin-right:6px}
pre{background:var(--code);border-radius:8px;padding:14px 16px;overflow:auto;
  font-size:13px;line-height:1.5}
pre.raw{white-space:pre-wrap;word-break:break-word}
code{background:var(--code);border-radius:4px;padding:1px 5px;font-size:0.92em}
pre code{background:none;padding:0}
blockquote{border-left:3px solid var(--line);margin:12px 0;padding:2px 14px;color:var(--muted)}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
ul,ol{margin:8px 0;padding-left:24px}
li{margin:3px 0}
.meta th{width:150px;color:var(--muted);font-weight:600}
.empty{color:var(--muted);padding:10px 0}
.notice{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--warn);
  border-radius:8px;padding:10px 14px;color:var(--muted);font-size:14px}
.notice.bad{border-left-color:var(--bad);color:var(--bad)}
.flash{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--tag);
  border-radius:8px;padding:10px 14px;font-size:14px}
.proposal{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin:16px 0}
.proposal .head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.proposal .head strong{font-size:15px}
.proposal .proposal-body{margin-top:12px;border-top:1px solid var(--line);padding-top:6px}
.proposal .acts{display:flex;gap:10px;margin-top:16px;padding-top:14px;
  border-top:1px solid var(--line)}
.proposal button{padding:7px 16px;border:1px solid var(--line);border-radius:6px;
  background:var(--bg);color:var(--ink);font-size:14px;cursor:pointer;font-family:inherit}
.proposal button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.proposal button.danger{color:var(--bad);border-color:var(--bad)}
pre.diff{white-space:pre-wrap;word-break:break-word;margin-top:8px}
details{margin-top:12px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
footer{max-width:1100px;margin:0 auto;padding:0 20px 30px;color:var(--muted);font-size:12px}
form.token{max-width:340px;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:20px}
form.token input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;
  background:var(--bg);color:var(--ink);margin:8px 0}
form.token button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;
  background:var(--accent);color:#fff;cursor:pointer}
"""

_NAV = (("home", "/", "总览"), ("memory", "/memory", "记忆"),
        ("devices", "/devices", "设备"), ("history", "/history", "历史"),
        ("reports", "/reports", "报告"), ("proposals", "/proposals", "提案"))


def _page(title: str, body: str, active: str = "") -> str:
    links = []
    for key, href, label in _NAV:
        cls = ' class="on"' if key == active else ""
        links.append(f'<a{cls} href="{href}">{label}</a>')
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)} · 记忆库</title><style>{_CSS}</style></head><body>"
        f"<header><div class=\"wrap\"><a class=\"brand\" href=\"/\">记忆库</a><nav>{''.join(links)}</nav></div></header>"
        f"<main>{body}</main>"
        "<footer>记忆库 · 本地视图</footer></body></html>"
    )


def _empty(message: str) -> str:
    return f'<p class="empty">{_esc(message)}</p>'


def _table(headers: list[str], rows: list[str]) -> str:
    if not rows:
        return _empty("暂无数据")
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


class WebApp:
    def __init__(self, cfg: WebConfig):
        self.cfg = cfg
        self.root = cfg.vault.resolve()

    # ----- 路径解析 -----

    def _has_symlink_component(self, rel: str) -> bool:
        current = self.root
        for part in rel.split("/"):
            current = current / part
            if current.is_symlink():
                return True
        return False

    def resolve_note(self, rel: str) -> Path | None:
        parts = rel.split("/")
        if (not rel or rel.startswith("/") or _has_control(rel)
                or ".." in parts or any(p.startswith(".") for p in parts)):
            return None
        if not rel.endswith(".md") or fmt.is_sync_conflict(rel):
            return None
        if rel != fmt.MEMORY_FILE and rel.split("/", 1)[0] not in ("profile", "rules", "projects"):
            return None
        raw = self.root / rel
        if (self._has_symlink_component(rel) or not raw.is_file()):
            return None
        path = raw.resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        return path

    def note_href(self, rel: str) -> str | None:
        if self.resolve_note(rel) is None:
            return None
        return _href("memory", rel)

    def resolve_proposal(self, name: str) -> Path | None:
        if not name or _has_control(name) or name != posixpath.basename(name):
            return None
        if not name.endswith(".md") or name.startswith("."):
            return None
        if fmt.is_sync_conflict(name):
            return None
        root = self.root / "proposals"
        if root.is_symlink():
            return None
        path = root / name
        if path.is_symlink() or not path.is_file():
            return None
        return path

    def resolve_report(self, name: str) -> Path | None:
        if not name or _has_control(name) or name != posixpath.basename(name):
            return None
        if not name.endswith(".md") or name.startswith("."):
            return None
        path = self.cfg.reports / name
        if path.is_symlink() or not path.is_file():
            return None
        return path

    # ----- 路由 -----

    def route(self, path: str, query: dict[str, list[str]]):
        if path != "/":
            path = path.rstrip("/") or "/"
        if path == "/":
            return 200, HTML_TYPE, self.dashboard()
        if path == "/healthz":
            return 200, TEXT_TYPE, "ok\n"
        if path == "/memory":
            return 200, HTML_TYPE, self.memory_index()
        if path.startswith("/memory/"):
            return self.note_page(path[len("/memory/"):], query)
        if path == "/devices":
            return 200, HTML_TYPE, self.devices_page()
        if path.startswith("/devices/"):
            return self.device_page(path[len("/devices/"):])
        if path == "/history":
            return 200, HTML_TYPE, self.history_page()
        if path.startswith("/history/"):
            return self.commit_page(path[len("/history/"):])
        if path == "/reports":
            return 200, HTML_TYPE, self.reports_page()
        if path.startswith("/reports/"):
            return self.report_page(path[len("/reports/"):], query)
        if path == "/proposals":
            return 200, HTML_TYPE, self.proposals_page()
        if path.startswith("/proposals/"):
            return self.proposal_page(path[len("/proposals/"):], query)
        return 404, HTML_TYPE, self.error_page(404, "页面不存在")

    # ----- 页面 -----

    def dashboard(self) -> str:
        notes = load_notes(self.cfg.vault)
        logs = load_logs(self.cfg.vault)
        archive = load_archive(self.cfg.archive)
        devices = build_devices(logs, archive)
        state = load_state(self.cfg.vault)
        proposals = load_proposals(self.cfg.vault)
        commits = load_commits(self.cfg.vault, limit=10)
        reports = load_reports(self.cfg.reports, limit=5)
        errors, warnings, issues_ok = vault_issues(self.cfg.vault)
        pending = sum(p.pending for p in proposals)
        total_files = sum(d.archive_files for d in devices)
        total_size = sum(d.archive_size for d in devices)
        issue_text = (f"{errors} 错误 / {warnings} 警告" if issues_ok
                      else "校验失败/不可读")

        cards = [
            (len(notes), "记忆笔记"),
            (pending, "待处理提案"),
            (state.get("last_run") or "尚未运行", "最近整理"),
            (f"{len(devices)} 台 / {total_files} 个文件", "设备与归档"),
        ]
        card_html = "".join(
            f'<div class="card"><div class="num">{_esc(value)}</div>'
            f'<div class="label">{_esc(label)}</div></div>'
            for value, label in cards)
        status_html = (
            '<div class="statusline">'
            f"<span>归档体积 <strong>{_esc(_fmt_bytes(total_size))}</strong></span>"
            f"<span>记忆库校验 <strong>{_esc(issue_text)}</strong></span>"
            "</div>")

        device_rows = []
        for device in devices[:20]:
            device_rows.append(
                "<tr>"
                f'<td><a href="{_href("devices", device.name)}">{_esc(device.name)}</a></td>'
                f'<td>{_esc(device.last_seen or "—")}</td>'
                f"<td>{len(device.logs)}</td>"
                f"<td>{device.archive_files}</td>"
                f"<td>{_fmt_bytes(device.archive_size)}</td>"
                "</tr>")
        device_table = _table(["设备", "最近活动", "活动条目", "归档文件", "归档体积"],
                              device_rows)

        commit_rows = [
            "<tr>"
            f'<td><a href="{_href("history", c.full)}"><code>{_esc(c.short)}</code></a></td>'
            f"<td>{_esc(c.date[:19].replace('T', ' '))}</td>"
            f"<td>{_esc(c.subject)}</td>"
            f"<td>{_esc(c.author)}</td>"
            "</tr>" for c in commits
        ]
        report_rows = [
            "<tr>"
            f'<td><a href="{_href("reports", r.name)}">{_esc(r.name)}</a></td>'
            f"<td>{_esc(_fmt_ts(r.mtime))}</td>"
            f"<td>{_fmt_bytes(r.size)}</td>"
            "</tr>" for r in reports
        ]
        body = (
            "<h1>总览</h1>"
            f'<p class="sub">记忆库：<code>{_esc(self.cfg.vault)}</code> · '
            f'归档：<code>{_esc(self.cfg.archive)}</code></p>'
            f'<div class="cards">{card_html}</div>{status_html}'
            "<h2>设备活动与归档</h2>"
            f'<div class="panel">{device_table}</div>'
            "<h2>最近提交</h2>"
            f'<div class="panel">{_table(["提交", "时间", "说明", "作者"], commit_rows)}</div>'
            "<h2>最近报告</h2>"
            f'<div class="panel">{_table(["报告", "时间", "大小"], report_rows)}</div>'
        )
        return _page("总览", body, "home")

    def memory_index(self) -> str:
        memory_path = self.cfg.vault / fmt.MEMORY_FILE
        memory_text = load_memory(self.cfg.vault)
        memory_html = (
            _truncation_note(memory_path)
            + f'<div class="panel">{self._render_text(memory_text, fmt.MEMORY_FILE)}</div>'
            if memory_text else _empty("缺少记忆索引文件"))
        notes = load_notes(self.cfg.vault)
        groups = {"profile": [], "rules": [], "projects": []}
        for note in sorted(notes.values(), key=lambda n: n.rel):
            top = note.rel.split("/", 1)[0]
            groups.setdefault(top, []).append(note)

        sections = []
        for top, label in (("profile", "画像与偏好"), ("rules", "规则"), ("projects", "项目")):
            items = groups.get(top, [])
            if not items:
                sections.append(f"<h2>{label}</h2>{_empty('暂无')}")
                continue
            rows = []
            for note in items:
                status = note.meta.get("status", "")
                badge = (f'<span class="badge {_esc(status)}">'
                         f'{_esc(_status_label(status))}</span>' if status else "")
                desc = note.meta.get("description", "")
                rows.append(
                    "<tr>"
                    f'<td><a href="{_href("memory", note.rel)}">{_esc(note.title)}</a>{badge}</td>'
                    f"<td>{_esc(desc)}</td>"
                    f"<td>{_esc(note.meta.get('updated', ''))}</td>"
                    "</tr>")
            sections.append(f"<h2>{label}</h2><div class=\"panel\">{_table(['条目', '说明', '更新'], rows)}</div>")

        body = (f'<h1>记忆</h1><p class="sub">记忆索引与画像、规则、项目笔记</p>'
                f"<h2>索引</h2>{memory_html}" + "".join(sections))
        return _page("记忆", body, "memory")

    def note_page(self, rel: str, query: dict[str, list[str]]):
        path = self.resolve_note(rel)
        if path is None:
            return 404, HTML_TYPE, self.error_page(404, "笔记不存在")
        text = _read_text(path)
        if text is None:
            return 404, HTML_TYPE, self.error_page(404, "笔记无法读取")
        rel = path.relative_to(self.root).as_posix()
        raw = (query.get("raw") or [""])[0] in ("1", "true", "yes")
        try:
            meta, body = fmt.parse_frontmatter(text)
        except fmt.FormatError:
            meta, body = None, text
        title = _first_heading(body) or (meta or {}).get("description") or Path(rel).stem
        toggle = (f'<a href="{_href("memory", rel)}">渲染</a>' if raw
                  else f'<a href="{_href("memory", rel)}?raw=1">原文</a>')
        if raw:
            content = f'<pre class="raw">{_esc(text)}</pre>'
        else:
            meta_table = ""
            if meta:
                rows = "".join(
                    f'<tr><th>{_esc(_META_LABELS.get(k, k))}</th>'
                    f'<td>{_esc(_status_label(v) if k == "status" else v)}</td></tr>'
                    for k, v in meta.items())
                meta_table = f'<table class="meta"><tbody>{rows}</tbody></table>'
            content = meta_table + f"<div class=\"panel\">{self._render_text(body, rel)}</div>"
        page = (f'<h1>{_esc(title)}</h1>'
                f'<p class="sub"><code>{_esc(rel)}</code> · <a href="/memory">返回记忆</a> · {toggle}</p>'
                f"{_truncation_note(path)}"
                f"{content}")
        return 200, HTML_TYPE, _page(title, page, "memory")

    def devices_page(self) -> str:
        logs = load_logs(self.cfg.vault)
        archive = load_archive(self.cfg.archive)
        devices = build_devices(logs, archive)
        rows = []
        for device in devices:
            rows.append(
                "<tr>"
                f'<td><a href="{_href("devices", device.name)}">{_esc(device.name)}</a></td>'
                f"<td>{_esc(device.last_log or '—')}</td>"
                f"<td>{len(device.logs)}</td>"
                f"<td>{device.archive_files}</td>"
                f"<td>{_fmt_bytes(device.archive_size)}</td>"
                f"<td>{_esc(_fmt_ts(device.archive_latest) or '—')}</td>"
                "</tr>")
        body = ("<h1>设备</h1>"
                '<p class="sub">设备活动来自记忆库中的活动记录；归档状态来自转录归档目录。</p>'
                f'<div class="panel">{_table(["设备", "最近活动", "活动条目", "归档文件", "归档体积", "最后归档"], rows)}</div>')
        return _page("设备", body, "devices")

    def device_page(self, name: str):
        if not _DEVICE_RE.match(name or ""):
            return 404, HTML_TYPE, self.error_page(404, "设备不存在")
        logs = load_logs(self.cfg.vault)
        archive = load_archive(self.cfg.archive)
        device = next((d for d in build_devices(logs, archive) if d.name == name), None)
        if device is None:
            return 404, HTML_TYPE, self.error_page(404, "设备不存在")

        sections = []
        by_file: dict[str, list[fmt.LogItem]] = {}
        for item in device.logs:
            by_file.setdefault(item.file, []).append(item)
        for file, items in by_file.items():
            rows = "".join(
                "<tr>"
                f"<td>{_esc(item.when)}</td>"
                f"<td>{_esc(item.source)}</td>"
                f'<td><span class="tag">{_esc(item.tag)}</span></td>'
                f"<td>{_esc(item.text)}</td>"
                "</tr>" for item in items)
            note = ""
            if _is_truncated(self.root / file):
                note = ('<p class="sub">该活动记录超过 '
                        f"{MAX_FILE_BYTES // 1024} KB，只显示最近部分。</p>")
            sections.append(f"<h3><code>{_esc(file)}</code></h3>{note}"
                            f'<div class="panel">{_table(["时间", "来源", "标签", "内容"], rows)}</div>')
        log_html = "".join(sections) or _empty("这台设备还没有活动记录")

        archive_rows = [
            "<tr>"
            f"<td>{_esc(stat.agent)}</td>"
            f"<td>{stat.files}</td>"
            f"<td>{_fmt_bytes(stat.size)}</td>"
            f"<td>{_esc(_fmt_ts(stat.latest) or '—')}</td>"
            "</tr>" for stat in device.archive
        ]
        body = (
            f"<h1>{_esc(device.name)}</h1>"
            f'<p class="sub"><a href="/devices">返回设备</a> · 最近活动 {_esc(device.last_seen or "—")}</p>'
            "<h2>转录归档</h2>"
            f'<div class="panel">{_table(["工具", "文件数", "体积", "最后更新"], archive_rows)}</div>'
            "<h2>活动记录</h2>"
            f"{log_html}"
        )
        return 200, HTML_TYPE, _page(device.name, body, "devices")

    def history_page(self) -> str:
        commits = load_commits(self.cfg.vault, limit=100)
        rows = [
            "<tr>"
            f'<td><a href="{_href("history", c.full)}"><code>{_esc(c.short)}</code></a></td>'
            f"<td>{_esc(c.date[:19].replace('T', ' '))}</td>"
            f"<td>{_esc(c.subject)}</td>"
            f"<td>{_esc(c.author)}</td>"
            "</tr>" for c in commits
        ]
        body = ("<h1>历史</h1>"
                '<p class="sub">记忆库的版本记录。自动提交每小时一次；整理任务写入的提交也会出现在这里。</p>'
                f'<div class="panel">{_table(["提交", "时间", "说明", "作者"], rows)}</div>')
        return _page("历史", body, "history")

    def commit_page(self, rev: str):
        try:
            loaded = load_commit(self.cfg.vault, rev)
        except GitTimeout:
            return 504, HTML_TYPE, self.error_page(504, "读取提交超时")
        if loaded is None:
            return 404, HTML_TYPE, self.error_page(404, "提交不存在")
        text, truncated = loaded
        note = ('<p class="sub">差异内容超过 '
                f"{MAX_DIFF_BYTES // 1024} KB，只显示开头部分。</p>"
                if truncated else "")
        body = (f'<h1>提交 <code>{_esc(rev)}</code></h1>'
                f'<p class="sub"><a href="/history">返回历史</a></p>{note}'
                f'<pre class="raw">{_esc(text)}</pre>')
        return 200, HTML_TYPE, _page(f"提交 {rev}", body, "history")

    def reports_page(self) -> str:
        reports = load_reports(self.cfg.reports)
        rows = [
            "<tr>"
            f'<td><a href="{_href("reports", r.name)}">{_esc(r.name)}</a></td>'
            f"<td>{_esc(_fmt_ts(r.mtime))}</td>"
            f"<td>{_fmt_bytes(r.size)}</td>"
            "</tr>" for r in reports
        ]
        body = ("<h1>整理报告</h1>"
                f'<p class="sub">报告目录：<code>{_esc(self.cfg.reports)}</code></p>'
                f'<div class="panel">{_table(["报告", "时间", "大小"], rows)}</div>')
        return _page("报告", body, "reports")

    def report_page(self, name: str, query: dict[str, list[str]]):
        path = self.resolve_report(name)
        if path is None:
            return 404, HTML_TYPE, self.error_page(404, "报告不存在")
        text = _read_text(path)
        if text is None:
            return 404, HTML_TYPE, self.error_page(404, "报告无法读取")
        raw = (query.get("raw") or [""])[0] in ("1", "true", "yes")
        toggle = (f'<a href="{_href("reports", path.name)}">渲染</a>' if raw
                  else f'<a href="{_href("reports", path.name)}?raw=1">原文</a>')
        content = (f'<pre class="raw">{_esc(text)}</pre>' if raw
                   else f'<div class="panel">{render_markdown(text)}</div>')
        body = (f"<h1>{_esc(path.name)}</h1>"
                f'<p class="sub"><a href="/reports">返回报告</a> · {toggle}</p>'
                f"{_truncation_note(path)}{content}")
        return 200, HTML_TYPE, _page(path.name, body, "reports")

    def proposals_page(self) -> str:
        proposals = load_proposals(self.cfg.vault)
        rows = []
        for p in proposals:
            items = "；".join(p.items) if p.items else "无法解析条目"
            rows.append(
                "<tr>"
                f'<td><a href="{_href("proposals", p.name)}">{_esc(p.title)}</a></td>'
                f"<td>{p.pending} / {p.total}</td>"
                f"<td>{_esc(items)}</td>"
                f"<td>{_esc(_fmt_ts(p.mtime))}</td>"
                "</tr>")
        body = ("<h1>整理提案</h1>"
                '<p class="sub">整理任务改动画像或规则时，会把变更写成提案。'
                "打开提案可逐条查看内容并批准或驳回；批准后会立即写入记忆库并提交。</p>"
                f'<div class="panel">{_table(["提案", "待处理 / 总数", "内容", "时间"], rows)}</div>')
        return _page("提案", body, "proposals")

    def proposal_page(self, name: str, query: dict[str, list[str]]):
        path = self.resolve_proposal(name)
        if path is None:
            return 404, HTML_TYPE, self.error_page(404, "提案不存在")
        text = _read_text(path)
        if text is None:
            return 404, HTML_TYPE, self.error_page(404, "提案无法读取")
        raw = (query.get("raw") or [""])[0] in ("1", "true", "yes")
        toggle = (f'<a href="{_href("proposals", path.name)}">渲染</a>' if raw
                  else f'<a href="{_href("proposals", path.name)}?raw=1">原文</a>')
        if raw:
            content = f'<pre class="raw">{_esc(text)}</pre>'
        else:
            content = self._proposal_sections(path.name, text, query)
        body = (f"<h1>{_esc(path.name)}</h1>"
                f'<p class="sub"><a href="/proposals">返回提案</a> · {toggle}</p>'
                f"{_truncation_note(path)}{content}")
        return 200, HTML_TYPE, _page(path.name, body, "proposals")

    def _proposal_sections(self, name: str, text: str, query: dict[str, list[str]]) -> str:
        sections = propmod.parse_sections(text)
        if not sections:
            return ('<p class="notice">没能从提案里解析出条目，下面按原文显示。</p>'
                    f'<div class="panel">{render_markdown(text)}</div>')
        done = (query.get("done") or [""])[0]
        flash = ""
        if done in ("accept", "reject"):
            flash = ('<p class="flash ok">已批准并写入记忆库，所有设备同步后会看到这次修改。</p>'
                     if done == "accept"
                     else '<p class="flash ok">已驳回，目标文件没有改动。</p>')
        pending = sum(1 for s in sections if s.status == propmod.PENDING)
        summary = f'<p class="sub">共 {len(sections)} 条 · 待处理 {pending} 条</p>'
        return flash + summary + "".join(
            self._section_card(name, section) for section in sections)

    def _section_card(self, name: str, sec: propmod.Section) -> str:
        badge = (f'<span class="badge {_esc(sec.status)}">'
                 f'{_esc(_status_label(sec.status))}</span>')
        shown: str | None = None
        problem: str | None = None
        if sec.status == propmod.ACCEPTED:
            target_path = self.root / sec.target
            shown = _read_text(target_path) if target_path.is_file() else None
            if shown is None:
                problem = "目标文件暂时无法读取"
        else:
            shown, problem = propmod.preview(self.root, sec)

        meta: dict[str, str] | None = None
        body = shown or ""
        if shown is not None:
            try:
                meta, body = fmt.parse_frontmatter(shown)
            except fmt.FormatError:
                meta, body = None, shown
        title = (meta or {}).get("description", "") or _first_heading(body)
        head = (f'<div class="head"><span class="tag">{_esc(sec.label)}</span>'
                + (f"<strong>{_esc(title)}</strong>" if title else "") + badge + "</div>")
        status_note = ('<p class="sub">已应用。下面显示目标文件的当前内容。</p>'
                       if sec.status == propmod.ACCEPTED else "")
        problem_html = (f'<p class="notice bad">无法自动应用：{_esc(problem)}</p>'
                        if problem else "")
        rendered = (f'<div class="proposal-body">{self._render_text(body, sec.target)}</div>'
                    if shown is not None else "")
        diff_html = ""
        if sec.diff is not None:
            diff_html = ('<details><summary>查看补丁</summary>'
                         f'<pre class="diff">{_esc(chr(10).join(sec.diff))}</pre></details>')
        actions = ""
        if sec.status == propmod.PENDING:
            accept = ("" if problem else
                      '<button class="primary" name="action" value="accept" '
                      'type="submit">批准并写入</button>')
            actions = (
                f'<form class="acts" method="post" '
                f'action="{_href("proposals", name + "/decision")}">'
                f'<input type="hidden" name="index" value="{sec.index}">'
                f"{accept}"
                '<button class="danger" name="action" value="reject" type="submit">驳回</button>'
                "</form>")
        return (f'<section class="proposal">{head}'
                f'<p class="sub">目标：<code>{_esc(sec.target)}</code></p>'
                f"{status_note}{problem_html}{rendered}{diff_html}{actions}</section>")

    def decide_proposal(self, name: str, fields: dict[str, list[str]]):
        index_text = (fields.get("index") or [""])[0]
        action = (fields.get("action") or [""])[0]
        if not index_text.isdigit() or action not in ("accept", "reject"):
            return 400, "请求参数不完整"
        try:
            propmod.decide(self.cfg.vault, name, int(index_text), action)
        except propmod.ProposalNotFound as e:
            return 404, str(e)
        except propmod.ProposalConflict as e:
            return 409, str(e)
        except propmod.ProposalError as e:
            return 400, str(e)
        return 303, f"{_href('proposals', name)}?done={action}"

    # ----- 辅助 -----

    def _render_text(self, text: str, base_path: str) -> str:
        wikilinks = {
            note.meta["name"]: _href("memory", note.rel)
            for note in load_notes(self.cfg.vault).values()
            if note.meta.get("name")
        }
        return render_markdown(text, base_path=base_path,
                               wikilinks=wikilinks, path_linker=self.note_href)

    def auth_page(self, path: str, query: dict[str, list[str]] | None = None) -> str:
        rest = [(k, v) for k, values in (query or {}).items() if k != "token"
                for v in values]
        action = _esc(quote(path, safe="/"))
        hidden = "".join(
            f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
            for k, v in rest)
        body = (
            "<h1>需要访问令牌</h1>"
            '<p class="sub">令牌来自 <code>AMEM_WEB_TOKEN</code> 或 <code>--token</code>。</p>'
            f'<form class="token" method="get" action="{action}">'
            f"{hidden}"
            '<input type="password" name="token" placeholder="访问令牌" autofocus>'
            '<button type="submit">进入</button></form>')
        return _page("访问令牌", body)

    def error_page(self, status: int, message: str) -> str:
        body = (f"<h1>{status}</h1><p class=\"sub\">{_esc(message)}</p>"
                '<p><a href="/">返回总览</a></p>')
        return _page(str(status), body)


# ---------- HTTP ----------

def _make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "amem-web"
        sys_version = ""

        def __getattr__(self, name):
            # 未实现的 HTTP 方法（CONNECT、任意扩展方法）统一返回 405。
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

        def do_GET(self):
            self._handle(app, head=False)

        def do_HEAD(self):
            self._handle(app, head=True)

        def do_POST(self):
            self._handle(app, head=False, post=True)

        def _method_not_allowed(self):
            self._send(405, HTML_TYPE, app.error_page(405, "只支持 GET/HEAD"),
                       head=False, extra=(("Allow", "GET, HEAD"),))

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = _method_not_allowed

        def _handle(self, app: WebApp, head: bool, post: bool = False):
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query, keep_blank_values=True)
            except (ValueError, UnicodeError):
                self._send(400, HTML_TYPE, app.error_page(400, "请求不合法"), head=head)
                return
            if path.startswith("//") or "\\" in path or _has_control(path):
                self._send(400, HTML_TYPE, app.error_page(400, "请求不合法"), head=head)
                return
            # 没有 token 时只接受回环 Host，防止 DNS rebinding 读取 vault。
            if not app.cfg.token and not _is_loopback_host(self.headers.get("Host", "")):
                self._send(400, HTML_TYPE, app.error_page(400, "Host 不合法"), head=head)
                return
            try:
                if not self._check_auth(app, path, query, head=head):
                    return
                if post:
                    self._handle_post(app, path, head=head)
                    return
                status, ctype, body = app.route(path, query)
            except Exception:
                traceback.print_exc()
                status, ctype, body = 500, HTML_TYPE, app.error_page(500, "内部错误")
            self._send(status, ctype, body, head=head)

        def _handle_post(self, app: WebApp, path: str, *, head: bool) -> None:
            """唯一的写入口：/proposals/<name>/decision。"""
            match = _DECISION_RE.match(path)
            if match is None:
                self.close_connection = True
                self._send(405, HTML_TYPE, app.error_page(405, "只支持 GET/HEAD"),
                           head=head, extra=(("Allow", "GET, HEAD"),))
                return
            if not app.cfg.token:
                # 没有令牌时，浏览器必须带上本机 Origin；跨站表单拿不到。
                origin = self.headers.get("Origin")
                if origin is None or not _is_loopback_origin(origin):
                    self.close_connection = True
                    self._send(403, HTML_TYPE, app.error_page(403, "拒绝跨站请求"), head=head)
                    return
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/x-www-form-urlencoded":
                self.close_connection = True
                self._send(415, HTML_TYPE, app.error_page(415, "请求格式不支持"), head=head)
                return
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                length = -1
            if length < 0 or length > 4096:
                self.close_connection = True
                status = 411 if length < 0 else 413
                message = "缺少请求长度" if length < 0 else "请求过大"
                self._send(status, HTML_TYPE, app.error_page(status, message), head=head)
                return
            body = self.rfile.read(length)
            try:
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                                max_num_fields=10)
            except (ValueError, UnicodeError):
                self._send(400, HTML_TYPE, app.error_page(400, "请求不合法"), head=head)
                return
            status, message = app.decide_proposal(match.group(1), form)
            if status == 303:
                self._send(303, TEXT_TYPE, "", head=head,
                           extra=(("Location", message),))
            else:
                self._send(status, HTML_TYPE, app.error_page(status, message), head=head)

        def _check_auth(self, app: WebApp, path: str, query: dict[str, list[str]],
                        *, head: bool) -> bool:
            if (path.rstrip("/") or "/") == "/healthz":
                return True
            token = app.cfg.token
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            if auth[:7].lower() == "bearer " and _same_secret(auth[7:].strip(), token):
                return True
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                cookies = SimpleCookie()
            cookie = cookies.get("amem_token")
            if cookie and _same_secret(cookie.value, token):
                return True
            supplied = (query.get("token") or [""])[0]
            if supplied and _same_secret(supplied, token):
                rest = [(k, v) for k, values in query.items() if k != "token"
                        for v in values]
                location = quote(path, safe="/")
                if rest:
                    location += "?" + urlencode(rest)
                cookie = SimpleCookie()
                cookie["amem_token"] = token
                cookie["amem_token"]["path"] = "/"
                cookie["amem_token"]["httponly"] = True
                cookie["amem_token"]["samesite"] = "Strict"
                cookie["amem_token"]["max-age"] = 30 * 24 * 3600
                self._send(302, TEXT_TYPE, "", head=head, extra=(
                    ("Location", location),
                    ("Set-Cookie", cookie.output(header="").strip()),
                ))
                return False
            self._send(401, HTML_TYPE, app.auth_page(path, query), head=head, extra=(
                ("WWW-Authenticate", "Bearer"),
            ))
            return False

        def _send(self, status: int, ctype: str, body: str, *, head: bool,
                  extra: tuple[tuple[str, str], ...] = ()) -> None:
            data = body.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
                for key, value in extra:
                    self.send_header(key, value)
                self.end_headers()
                if not head:
                    self.wfile.write(data)
            except (ConnectionError, OSError):
                # 客户端提前断开（包括写响应头时）不是服务端错误，不打印堆栈。
                return
            sys.stderr.write(
                f"{self.client_address[0]} {self.command} "
                f"{self.path.split('?', 1)[0]} {status}\n")

        def log_message(self, fmt, *args):
            pass

    return Handler


def make_server(cfg: WebConfig) -> ThreadingHTTPServer:
    if cfg.token and not _valid_token(cfg.token):
        raise ValueError("token 只能包含 ASCII 可见字符，且不能含空白、引号、逗号、分号或反斜杠")
    if not _is_loopback(cfg.host) and not cfg.token:
        raise ValueError("监听非回环地址时必须设置 token")

    class _Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in cfg.host else socket.AF_INET

    return _Server((cfg.host, cfg.port), _make_handler(WebApp(cfg)))


def _is_loopback(host: str) -> bool:
    return _is_loopback_host(host)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m amem.web", description="记忆库 Web")
    ap.add_argument("--vault", type=Path, help="vault 路径（默认 ~/memory-vault）")
    ap.add_argument("--archive", type=Path, help="转录归档路径（默认 ~/transcript-archive）")
    ap.add_argument("--reports", type=Path, help="dreaming 报告目录（默认 vault 同级 dream-reports）")
    ap.add_argument("--host", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, help="监听端口（默认 8787）")
    ap.add_argument("--token", help="访问令牌；非回环地址必须设置")
    args = ap.parse_args(argv)

    try:
        cfg = WebConfig.from_env()
    except ValueError as e:
        ap.error(f"环境变量配置错误: {e}")
    vault = args.vault.expanduser() if args.vault else cfg.vault
    archive = args.archive.expanduser() if args.archive else cfg.archive
    reports = args.reports.expanduser() if args.reports else cfg.reports
    if not args.reports and not os.environ.get("AMEM_REPORTS"):
        reports = vault.parent / "dream-reports"
    cfg = WebConfig(vault=vault, archive=archive, reports=reports,
                    host=args.host or cfg.host,
                    port=args.port if args.port is not None else cfg.port,
                    token=(args.token if args.token is not None else cfg.token) or None)

    if not cfg.vault.is_dir():
        ap.error(f"vault 不存在: {cfg.vault}")
    if not 1 <= cfg.port <= 65535:
        ap.error(f"端口不合法: {cfg.port}")
    if cfg.token and not _valid_token(cfg.token):
        ap.error("令牌只能包含 ASCII 可见字符，且不能含空白、引号、逗号、分号或反斜杠")
    if not _is_loopback(cfg.host) and not cfg.token:
        ap.error("监听非回环地址时必须设置 --token 或 AMEM_WEB_TOKEN")

    try:
        server = make_server(cfg)
    except (OSError, ValueError) as e:
        ap.error(f"无法启动服务: {e}")
    host, port = server.server_address[:2]
    display_host = f"[{host}]" if ":" in host else host
    print(f"amem-web: http://{display_host}:{port}/ （提案可在页面内批准或驳回）", flush=True)
    if not cfg.archive.is_dir():
        print(f"amem-web: 归档目录不存在，设备归档信息为空: {cfg.archive}", flush=True)
    if not cfg.reports.is_dir():
        print(f"amem-web: 报告目录不存在: {cfg.reports}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
