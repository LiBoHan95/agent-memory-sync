"""dreaming 主流程：收集增量 → 解析 → 筛选 → headless agent 在副本上整理 → 闸门 → 写回 → 报告。

agent 只能改临时副本，真正写回 vault 的只有 gate.apply。
任何一步失败都不推进 .dream-state，下次重跑同一批增量。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import format as fmt
from . import gate, parsers
from .parsers.base import DREAM_WORKDIR_MARK, Message
from .screen import PassThrough, Screener

STATE_FILE = ".dream-state"
PROMPT_FILE = Path(__file__).with_name("prompts") / "dreaming.md"
DEFAULT_AGENT = ("claude -p {prompt} --permission-mode acceptEdits "
                 "--allowedTools Read,Write,Edit,Glob,Grep --output-format json")


@dataclass
class Config:
    vault: Path
    archive: Path | None = None
    agent_cmd: str = DEFAULT_AGENT     # {prompt} 会被替换成指令文本，工作目录是临时目录
    agent_timeout: int = 1800
    budget_chars: int = 200_000        # 单次交给 agent 的输入上限
    commit: bool = True
    report_dir: Path | None = None     # 默认 <vault 父目录>/dream-reports
    dry_run: bool = False              # 只跑到闸门，不写回、不推进状态

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(
            vault=Path(e.get("AMEM_VAULT", "~/memory-vault")).expanduser(),
            archive=Path(e.get("AMEM_ARCHIVE", "~/transcript-archive")).expanduser(),
            agent_cmd=e.get("AMEM_AGENT_CMD", DEFAULT_AGENT),
            agent_timeout=int(e.get("AMEM_AGENT_TIMEOUT", "1800")),
            budget_chars=int(e.get("AMEM_BUDGET_CHARS", "200000")),
        )


@dataclass
class State:
    transcripts: dict[str, int] = field(default_factory=dict)  # 归档相对路径 -> 字节偏移
    logs: dict[str, int] = field(default_factory=dict)         # log 相对路径 -> 已处理字节数
    last_run: str = ""

    @classmethod
    def load(cls, vault: Path) -> "State":
        p = vault / STATE_FILE
        if not p.exists():
            return cls()
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(dict(d.get("transcripts", {})), dict(d.get("logs", {})), d.get("last_run", ""))

    def save(self, vault: Path) -> None:
        p = vault / STATE_FILE
        tmp = p.with_name(STATE_FILE + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "last_run": self.last_run,
                                   "transcripts": self.transcripts, "logs": self.logs},
                                  ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)


# ---------- 收集增量 ----------

@dataclass
class Batch:
    logs: list[tuple[str, str]] = field(default_factory=list)       # (路径, 新增文本)
    messages: list[Message] = field(default_factory=list)
    next_state: State = field(default_factory=State)
    skipped: list[str] = field(default_factory=list)                 # 超预算留到下次的文件
    unknown: list[str] = field(default_factory=list)                 # 没有解析器的目录

    @property
    def empty(self) -> bool:
        return not self.logs and not self.messages


def collect(cfg: Config, state: State, screener: Screener) -> Batch:
    """按预算收集增量。第一个文件即使超预算也收（会截断），避免卡死在一个大文件上。"""
    b = Batch(next_state=State(dict(state.transcripts), dict(state.logs)))
    used = 0

    def fits(n: int) -> bool:
        return used == 0 or used + n <= cfg.budget_chars

    for rel, data in sorted(fmt.iter_files(cfg.vault).items()):
        if not fmt.is_log_path(rel) or data == fmt.SYMLINK_MARK:
            continue
        done = state.logs.get(rel, 0)
        if len(data) < done:          # 被截断过，不该发生；从头再读并在报告里体现
            done = 0
        new = (fmt.decode(data[done:]) or "").strip()
        if not new:
            b.next_state.logs[rel] = len(data)
            continue
        if not fits(len(new)):
            b.skipped.append(rel)
            continue
        b.logs.append((rel, new))
        b.next_state.logs[rel] = len(data)
        used += len(new)

    if cfg.archive and cfg.archive.is_dir():
        for dev_dir in sorted(p for p in cfg.archive.iterdir() if p.is_dir()):
            for agent_dir in sorted(p for p in dev_dir.iterdir() if p.is_dir()):
                parser = parsers.get(agent_dir.name)
                if parser is None:
                    b.unknown.append(agent_dir.relative_to(cfg.archive).as_posix())
                    continue
                for path in sorted(agent_dir.rglob("*")):
                    if path.is_symlink() or not path.is_file() or not parser.matches(path):
                        continue
                    rel = path.relative_to(cfg.archive).as_posix()
                    off = state.transcripts.get(rel, 0)
                    if path.stat().st_size == off:
                        continue
                    msgs, new_off = parser.parse(path, off, device=dev_dir.name, rel=rel)
                    msgs = screener.screen(msgs)
                    n = sum(len(m.text) for m in msgs)
                    if msgs and not fits(n):
                        b.skipped.append(rel)
                        continue
                    b.messages.extend(msgs)
                    b.next_state.transcripts[rel] = new_off
                    used += n
    return b


def render_input(b: Batch, limit: int) -> str:
    """交给 agent 的增量材料。按会话分组，用户消息完整保留，超限时截断尾部。"""
    out = ["# 本次增量", "", "## 新 log", ""]
    for rel, text in b.logs:
        out += [f"### {rel}", "", text, ""]
    if not b.logs:
        out += ["（无）", ""]
    out += ["## 新转录（已过滤工具调用和系统注入）", ""]
    session = None
    for m in sorted(b.messages, key=lambda m: (m.device, m.agent, m.session, m.timestamp)):
        key = (m.device, m.agent, m.session)
        if key != session:
            session = key
            out += ["", f"### {m.agent}@{m.device} · 会话 {m.session[:8]} · {m.timestamp[:10]} · {m.cwd}", ""]
        who = "用户" if m.role == "user" and m.origin == "human" else \
              "自动提示" if m.role == "user" else "agent"
        out.append(f"**{who}**：{m.text}")
    if not b.messages:
        out.append("（无）")
    text = "\n".join(out) + "\n"
    if len(text) > limit:
        text = text[:limit] + "\n\n…[超出本次预算，已截断]\n"
    return text


# ---------- 运行 agent ----------

class AgentFailed(RuntimeError):
    pass


def build_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def run_agent(cmd: str, workdir: Path, prompt: str, timeout: int) -> str:
    """在 workdir 里运行 headless agent。返回 stdout。命令里的 {prompt} 整体替换为一个参数。"""
    argv = [prompt if a == "{prompt}" else a for a in shlex.split(cmd)]
    if prompt not in argv:
        raise AgentFailed("agent 命令里缺少 {prompt} 占位符")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMEM_")}
    try:
        r = subprocess.run(argv, cwd=workdir, capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentFailed(f"agent 启动或超时: {e}") from e
    if r.returncode != 0:
        raise AgentFailed(f"agent 退出码 {r.returncode}: {r.stderr[-500:]}")
    return r.stdout


def _materialize(files: dict[str, bytes], dest: Path) -> None:
    for rel, data in files.items():
        if data == fmt.SYMLINK_MARK:
            continue
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


# ---------- 报告 ----------

def render_report(now: datetime, b: Batch, dec: gate.Decision | None, res: gate.ApplyResult | None,
                  screener: str, agent_out: str, error: str = "") -> str:
    L = [f"# dreaming 报告 {now:%Y-%m-%d %H:%M}", "",
         f"- 增量：log {len(b.logs)} 段，转录消息 {len(b.messages)} 条（筛选：{screener}）"]
    if b.skipped:
        L.append(f"- 超预算留到下次：{len(b.skipped)} 个文件")
    if b.unknown:
        L.append(f"- 没有解析器的目录：{', '.join(b.unknown)}")
    if error:
        L += ["", f"**失败**：{error}", "", "状态未推进，下次会重跑这批增量。"]
    if dec:
        names = {gate.A: "A 已提交", gate.B: "B 已提交（可单独 revert）", gate.C: "C 转提案",
                 gate.REJECT: "已拒绝"}
        for t in (gate.A, gate.B, gate.C, gate.REJECT):
            cs = dec.by_tier(t)
            if cs:
                L += ["", f"## {names[t]}", ""]
                L += [f"- `{c.path}`（{c.kind}）{c.reason}" for c in cs]
        warns = [i for i in dec.issues if i.severity == "warning"]
        if warns:
            L += ["", "## 警告", ""] + [f"- {i}" for i in warns]
    if res and res.proposal:
        L += ["", f"新提案：`{res.proposal}`，下次任意 agent 启动时会提醒你。"]
    if agent_out:
        L += ["", "## agent 输出（末尾）", "", "````", agent_out[-3000:], "````"]
    return "\n".join(L) + "\n"


# ---------- 主流程 ----------

@dataclass
class RunResult:
    status: str                 # empty | ok | failed | dry-run
    report: Path | None = None
    decision: gate.Decision | None = None


def _agent_text(stdout: str) -> str:
    """claude --output-format json 的结果在 result 字段；其他 agent 原样返回。"""
    try:
        d = json.loads(stdout)
    except ValueError:
        return stdout
    return str(d.get("result", stdout)) if isinstance(d, dict) else stdout


def _safe_output(text: str) -> str:
    """报告不进 vault，但也不应留下密钥：命中就整段丢弃。"""
    return "（agent 输出疑似含密钥，已丢弃）" if gate.RegexScanner().scan({"o": text}) else text


def run(cfg: Config, *, screener: Screener | None = None, scanner: gate.Scanner | None = None,
        now: datetime | None = None) -> RunResult:
    screener = screener or PassThrough()
    scanner = scanner or gate.default_scanner()
    now = now or datetime.now()
    vault = Path(cfg.vault)
    report_dir = Path(cfg.report_dir or vault.parent / "dream-reports")
    lock_path = Path(tempfile.gettempdir(), "amem-lock-" + hashlib.sha1(str(vault.resolve()).encode()).hexdigest()[:12] + ".lock")

    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个 dreaming 正在运行")

        state = State.load(vault)
        batch = collect(cfg, state, screener)
        if batch.empty:
            if not cfg.dry_run:
                batch.next_state.last_run = now.isoformat(timespec="seconds")
                batch.next_state.save(vault)
            return RunResult("empty")

        dec = res = None
        agent_out = error = ""
        before = {k: v for k, v in fmt.iter_files(vault).items() if k != STATE_FILE}
        with tempfile.TemporaryDirectory(prefix=DREAM_WORKDIR_MARK) as tmp:
            work = Path(tmp)
            copy = work / "vault"
            _materialize(before, copy)
            (work / "input.md").write_text(render_input(batch, cfg.budget_chars), encoding="utf-8")
            try:
                agent_out = _safe_output(_agent_text(
                    run_agent(cfg.agent_cmd, work, build_prompt(), cfg.agent_timeout)))
                after = fmt.iter_files(copy)
                dec = gate.evaluate(before, after, scanner)
                if not cfg.dry_run:
                    res = gate.apply(vault, dec, commit=cfg.commit, now=now)
            except (AgentFailed, gate.ScannerUnavailable, RuntimeError) as e:
                error = str(e)

        if not error and not cfg.dry_run:
            batch.next_state.last_run = now.isoformat(timespec="seconds")
            batch.next_state.save(vault)
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{now:%Y-%m-%d-%H%M%S}.md"
        report.write_text(render_report(now, batch, dec, res, screener.name, agent_out, error),
                          encoding="utf-8")
        status = "failed" if error else "dry-run" if cfg.dry_run else "ok"
        return RunResult(status, report, dec)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m amem.dream", description="运行一次 dreaming")
    ap.add_argument("--vault", type=Path)
    ap.add_argument("--archive", type=Path)
    ap.add_argument("--agent-cmd")
    ap.add_argument("--dry-run", action="store_true", help="只跑到闸门，不写回、不推进状态")
    ap.add_argument("--no-commit", action="store_true")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    if a.vault:
        cfg.vault = a.vault.expanduser()
    if a.archive:
        cfg.archive = a.archive.expanduser()
    if a.agent_cmd:
        cfg.agent_cmd = a.agent_cmd
    cfg.dry_run, cfg.commit = a.dry_run, not a.no_commit
    r = run(cfg)
    print(f"{r.status}" + (f" · 报告 {r.report}" if r.report else ""))
    return 1 if r.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
