"""按项目清除原始转录，并删除对应的项目记忆页。

只删除 cwd 位于目标项目目录内的会话文件；log/、profile/、rules/ 不动。
默认 dry-run，加 `--yes` 才真正删除；删除前把每个文件的路径、大小和 sha256
写入清单，之后无法从归档恢复时至少知道删了什么。

用法：
  python3 -m amem.forget --project ~/dev/old-project --dry-run
  python3 -m amem.forget --project ~/dev/old-project --vault ~/memory-vault --yes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import format as fmt
from . import parsers
from .archive import CONVERSATION_DIR, CONVERSATION_MARKER
from .parsers.base import is_transcript, read_head_lines

LOCAL_ROOTS: tuple[tuple[str, str], ...] = (
    ("claude-code", "~/.claude/projects"),
    ("claude-code", "~/.claude/transcripts"),
    ("codex", "~/.codex/sessions"),
    ("codex", "~/.codex/archived_sessions"),
    ("dsh", "~/.dsh/sessions"),
    ("pi", "~/.pi/agent/sessions"),
)
SKIP_DIRS = frozenset({"conversation", ".amem-forget", ".git"})
WORKSPACE_NAMES = frozenset({"dev", "development", "workspace", "projects", "src",
                             "code", "repos", "git", "work"})


@dataclass(frozen=True)
class Candidate:
    path: Path
    root: Path
    agent: str
    cwd: str
    size: int


def _norm(path: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def _session_cwd(path: Path, agent: str) -> str:
    """只读文件开头几行，拿会话的 cwd。读不到返回空字符串。"""
    for line in read_head_lines(path, 20):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if agent == "codex" and entry.get("type") == "session_meta":
            payload = entry.get("payload")
            if isinstance(payload, dict) and payload.get("cwd"):
                return str(payload["cwd"])
        elif agent in ("pi", "dsh") and entry.get("type") == "session":
            if entry.get("cwd"):
                return str(entry["cwd"])
        elif entry.get("cwd"):
            return str(entry["cwd"])
    return ""


def _in_project(cwd: str, project: Path) -> bool:
    if not cwd:
        return False
    try:
        path = Path(cwd).resolve()
    except OSError:
        return False
    return path == project or project in path.parents


def _candidates_local(roots: tuple[tuple[str, str], ...],
                      errors: list[str] | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    for agent, raw in roots:
        root = Path(os.path.expanduser(raw))
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file() or not is_transcript(path):
                continue
            try:
                cwd = _session_cwd(path, agent)
            except (OSError, RuntimeError) as e:
                if errors is not None:
                    errors.append(f"{path}: {e}")
                continue
            if not cwd:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            out.append(Candidate(path, root, agent, cwd, size))
    return out


def _candidates_archive(root: Path,
                        errors: list[str] | None = None) -> list[Candidate]:
    out: list[Candidate] = []
    if not root.is_dir():
        return out
    for device_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if device_dir.name.startswith(".") or device_dir.name in SKIP_DIRS:
            continue
        for agent_dir in sorted(p for p in device_dir.iterdir() if p.is_dir()):
            agent = agent_dir.name
            if agent not in parsers.PARSERS:
                continue
            for path in sorted(agent_dir.rglob("*")):
                if path.is_symlink() or not path.is_file() or not is_transcript(path):
                    continue
                try:
                    cwd = _session_cwd(path, agent)
                except (OSError, RuntimeError) as e:
                    if errors is not None:
                        errors.append(f"{path}: {e}")
                    continue
                if not cwd:
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                out.append(Candidate(path, root, agent, cwd, size))
    return out


def _conversation_cwds(path: Path) -> list[str]:
    """解析对话层文件头里的 cwd。

    新格式带版本标记，所有会话头都在第一条消息之前，只看 `## ` 之前即可，
    正文里的 `- session:`/`- cwd:` 不会被当成元数据。旧格式没有标记，只能
    按相邻行尽力扫描（会话头与消息交错）；这些文件会在下一轮
    `conversation` 且原始明文还在时自动重建。
    """
    texts = [line.decode("utf-8", "replace").strip()
             for line in read_head_lines(path, 10000)]
    new_format = any(t == CONVERSATION_MARKER for t in texts[:5])
    out: list[str] = []
    prev = ""
    for text in texts:
        if new_format and text.startswith("## "):
            break
        if text.startswith("- cwd:") and prev.startswith("- session:"):
            cwd = text[len("- cwd:"):].strip()
            if cwd:
                out.append(cwd)
        if text:
            prev = text
    return out


def _candidates_conversation(root: Path, project: Path,
                             errors: list[str] | None = None) -> list[Candidate]:
    """对话层没有原始目录名可依赖，按文件头里的 cwd 匹配。"""
    conv_root = root / CONVERSATION_DIR
    out: list[Candidate] = []
    if not conv_root.is_dir():
        return out
    for path in sorted(conv_root.rglob("*.md.zst")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            cwds = _conversation_cwds(path)
            size = path.stat().st_size
        except (OSError, RuntimeError) as e:
            if errors is not None:
                errors.append(f"{path}: {e}")
            continue
        matched = next((c for c in cwds if _in_project(c, project)), None)
        if matched is not None:
            out.append(Candidate(path, conv_root, "conversation", matched, size))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_empty_dirs(path: Path, root: Path) -> None:
    current = path
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _note_name(project: Path, note: str | None) -> str:
    name = (note or project.name).strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    # 只要求是安全的单层文件名；允许中文和空格，挡住路径穿越与控制字符。
    if (not name or name in (".", "..") or name.startswith(".")
            or "/" in name or "\\" in name
            or any(ch in name for ch in "\x00\r\n")):
        raise ValueError(f"项目页名不合法: {name!r}（不能包含路径分隔符或控制字符）")
    return name


def _backlinks(vault: Path, note: str, filename: str) -> list[str]:
    """查找指向项目页的链接。只查笔记层，不把 log/、proposals/ 当成反链。"""
    esc_note = re.escape(note)
    esc_file = re.escape(filename)
    patterns = [
        # [[target]] / [[target|别名]] / [[target#小节]] / [[projects/target]] / [[target.md]]
        re.compile(r"\[\[(?:projects/)?" + esc_note + r"(?:\.md)?(?=[\]|#])"),
        # [文字](projects/target.md) / (projects/target.md#锚点) / (<projects/target.md> "标题")
        re.compile(r"\]\(\s*<?projects/" + esc_file + r"(?=[#\s)>])[^)]*\)"),
        # 引用式定义：[文字]: projects/target.md
        re.compile(r"^\s*\[[^\]]+\]:\s*<?projects/" + esc_file + r"(?=[\s>]|$)", re.M),
    ]
    out = []
    for path in sorted(vault.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(vault).as_posix()
        if rel.startswith(("log/", "proposals/", ".git/")):
            continue
        if rel in (f"projects/{filename}", "MEMORY.md"):
            continue
        text = fmt.decode(path.read_bytes()) or ""
        if any(p.search(text) for p in patterns):
            out.append(rel)
    return out


def _record(fh, entry: dict) -> None:
    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


def _plan_text(project: Path, found: list[Candidate], note_path: Path | None) -> str:
    total = sum(c.size for c in found)
    lines = [f"项目: {project}", f"匹配会话: {len(found)} 个，共 {total / 1e6:.1f} MB"]
    by_agent: dict[str, list[Candidate]] = {}
    for c in found:
        by_agent.setdefault(c.agent, []).append(c)
    for agent in sorted(by_agent):
        items = by_agent[agent]
        size = sum(c.size for c in items) / 1e6
        lines.append(f"- {agent}: {len(items)} 个，{size:.1f} MB")
    if note_path is not None:
        lines.append(f"项目页: {note_path}（{'存在' if note_path.is_file() else '不存在'}）")
    return "\n".join(lines)


def _note_plan(vault: Path, note: str, *, force: bool) -> tuple[Path, dict | None, str | None, bool]:
    """返回 (页面路径, 待写清单条目, 提示, 是否允许清理索引行)。"""
    filename = f"{note}.md"
    path = vault / "projects" / filename
    if not path.is_file():
        return path, None, f"项目页不存在: projects/{filename}", True
    links = _backlinks(vault, note, filename)
    if links and not force:
        return path, None, ("项目页仍有反链，未删除（加 --force 可强制删除）: "
                            + ", ".join(links)), False
    entry = {"action": "delete", "kind": "note", "status": "pending", "path": str(path),
             "vault_rel": path.relative_to(vault).as_posix(),
             "bytes": path.stat().st_size, "sha256": _sha256(path)}
    return path, entry, None, True


def _remove_index(vault: Path, filename: str, record) -> tuple[str, bool]:
    memory = vault / "MEMORY.md"
    if not memory.is_file():
        return "", False
    text = fmt.decode(memory.read_bytes()) or ""
    pattern = re.compile(r"\].*\(projects/" + re.escape(filename) + r"\)")
    lines = text.splitlines()
    kept = [line for line in lines if not pattern.search(line)]
    if len(kept) == len(lines):
        return "", False
    new = "\n".join(kept) + ("\n" if kept else "")
    # MEMORY.md 通过 Syncthing 分发；中途崩溃不能留下截断的索引。
    # 点前缀与 install/setup 的 `.*.amem-tmp` 忽略规则一致。
    tmp = memory.with_name("." + memory.name + ".amem-tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(memory)
    removed = len(lines) - len(kept)
    record({"action": "modify", "kind": "index", "path": str(memory),
            "removed_lines": removed})
    return f"已从 MEMORY.md 移除 {removed} 行索引", True


def run(project: Path, *, vault: Path | None, archive: Path | None,
        local_roots: tuple[tuple[str, str], ...] | None, note: str | None,
        apply: bool, force: bool, manifest_path: Path) -> int:
    project = _norm(project)
    vault = _norm(vault) if vault is not None else None
    archive = _norm(archive) if archive is not None else None
    home = _norm(Path.home())
    if not force:
        if project in (Path("/"), home) or len(project.parts) < 3:
            print(f"拒绝清除过宽的路径: {project}（加 --force 可覆盖）", file=sys.stderr)
            return 2
        if project.parent == home and project.name.lower() in WORKSPACE_NAMES:
            print(f"拒绝清除工作区根目录: {project}（加 --force 可覆盖）", file=sys.stderr)
            return 2

    scan_errors: list[str] = []
    found: list[Candidate] = []
    if local_roots is not None:
        found.extend(c for c in _candidates_local(local_roots, scan_errors)
                     if _in_project(c.cwd, project))
    if archive is not None:
        found.extend(c for c in _candidates_archive(archive, scan_errors)
                     if _in_project(c.cwd, project))
        found.extend(_candidates_conversation(archive, project, scan_errors))
    found.sort(key=lambda c: str(c.path))
    for e in scan_errors:
        print(f"扫描失败（未纳入删除计划）: {e}", file=sys.stderr)

    note_name = None
    note_path = None
    note_entry = None
    note_message = None
    note_remove_index = False
    if vault is not None:
        try:
            note_name = _note_name(project, note)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        note_path = vault / "projects" / f"{note_name}.md"
        try:
            note_path, note_entry, note_message, note_remove_index = _note_plan(
                vault, note_name, force=force)
        except OSError as e:
            print(f"项目页检查失败: {e}", file=sys.stderr)
            return 1

    print(_plan_text(project, found, note_path))
    if note_message:
        print(note_message)
    if not apply:
        print("\ndry-run：未删除任何文件。确认后加 --yes。")
        if scan_errors:
            print(f"扫描失败 {len(scan_errors)} 个文件，删除计划不完整。", file=sys.stderr)
            return 1
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    planned: list[tuple[Candidate, dict]] = []
    failures = 0
    for c in found:
        try:
            digest = _sha256(c.path)
        except OSError as e:
            failures += 1
            print(f"跳过（无法读取）: {c.path}: {e}", file=sys.stderr)
            continue
        planned.append((c, {
            "ts": now, "action": "delete", "status": "pending",
            "kind": "conversation" if c.agent == "conversation" else "transcript",
            "agent": c.agent, "cwd": c.cwd, "path": str(c.path),
            "bytes": c.size, "sha256": digest,
        }))

    fh = None
    manifest_written = False
    if planned or note_entry is not None or note_remove_index:
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(manifest_path, "a", encoding="utf-8")
            # 删除前先把路径、大小和 sha256 落盘；进程被杀也留得下证据。
            for _, entry in planned:
                _record(fh, entry)
            if note_entry is not None:
                _record(fh, note_entry)
            manifest_written = True
        except OSError as e:
            if fh is not None:
                fh.close()
            print(f"无法写删除清单，未删除任何文件: {manifest_path}: {e}", file=sys.stderr)
            return 1

    removed = 0
    try:
        for c, entry in planned:
            try:
                c.path.unlink()
                _remove_empty_dirs(c.path.parent, c.root)
                removed += 1
            except OSError as e:
                failures += 1
                print(f"删除失败: {c.path}: {e}", file=sys.stderr)
                if fh is not None:
                    try:
                        _record(fh, {**entry, "action": "delete-failed", "error": str(e)})
                    except OSError:
                        pass

        if note_entry is not None:
            try:
                note_path.unlink()
                note_message = f"已删除项目页 {note_entry['vault_rel']}"
            except OSError as e:
                failures += 1
                note_remove_index = False
                print(f"删除项目页失败: {note_path}: {e}", file=sys.stderr)
                if fh is not None:
                    try:
                        _record(fh, {**note_entry, "action": "delete-failed", "error": str(e)})
                    except OSError:
                        pass

        if vault is not None and note_name is not None and note_remove_index:
            try:
                index_message, _ = _remove_index(
                    vault, f"{note_name}.md",
                    lambda e: _record(fh, e) if fh is not None else None)
                if index_message:
                    note_message = f"{note_message}，{index_message}" if note_message else index_message
            except OSError as e:
                failures += 1
                print(f"更新 MEMORY.md 失败: {e}", file=sys.stderr)
    finally:
        if fh is not None:
            fh.close()

    print(f"\n已删除 {removed} 个转录/对话层文件"
          + (f"；失败 {failures} 个" if failures else "")
          + (f"；扫描失败 {len(scan_errors)} 个" if scan_errors else ""))
    if manifest_written:
        print(f"清单: {manifest_path}")
    if note_message:
        print(note_message)
    if vault is not None:
        print("未提交 git；服务器 autocommit 会提交，其他设备通过 Syncthing 收到删除。")
    return 1 if failures or scan_errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m amem.forget",
                                 description="按项目清除原始转录与项目记忆页")
    ap.add_argument("--project", required=True, help="项目根目录（已删除的路径也可以）")
    ap.add_argument("--vault", type=Path, help="记忆库路径；给出时同时删除项目页和索引行")
    ap.add_argument("--archive", type=Path, help="服务器归档根目录（<设备>/<agent>/...）")
    ap.add_argument("--note", help="项目页文件名（不含 .md），默认取项目目录名")
    ap.add_argument("--no-local", action="store_true", help="不扫描本机各 agent 的转录目录")
    ap.add_argument("--yes", action="store_true", help="真正删除；不加时只做 dry-run")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    ap.add_argument("--force", action="store_true", help="允许过宽路径或删除仍有反链的项目页")
    ap.add_argument("--manifest", type=Path,
                    default=Path("~/.config/amem/forget.jsonl").expanduser(),
                    help="删除清单（追加写入）")
    a = ap.parse_args(argv)

    return run(a.project, vault=a.vault, archive=a.archive,
               local_roots=None if a.no_local else LOCAL_ROOTS,
               note=a.note, apply=a.yes and not a.dry_run,
               force=a.force, manifest_path=a.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
