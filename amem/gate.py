"""写回闸门：比对 agent 改过的临时副本和 vault，按路径分级，校验后才写回。

这是安全边界，必须是确定性代码：分级只看"改了哪个路径、怎么改"，不看 agent 怎么说。
  A 直接提交：MEMORY.md、log/ 追加、proposals/ 新文件
  B 独立提交：projects/ 新增或修改；profile/ rules/ 里只把条目标为 superseded
  C 转提案  ：profile/ rules/ 的其他新增或修改
  拒绝      ：删除、改写已有 log、越权路径、符号链接、非 UTF-8、格式错误、疑似密钥
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol

from . import format as fmt

A, B, C, REJECT = "A", "B", "C", "reject"
# 闸门自己写的文件，agent 不能碰
PROTECTED = frozenset({"AGENT_PROTOCOL.md", "FORMAT.md", ".dream-state", ".stignore"})
SUPERSEDE_FIELDS = frozenset({"status", "superseded_by", "updated", "source"})


@dataclass
class Change:
    path: str
    kind: str                 # add | modify | delete
    old: bytes | None
    new: bytes | None
    tier: str = ""
    reason: str = ""

    @property
    def added_text(self) -> str:
        """需要做密钥扫描的新内容：log 追加只扫追加部分。"""
        if self.new is None:
            return ""
        new = fmt.decode(self.new) or ""
        if self.kind == "modify" and self.old is not None and self.path.startswith("log/"):
            return new[len(fmt.decode(self.old) or ""):]
        return new


@dataclass
class Decision:
    changes: list[Change] = field(default_factory=list)
    issues: list[fmt.Issue] = field(default_factory=list)   # 校验后仍存在的 warning 等

    def by_tier(self, tier: str) -> list[Change]:
        return [c for c in self.changes if c.tier == tier]


def diff_files(before: Mapping[str, bytes], after: Mapping[str, bytes]) -> list[Change]:
    out = []
    for path in sorted(set(before) | set(after)):
        o, n = before.get(path), after.get(path)
        if o == n:
            continue
        kind = "add" if o is None else "delete" if n is None else "modify"
        out.append(Change(path, kind, o, n))
    return out


# ---------- 分级 ----------

MAX_FILE_BYTES = 256 * 1024


def _only_superseded(old: bytes, new: bytes) -> bool:
    """profile/ rules/ 的修改是否只是把条目标成 superseded（正文不变）。"""
    try:
        om, ob = fmt.parse_frontmatter(fmt.decode(old) or "")
        nm, nb = fmt.parse_frontmatter(fmt.decode(new) or "")
    except fmt.FormatError:
        return False
    if om is None or nm is None or ob != nb:
        return False
    if om.get("status") == "superseded" or nm.get("status") != "superseded":
        return False
    changed = {k for k in set(om) | set(nm) if om.get(k) != nm.get(k)}
    return changed <= SUPERSEDE_FIELDS


def classify(ch: Change) -> tuple[str, str]:
    p = ch.path
    parts = p.split("/")
    if p.startswith("/") or ".." in parts or "" in parts:
        return REJECT, "非法路径"
    if ch.new == fmt.SYMLINK_MARK or ch.old == fmt.SYMLINK_MARK:
        return REJECT, "不允许符号链接"
    if p in PROTECTED or parts[0].startswith(".") or fmt.is_sync_conflict(p):
        return REJECT, "受保护文件"
    if ch.kind == "delete":
        return REJECT, "不允许删除，过期内容请标 superseded"
    if not (p.endswith(".md") or parts[-1] == ".gitkeep"):
        return REJECT, "只允许 .md 文件"
    if len(ch.new or b"") > MAX_FILE_BYTES:
        return REJECT, f"文件超过 {MAX_FILE_BYTES} 字节"
    if fmt.decode(ch.new or b"") is None:
        return REJECT, "不是 UTF-8"
    if p == fmt.MEMORY_FILE:
        return A, "索引"
    top = parts[0]
    if top == "log":
        if not fmt.is_log_path(p):
            return REJECT, "log 文件名应为 log/YYYY-MM.<设备>.md"
        if ch.kind == "modify" and not ch.new.startswith(ch.old):
            return REJECT, "log 只能追加，不能改已有内容"
        return A, "log 追加"
    if top == "proposals" and len(parts) == 2:
        if ch.kind == "modify":
            return REJECT, "已有提案只能由用户会话修改"
        return A, "新提案"
    if top == "projects" and len(parts) >= 2:
        return B, "项目背景"
    if top in ("profile", "rules") and len(parts) >= 2:
        if ch.kind == "modify" and _only_superseded(ch.old, ch.new):
            return B, "标记 superseded"
        return C, "画像/规则变更需确认"
    return REJECT, "不允许写入的路径"


# ---------- 密钥扫描 ----------

class ScannerUnavailable(RuntimeError):
    """扫描器不可用。调用方必须拒绝全部变更（fail closed）。"""


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str


class Scanner(Protocol):
    def scan(self, texts: Mapping[str, str]) -> list[Finding]: ...


# 兜底规则：gitleaks 之外再过一遍，规则宁严勿松（误报只会让条目转人工）
_BUILTIN_RULES = [
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("api-key-prefix", r"\b(?:sk|pk|rk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("url-credential", r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    ("url-secret-param", r"(?i)[?&][a-z0-9_-]*(?:api[_-]?key|token|secret|password)=[^\s&]{8,}"),
    ("assignment", r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b"
                   r"\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]


class RegexScanner:
    def __init__(self, rules=None):
        self.rules = [(n, re.compile(p)) for n, p in (rules or _BUILTIN_RULES)]

    def scan(self, texts: Mapping[str, str]) -> list[Finding]:
        out = []
        for path, text in texts.items():
            for name, rx in self.rules:
                if rx.search(text):
                    out.append(Finding(path, name))
        return out


class GitleaksScanner:
    def __init__(self, binary: str = "gitleaks", timeout: int = 120):
        self.binary = shutil.which(binary)
        self.timeout = timeout

    def scan(self, texts: Mapping[str, str]) -> list[Finding]:
        if not self.binary:
            raise ScannerUnavailable("找不到 gitleaks，拒绝写回")
        if not texts:
            return []
        with tempfile.TemporaryDirectory(prefix="amem-scan-") as tmp:
            for path, text in texts.items():
                p = Path(tmp, "src", path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            report = Path(tmp, "report.json")
            try:
                r = subprocess.run(
                    [self.binary, "detect", "--no-git", "--no-banner", "--redact",
                     "--source", str(Path(tmp, "src")), "--report-format", "json",
                     "--report-path", str(report), "--exit-code", "1"],
                    capture_output=True, text=True, timeout=self.timeout)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise ScannerUnavailable(f"gitleaks 运行失败: {e}") from e
            if r.returncode not in (0, 1):
                raise ScannerUnavailable(f"gitleaks 退出码 {r.returncode}: {r.stderr[-300:]}")
            try:
                data = json.loads(report.read_text() or "[]")
            except (OSError, ValueError) as e:
                raise ScannerUnavailable(f"gitleaks 报告无法解析: {e}") from e
            if r.returncode == 1 and not data:
                raise ScannerUnavailable("gitleaks 报告有泄露但结果为空")
            src = str(Path(tmp, "src")) + "/"
            return [Finding(str(d.get("File", "")).replace(src, "", 1).lstrip("/"),
                            "gitleaks:" + str(d.get("RuleID", "?"))) for d in data]


class CompositeScanner:
    def __init__(self, *scanners: Scanner):
        self.scanners = scanners

    def scan(self, texts: Mapping[str, str]) -> list[Finding]:
        out: list[Finding] = []
        for s in self.scanners:
            out.extend(s.scan(texts))
        return out


def default_scanner() -> Scanner:
    return CompositeScanner(GitleaksScanner(), RegexScanner())


# ---------- 评估 ----------

def evaluate(before: Mapping[str, bytes], after: Mapping[str, bytes],
             scanner: Scanner) -> Decision:
    """纯函数：不写盘。返回每个变更的分级结果。
    扫描器不可用时抛 ScannerUnavailable，调用方应整体放弃。"""
    dec = Decision(diff_files(before, after))
    for ch in dec.changes:
        ch.tier, ch.reason = classify(ch)

    # 1. 密钥扫描：所有未被拒绝的变更都要扫（包括 C，提案里也不能有密钥）
    live = [c for c in dec.changes if c.tier != REJECT]
    findings = scanner.scan({c.path: c.added_text for c in live})
    hit: dict[str, list[str]] = {}
    for f in findings:
        hit.setdefault(f.path, []).append(f.rule)
    for c in live:
        if c.path in hit:
            c.tier, c.reason = REJECT, "疑似密钥: " + ", ".join(sorted(set(hit[c.path])))

    # 2. C 级单独校验格式（不进入候选状态）
    for c in dec.by_tier(C):
        _, errs = fmt.validate_note(c.path, fmt.decode(c.new) or "")
        if errs:
            c.tier, c.reason = REJECT, "格式错误: " + "; ".join(e.detail for e in errs)

    # 3. A/B 应用到候选状态后整体校验；只追究"新出现"的错误
    baseline = {i.key() for i in fmt.errors(fmt.validate_vault(before))}
    while True:
        applied = [c for c in dec.changes if c.tier in (A, B)]
        cand = dict(before)
        for c in applied:
            cand[c.path] = c.new
        issues = fmt.validate_vault(cand)
        new_errs = [i for i in fmt.errors(issues) if i.key() not in baseline]
        if not new_errs:
            dec.issues = issues
            return dec
        changed = {c.path: c for c in applied}
        # 标记 superseded 但新条目还在 C 级待确认：一起转提案，而不是拒绝
        pending = {(fmt.parse_frontmatter(fmt.decode(c.new) or "")[0] or {}).get("name")
                   for c in dec.by_tier(C)}
        demoted = False
        for p, c in changed.items():
            mine = [i for i in new_errs if i.path == p]
            if mine and c.tier == B and all(
                    i.code == "superseded-by-broken" and i.detail.split("=", 1)[1].split(" ")[0] in pending
                    for i in mine):
                c.tier, c.reason, demoted = C, "替代条目待确认，一起转提案", True
        if demoted:
            continue
        blamed = {i.path for i in new_errs if i.path in changed}
        if not blamed:
            # 错误落在未改动的文件上（如重名），无法归因，全部拒绝
            blamed = set(changed)
        for p in blamed:
            errs = [i.detail for i in new_errs if i.path == p] or [str(i) for i in new_errs]
            changed[p].tier, changed[p].reason = REJECT, "校验失败: " + "; ".join(errs)


# ---------- 写回 ----------

@dataclass
class ApplyResult:
    committed: list[tuple[str, list[str]]] = field(default_factory=list)  # (commit 信息, 路径)
    proposal: str | None = None
    conflicts: list[Change] = field(default_factory=list)


def _git(vault: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()}")
    return r.stdout


def _write(vault: Path, rel: str, data: bytes) -> None:
    p = Path(vault, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.amem-tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def _commit(vault: Path, msg: str, paths: list[str]) -> None:
    _git(vault, "add", "--", *paths)
    if _git(vault, "diff", "--cached", "--name-only", "--", *paths).strip():
        _git(vault, "commit", "-q", "-m", msg, "--", *paths)


def render_proposal(changes: list[Change], when: datetime) -> str:
    lines = [f"# 整理提案 {when:%Y-%m-%d}", "",
             "以下变更涉及画像或规则，需要你确认。处理后在每条标题后标注 accepted / rejected。", ""]
    for i, c in enumerate(changes, 1):
        old = (fmt.decode(c.old) or "").splitlines() if c.old else []
        new = (fmt.decode(c.new) or "").splitlines()
        diff = difflib.unified_diff(old, new, f"a/{c.path}", f"b/{c.path}", lineterm="")
        lines += [f"## {i}. {'新增' if c.kind == 'add' else '修改'} `{c.path}` · pending", "",
                  "````diff", *diff, "````", ""]
    return "\n".join(lines)


def apply(vault: Path, dec: Decision, *, commit: bool = True, now: datetime | None = None,
          tag: str = "dream") -> ApplyResult:
    """把 A/B 写回 vault，C 写成提案。写之前确认目标文件在整理期间没被别人改过。"""
    vault = Path(vault)
    now = now or datetime.now()
    res = ApplyResult()
    current = fmt.iter_files(vault)
    for c in dec.changes:
        if c.tier in (A, B, C) and current.get(c.path) != c.old:
            res.conflicts.append(c)
            c.tier, c.reason = REJECT, "整理期间文件被其他设备修改，下次再处理"

    a = dec.by_tier(A)
    for c in a:
        _write(vault, c.path, c.new)
    if commit and a:
        msg = f"{tag}: A 自动整理 {len(a)} 个文件"
        _commit(vault, msg, [c.path for c in a])
        res.committed.append((msg, [c.path for c in a]))

    for c in dec.by_tier(B):
        _write(vault, c.path, c.new)
        if commit:
            msg = f"{tag}: B {c.reason} {c.path}"
            _commit(vault, msg, [c.path])
            res.committed.append((msg, [c.path]))

    cs = dec.by_tier(C)
    if cs:
        rel = f"proposals/{now:%Y-%m-%d}.md"
        n = 2
        while rel in current or Path(vault, rel).exists():
            rel, n = f"proposals/{now:%Y-%m-%d}-{n}.md", n + 1
        _write(vault, rel, render_proposal(cs, now).encode("utf-8"))
        res.proposal = rel
        if commit:
            msg = f"{tag}: C 新提案 {len(cs)} 条"
            _commit(vault, msg, [rel])
            res.committed.append((msg, [rel]))
    return res
