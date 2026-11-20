"""归档分层：对话层抽取、冷数据压缩与体积统计。

原始归档目录结构是 `<设备>/<agent>/...`。本模块在它旁边生成：

  conversation/<设备>/<agent>/.../*.md.zst

对话层只保留 user/assistant 文本（走 `amem.parsers`），体积通常只有原始的
百分之几，压缩后更小；它不参与 dreaming，但可以在原始转录按保留策略删除后
用来重新提炼。

`compact` 把超过保留期的明文 `.jsonl` 压成 `.jsonl.zst`，可选删除明文。
配合 `archive-transcripts.sh` 的 `AMEM_ARCHIVE_HOT_DAYS` 使用：客户端只上传
最近 N 天的文件，服务器处理完旧文件后不会再收到同一份明文。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import parsers
from .parsers.base import (
    complete_lines,
    is_compressed,
    prefix_matches_lines,
    read_head_lines,
)

CONVERSATION_DIR = "conversation"
CONVERSATION_MARKER = "<!-- amem-conversation:v2 -->"
SKIP_TOP = frozenset({CONVERSATION_DIR, ".amem-forget", ".git"})


@dataclass
class Stats:
    files: int = 0
    messages: int = 0
    skipped: int = 0
    errors: int = 0
    deleted: int = 0
    bytes_in: int = 0
    bytes_out: int = 0


def _zstd() -> str:
    binary = shutil.which("zstd")
    if not binary:
        raise SystemExit("找不到 zstd，请先安装（brew install zstd / apt install zstd）")
    return binary


def _iter_sessions(root: Path):
    """产出 (device, agent, agent_dir, path, rel_to_agent)。"""
    if not root.is_dir():
        return
    for device_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if device_dir.name.startswith(".") or device_dir.name in SKIP_TOP:
            continue
        for agent_dir in sorted(p for p in device_dir.iterdir() if p.is_dir()):
            agent = agent_dir.name
            if agent not in parsers.PARSERS:
                continue
            parser = parsers.get(agent)
            assert parser is not None
            for path in sorted(agent_dir.rglob("*")):
                if path.is_symlink() or not path.is_file():
                    continue
                if parser.matches(path):
                    yield device_dir.name, agent, agent_dir, path, path.relative_to(agent_dir).as_posix()


def _conversation_path(root: Path, device: str, agent: str, rel: str) -> Path:
    p = Path(rel)
    name = p.name
    for suffix in (".jsonl.zstd", ".jsonl.zst", ".jsonl"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return root / CONVERSATION_DIR / device / agent / p.parent / f"{name}.md.zst"


def _render(device: str, agent: str, rel: str, messages) -> bytes:
    lines = [CONVERSATION_MARKER, f"# {agent}@{device} · {rel}", ""]
    # 每条消息都带 cwd；取每个会话第一条非空值，避免首条消息没有 cwd 时
    # 对话层文件头为空、forget 按 cwd 匹配不到而残留。
    # 所有会话头统一放在第一条消息之前，forget 只解析第一个 `## ` 之前的内容，
    # 消息正文里即使出现 `- session:`/`- cwd:` 也不会被当成元数据。
    session_cwds: dict[str, str] = {}
    session_order: list[str] = []
    for m in messages:
        if m.session not in session_cwds:
            session_cwds[m.session] = m.cwd
            session_order.append(m.session)
        elif not session_cwds[m.session] and m.cwd:
            session_cwds[m.session] = m.cwd
    for session in session_order:
        lines += [f"- session: {session}",
                  f"- cwd: {session_cwds[session]}", ""]
    for m in messages:
        who = "user" if m.role == "user" and m.origin == "human" else \
              "auto" if m.role == "user" else "assistant"
        lines += [f"## {m.timestamp} · {who}", "", m.text, ""]
    return "\n".join(lines).encode("utf-8")


def _write_zstd(path: Path, data: bytes, binary: str, level: int = 19) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 并发 compact 不能共用同一个临时文件；进程号足以区分，失败时清理。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.amem-tmp")
    r = subprocess.run([binary, "-q", f"-{level}", "-f", "-o", str(tmp), "-"],
                       input=data, capture_output=True)
    if r.returncode != 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"zstd 压缩失败: {path}: {r.stderr[-200:]}")
    tmp.replace(path)


def _conversation_is_current(path: Path) -> bool:
    """对话层是否由当前格式生成（带版本标记）。旧格式会触发重建。"""
    try:
        head = read_head_lines(path, 1)
    except (OSError, RuntimeError):
        return False
    return bool(head) and head[0].strip() == CONVERSATION_MARKER.encode()


def _conversation_fresh(out: Path, source: Path) -> bool:
    return (out.exists() and out.stat().st_mtime >= source.stat().st_mtime
            and _conversation_is_current(out))


def _write_conversation(root: Path, device: str, agent: str, path: Path,
                        rel: str, *, force: bool, stats: Stats) -> Path:
    out = _conversation_path(root, device, agent, rel)
    if not force and _conversation_fresh(out, path):
        stats.skipped += 1
        return out
    parser = parsers.get(agent)
    assert parser is not None
    messages, _ = parser.parse(path, 0, device=device, rel=rel)
    if not messages:
        stats.skipped += 1
        return out
    data = _render(device, agent, rel, messages)
    _write_zstd(out, data, _zstd())
    stats.files += 1
    stats.messages += len(messages)
    stats.bytes_in += path.stat().st_size
    stats.bytes_out += out.stat().st_size
    return out


def build_conversation(root: Path, *, dry_run: bool = False, force: bool = False) -> Stats:
    stats = Stats()
    for device, agent, _, path, rel in _iter_sessions(root):
        out = _conversation_path(root, device, agent, rel)
        try:
            if not force and _conversation_fresh(out, path):
                stats.skipped += 1
                continue
        except OSError as e:
            stats.errors += 1
            print(f"对话层检查失败: {path}: {e}", file=sys.stderr)
            continue
        if dry_run:
            parser = parsers.get(agent)
            assert parser is not None
            try:
                messages, _ = parser.parse(path, 0, device=device, rel=rel)
            except (OSError, RuntimeError) as e:
                stats.errors += 1
                print(f"对话层解析失败: {path}: {e}", file=sys.stderr)
                continue
            if messages:
                stats.files += 1
                stats.messages += len(messages)
                stats.bytes_in += path.stat().st_size
            else:
                stats.skipped += 1
            continue
        try:
            _write_conversation(root, device, agent, path, rel, force=force, stats=stats)
        except (OSError, RuntimeError) as e:
            stats.errors += 1
            print(f"对话层生成失败: {path}: {e}", file=sys.stderr)
    return stats


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decompress(path: Path) -> bytes:
    r = subprocess.run([_zstd(), "-dc", str(path)], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"zstd 解压失败: {path}: {r.stderr[-200:]}")
    return r.stdout


def _prefix_bytes(data: bytes, lines: int) -> bytes | None:
    """取前 lines 个完整行（含换行）的字节；不足时返回 None。"""
    end = 0
    for _ in range(lines):
        i = data.find(b"\n", end)
        if i < 0:
            return None
        end = i + 1
    return data[:end]


def _same_prefix(old_data: bytes, new_data: bytes, lines: int) -> bool:
    prefix = _prefix_bytes(old_data, lines)
    return prefix is not None and prefix == _prefix_bytes(new_data, lines)


def _state_compressed_lines(vault: Path, rel: str) -> int:
    """读 .dream-state 里某个压缩文件已处理的行数；读不到按 0。"""
    state_path = vault / ".dream-state"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(state, dict):
        return 0
    compressed = state.get("compressed_lines")
    if not isinstance(compressed, dict):
        return 0
    value = compressed.get(rel, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _migrate_state(vault: Path, root: Path, old: Path, new: Path,
                   *, trust_prev: bool = True) -> None:
    """把 .dream-state 里明文的偏移迁移成压缩文件的行状态。

    只有旧偏移确实读到明文末尾时才标记为“已读完”；否则保留已读行数、
    强制下次从剩余尾部继续，宁可重读也不能把未读消息标成已处理。
    明文的旧偏移本身保留在 state 里：明文被 rsync 重新上传或 unlink 失败时，
    dreaming 仍从旧偏移继续，不会重放整个会话。
    """
    state_path = vault / ".dream-state"
    if not state_path.is_file():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(state, dict):
        return
    transcripts = state.get("transcripts")
    if not isinstance(transcripts, dict):
        return
    old_rel = old.relative_to(root).as_posix()
    new_rel = new.relative_to(root).as_posix()
    if old_rel not in transcripts:
        return
    old_off = transcripts.get(old_rel)
    compressed = state.get("compressed_lines")
    if not isinstance(compressed, dict):
        compressed = {}
        state["compressed_lines"] = compressed
    prev_done = compressed.get(new_rel, 0)
    if not isinstance(prev_done, int) or prev_done < 0:
        prev_done = 0
    transcripts.pop(new_rel, None)
    compressed.pop(new_rel, None)

    if not trust_prev:
        # 压缩前已确认明文前缀与旧 zst 不一致：文件被替换，旧进度不可信。
        transcripts[old_rel] = 0
        transcripts[new_rel] = 0
        compressed[new_rel] = 0
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(state_path)
        return

    # 保留明文的旧偏移，不要 pop：客户端 rsync 可能把同一份明文重新上传
    # （默认 AMEM_ARCHIVE_HOT_DAYS=0 时每小时都会），或者 unlink 失败留下
    # 明文。只要旧偏移还在，dreaming 就只会读尾部，不会从 0 重放整个会话。
    if not isinstance(old_off, int):
        transcripts.pop(old_rel, None)
        old_off = None

    try:
        old_size = old.stat().st_size
        old_data = old.read_bytes()
        new_size = new.stat().st_size
        new_data = _decompress(new)
    except (OSError, RuntimeError):
        # 无法确认内容时让 dreaming 从头读压缩文件，最多重复一次。
        pass
    else:
        lines = complete_lines(new_data)
        total = len(lines)
        if isinstance(old_off, int) and 0 <= old_off <= old_size:
            if old_off == old_size and (old_size == 0 or old_data.endswith(b"\n")):
                # 旧偏移在末尾通常表示已读完，但明文可能被替换成不同内容；
                # 有旧压缩进度时必须先核对前缀。
                if prev_done and not _same_prefix(old_data, new_data, prev_done):
                    transcripts[old_rel] = 0
                    transcripts[new_rel] = 0
                    compressed[new_rel] = 0
                else:
                    transcripts[new_rel] = new_size
                    compressed[new_rel] = total
            else:
                end = min(old_off, len(new_data))
                consumed = min(new_data.count(b"\n", 0, end), total)
                # dreaming 可能已经通过压缩文件读过更新的进度；重新压缩不能
                # 把已读行数回滚。但只有明文前缀与压缩内容一致时才能沿用旧
                # 进度，文件被替换成不同内容时必须从头处理。
                done = consumed
                if prev_done:
                    done = max(consumed, prev_done) if _same_prefix(
                        old_data, new_data, prev_done) else 0
                compressed[new_rel] = done
                transcripts[new_rel] = new_size if done >= total else 0
        elif isinstance(old_off, int):
            # 旧偏移越过明文末尾，说明文件被截断或替换；旧进度不可信。
            transcripts[old_rel] = 0
            transcripts[new_rel] = 0
            compressed[new_rel] = 0
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(state_path)


def _lock_vault(vault: Path):
    """获取与 dreaming 相同的 vault 锁，避免并发改写 .dream-state。"""
    lock_path = Path(tempfile.gettempdir(), "amem-lock-" + hashlib.sha1(
        str(vault.resolve()).encode()).hexdigest()[:12] + ".lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise RuntimeError("另一个 dreaming 或 compact 正在使用同一个 vault") from None
    return fh


def compact(root: Path, *, older_than_days: int, delete_raw: bool = False,
            dry_run: bool = False, vault: Path | None = None) -> Stats:
    stats = Stats()
    lock = None
    if delete_raw and not dry_run and vault is not None:
        try:
            lock = _lock_vault(vault)
        except (OSError, RuntimeError) as e:
            stats.errors += 1
            print(f"未执行压缩：{e}", file=sys.stderr)
            return stats
    try:
        cutoff = time.time() - older_than_days * 86400
        for device, agent, _, path, rel in _iter_sessions(root):
            if is_compressed(path):
                continue
            try:
                st = path.stat()
                if st.st_mtime >= cutoff:
                    continue
                size, mtime_ns = st.st_size, st.st_mtime_ns
            except OSError as e:
                stats.errors += 1
                print(f"跳过（无法读取）: {path}: {e}", file=sys.stderr)
                continue
            stats.bytes_in += size
            if dry_run:
                stats.files += 1
                continue

            # 对话层可能过期或不存在；写失败不阻止压缩，压缩文件仍是完整备份。
            try:
                _write_conversation(root, device, agent, path, rel, force=False, stats=Stats())
            except (OSError, RuntimeError) as e:
                stats.errors += 1
                print(f"对话层生成失败（仍压缩原始文件）: {path}: {e}", file=sys.stderr)

            zst = path.with_name(path.name + ".zst")
            # 覆盖旧 zst 前先核对明文前缀：不一致说明文件被替换，旧行进度
            # 不能套到新内容上。
            trust_prev = True
            if delete_raw and vault is not None:
                prev_done = _state_compressed_lines(vault, zst.relative_to(root).as_posix())
                if prev_done:
                    try:
                        trust_prev = zst.exists() and prefix_matches_lines(path, zst, prev_done)
                    except (OSError, RuntimeError):
                        trust_prev = False
            try:
                source_hash = _sha256_file(path)
                if zst.exists():
                    try:
                        same = _sha256_bytes(_decompress(zst)) == source_hash
                    except RuntimeError:
                        same = False
                else:
                    same = False
                if not same:
                    _write_zstd(zst, path.read_bytes(), _zstd())
                    if _sha256_bytes(_decompress(zst)) != source_hash:
                        raise RuntimeError(f"压缩校验失败，保留明文: {path}")
            except (OSError, RuntimeError) as e:
                stats.errors += 1
                print(f"压缩失败（保留明文）: {path}: {e}", file=sys.stderr)
                continue

            stats.files += 1
            stats.bytes_out += zst.stat().st_size
            if delete_raw:
                try:
                    current = path.stat()
                    if current.st_size != size or current.st_mtime_ns != mtime_ns:
                        # rsync 在压缩期间追加了新内容；这些内容不在 zst 里，
                        # 删除明文会永久丢失，保留到下一轮重新压缩。
                        raise RuntimeError("明文在压缩期间被修改，保留明文")
                    if vault is not None:
                        _migrate_state(vault, root, path, zst, trust_prev=trust_prev)
                    path.unlink()
                    stats.deleted += 1
                except (OSError, RuntimeError) as e:
                    stats.errors += 1
                    print(f"未删除明文（{e}）: {path}", file=sys.stderr)
        return stats
    finally:
        if lock is not None:
            lock.close()


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def stats(root: Path) -> dict[str, int]:
    out = {
        "raw": _tree_size(root) - _tree_size(root / CONVERSATION_DIR),
        "conversation": _tree_size(root / CONVERSATION_DIR),
    }
    compressed = sum(p.stat().st_size for p in root.rglob("*")
                     if p.is_file() and p.name.endswith((".zst", ".zstd"))
                     and CONVERSATION_DIR not in p.relative_to(root).parts)
    out["raw_compressed"] = compressed
    out["total"] = _tree_size(root)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m amem.archive",
                                 description="归档对话层抽取、压缩与统计")
    sub = ap.add_subparsers(dest="cmd", required=True)

    conv = sub.add_parser("conversation", help="生成/更新对话层")
    conv.add_argument("--archive", type=Path, default=Path(os.environ.get(
        "AMEM_ARCHIVE", "~/transcript-archive")).expanduser())
    conv.add_argument("--dry-run", action="store_true")
    conv.add_argument("--force", action="store_true", help="即使对话层比源文件新也重建")

    comp = sub.add_parser("compact", help="把超过保留期的明文转录压成 .jsonl.zst")
    comp.add_argument("--archive", type=Path, default=Path(os.environ.get(
        "AMEM_ARCHIVE", "~/transcript-archive")).expanduser())
    comp.add_argument("--older-than", type=int, default=180, metavar="DAYS")
    comp.add_argument("--delete-raw", action="store_true", help="校验压缩后删除明文")
    comp.add_argument("--dry-run", action="store_true")
    comp.add_argument("--vault", type=Path, default=Path(os.environ.get(
        "AMEM_VAULT", "~/memory-vault")).expanduser(),
        help="同时迁移 .dream-state 偏移（默认 AMEM_VAULT 或 ~/memory-vault）")

    st = sub.add_parser("stats", help="按层统计体积")
    st.add_argument("--archive", type=Path, default=Path(os.environ.get(
        "AMEM_ARCHIVE", "~/transcript-archive")).expanduser())

    a = ap.parse_args(argv)
    root = a.archive.expanduser()
    if a.cmd == "conversation":
        s = build_conversation(root, dry_run=a.dry_run, force=a.force)
        size = (f"原始 {s.bytes_in / 1e6:.1f}MB（dry-run 未写盘）" if a.dry_run
                else f"{s.bytes_in / 1e6:.1f}MB -> {s.bytes_out / 1e6:.1f}MB")
        print(f"{'dry-run · ' if a.dry_run else ''}对话层: {s.files} 个文件，"
              f"{s.messages} 条消息，跳过 {s.skipped}，失败 {s.errors}；{size}")
        return 1 if s.errors else 0
    if a.cmd == "compact":
        s = compact(root, older_than_days=a.older_than, delete_raw=a.delete_raw,
                    dry_run=a.dry_run, vault=a.vault)
        size = (f"原始 {s.bytes_in / 1e6:.1f}MB（dry-run 未写盘）" if a.dry_run
                else f"{s.bytes_in / 1e6:.1f}MB -> {s.bytes_out / 1e6:.1f}MB")
        print(f"{'dry-run · ' if a.dry_run else ''}压缩: {s.files} 个文件，失败 {s.errors}；"
              f"{size}"
              f"{f'；明文已删除 {s.deleted} 个' if a.delete_raw and not a.dry_run else ''}")
        return 1 if s.errors else 0
    s = stats(root)
    print("归档体积: " + "，".join(f"{k}={v / 1e6:.1f}MB" for k, v in s.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
