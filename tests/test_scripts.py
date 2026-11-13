"""运维脚本的冒烟测试：语法、autocommit、install/check、dream 前置检查与空跑。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TEMPLATE = ROOT / "vault-template"

BASH = shutil.which("bash") or "/bin/bash"

GIT_ID = {
    "GIT_AUTHOR_NAME": "amem-test",
    "GIT_AUTHOR_EMAIL": "amem-test@example.com",
    "GIT_COMMITTER_NAME": "amem-test",
    "GIT_COMMITTER_EMAIL": "amem-test@example.com",
}


def run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path | None = None):
    full_env = dict(os.environ)
    full_env.update(GIT_ID)
    if env:
        full_env.update(env)
    return subprocess.run(cmd, text=True, capture_output=True, env=full_env, cwd=cwd)


def make_vault(tmp: Path) -> Path:
    vault = tmp / "vault"
    shutil.copytree(TEMPLATE, vault)
    run(["git", "init", "-q"], cwd=vault)
    run(["git", "add", "-A"], cwd=vault)
    r = run(["git", "commit", "-qm", "init"], cwd=vault)
    assert r.returncode == 0, r.stderr
    return vault


def git(vault: Path, *args: str) -> str:
    r = run(["git", *args], cwd=vault)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class ScriptSyntaxTest(unittest.TestCase):
    def test_bash_syntax(self):
        scripts = sorted(SCRIPTS.glob("*.sh"))
        self.assertTrue(scripts)
        for path in scripts:
            with self.subTest(script=path.name):
                r = run([BASH, "-n", str(path)])
                self.assertEqual(r.returncode, 0, r.stderr)


class AutocommitTest(unittest.TestCase):
    def test_commit_and_ignore_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = make_vault(tmp)
            env = {"AMEM_VAULT": str(vault)}

            # 无变更不产生 commit
            before = git(vault, "rev-list", "--count", "HEAD")
            r = run([BASH, str(SCRIPTS / "autocommit.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(git(vault, "rev-list", "--count", "HEAD"), before)

            # 新 log 会产生 auto: 提交
            (vault / "log" / "2026-11.test.md").write_text(
                "## 2026-11-13 15:00 · test@device\n- [fact] 测试\n", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "autocommit.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(git(vault, "log", "-1", "--format=%s").startswith("auto: "))

            # .dream-state 被 vault 的 .gitignore 排除
            (vault / ".dream-state").write_text("{}", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "autocommit.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn(".dream-state", git(vault, "status", "--porcelain"))


class InstallCheckTest(unittest.TestCase):
    def test_install_idempotent_and_check_passes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(
                '{\n  "theme": "dark"\n}\n', encoding="utf-8")
            vault = make_vault(tmp)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

            claude_md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("AGENT_PROTOCOL.md", claude_md)
            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "dark")
            self.assertEqual(settings["cleanupPeriodDays"], 36500)
            self.assertIn(str(vault), settings["permissions"]["additionalDirectories"])
            self.assertTrue(list((home / ".claude").glob("settings.json.bak.*")))

            for rel in (".codex/AGENTS.md", ".gemini/GEMINI.md", ".pi/agent/AGENTS.md"):
                self.assertIn("AGENT_PROTOCOL.md", (home / rel).read_text(encoding="utf-8"))

            stignore = vault / ".stignore"
            self.assertTrue(stignore.exists())
            self.assertIn(".git", stignore.read_text(encoding="utf-8"))

            before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "install.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((home / ".claude" / "settings.json").read_text(encoding="utf-8"), before)
            self.assertEqual(stignore.read_text(encoding="utf-8"),
                             ".git\n.DS_Store\n.dream-state\n.*.amem-tmp\n")

            r = run([BASH, str(SCRIPTS / "check-agents.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            if sys.platform == "darwin":
                plist = home / "Library" / "LaunchAgents" / "io.github.amem.archive.plist"
                self.assertTrue(plist.exists())
                self.assertIn(str(SCRIPTS / "archive-transcripts.sh"),
                              plist.read_text(encoding="utf-8"))

    def test_check_reports_missing_vault(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"AMEM_VAULT": str(Path(td) / "missing"), "AMEM_PROJECT_ROOTS": ""}
            r = run([BASH, str(SCRIPTS / "check-agents.sh")], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("vault 不存在", r.stdout)


class DreamScriptTest(unittest.TestCase):
    def test_help(self):
        r = run([BASH, str(SCRIPTS / "dream.sh"), "--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("用法", r.stdout)

    def test_preflight_requires_gitleaks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            (bindir / "python3").symlink_to(sys.executable)
            env = {
                "PATH": str(bindir),
                "AMEM_VAULT": str(tmp / "vault"),
                "AMEM_ARCHIVE": str(tmp / "archive"),
                "AMEM_DREAM_LOG": str(tmp / "dream.log"),
            }
            (tmp / "vault").mkdir()
            (tmp / "archive").mkdir()
            r = run([BASH, str(SCRIPTS / "dream.sh")], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("gitleaks", r.stderr)

    def test_empty_run_through_gate(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = make_vault(tmp)
            archive = tmp / "archive"
            archive.mkdir()
            (vault / "log" / "2026-11.test.md").write_text(
                "## 2026-11-13 15:00 · test@device\n- [fact] 测试\n", encoding="utf-8")

            bindir = tmp / "bin"
            bindir.mkdir()
            fake = bindir / "gitleaks"
            fake.write_text(
                "#!/bin/sh\n"
                "report=''; prev=''\n"
                "for a in \"$@\"; do [ \"$prev\" = '--report-path' ] && report=\"$a\"; prev=\"$a\"; done\n"
                "[ -n \"$report\" ] && printf '[]' > \"$report\"\n"
                "exit 0\n", encoding="utf-8")
            fake.chmod(0o755)

            env = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                "AMEM_VAULT": str(vault),
                "AMEM_ARCHIVE": str(archive),
                "AMEM_DREAM_LOG": str(tmp / "dream.log"),
                "AMEM_AGENT_CMD": "true {prompt}",
            }
            r = run([BASH, str(SCRIPTS / "dream.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ok · 报告", (tmp / "dream.log").read_text(encoding="utf-8"))
            reports = list((tmp / "dream-reports").glob("*.md"))
            self.assertEqual(len(reports), 1)


class ArchiveScriptTest(unittest.TestCase):
    def _env(self, tmp: Path, home: Path, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(home),
            "AMEM_DEVICE": "testmac",
            "AMEM_ARCHIVE_TARGET": str(tmp / "target"),
            "AMEM_ARCHIVE_LOG": str(tmp / "archive.log"),
            "AMEM_ARCHIVE_LOCK": str(tmp / "lock"),
            "AMEM_ARCHIVE_CONFIG": str(tmp / "no.env"),
        }
        env.update(extra)
        return env

    def test_archive_local_target_without_delete(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            src = home / ".claude" / "projects" / "p1"
            src.mkdir(parents=True)
            (src / "s1.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
            (home / ".claude" / "transcripts").mkdir()
            (home / ".claude" / "transcripts" / "old.jsonl").write_text("o\n", encoding="utf-8")
            (home / ".codex" / "sessions" / "2026").mkdir(parents=True)
            (home / ".codex" / "sessions" / "2026" / "r.jsonl").write_text("x\n", encoding="utf-8")
            (home / ".codex" / "archived_sessions").mkdir()
            (home / ".codex" / "archived_sessions" / "r.jsonl.zst").write_text("z\n", encoding="utf-8")
            (home / ".pi" / "agent" / "sessions" / "p").mkdir(parents=True)
            (home / ".pi" / "agent" / "sessions" / "p" / "s.jsonl").write_text("p\n", encoding="utf-8")

            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh")], env=self._env(tmp, home))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            base = tmp / "target" / "testmac"
            self.assertTrue((base / "claude-code" / "p1" / "s1.jsonl").exists())
            self.assertTrue((base / "claude-code" / "transcripts" / "old.jsonl").exists())
            self.assertTrue((base / "codex" / "2026" / "r.jsonl").exists())
            self.assertTrue((base / "codex" / "archived_sessions" / "r.jsonl.zst").exists())
            self.assertTrue((base / "pi" / "p" / "s.jsonl").exists())

            # 本地删除后，归档保留（rsync 不带 --delete）
            (src / "s1.jsonl").unlink()
            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh")], env=self._env(tmp, home))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((base / "claude-code" / "p1" / "s1.jsonl").exists())

    def test_dry_run_does_not_copy(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            src = home / ".claude" / "projects" / "p1"
            src.mkdir(parents=True)
            (src / "s1.jsonl").write_text("x\n", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh"), "--dry-run"],
                    env=self._env(tmp, home))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(
                (tmp / "target" / "testmac" / "claude-code" / "p1" / "s1.jsonl").exists())

    def test_remote_target_creates_destination_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude" / "projects" / "p1").mkdir(parents=True)
            (home / ".claude" / "projects" / "p1" / "s1.jsonl").write_text("x\n", encoding="utf-8")
            # 只有 archived_sessions，没有 sessions；父目录 codex 不存在
            (home / ".codex" / "archived_sessions").mkdir(parents=True)
            (home / ".codex" / "archived_sessions" / "r.jsonl").write_text("z\n", encoding="utf-8")
            (home / ".pi" / "agent" / "sessions" / "p").mkdir(parents=True)
            (home / ".pi" / "agent" / "sessions" / "p" / "s.jsonl").write_text("p\n", encoding="utf-8")

            bindir = tmp / "bin"
            bindir.mkdir()
            ssh_log = tmp / "ssh.log"
            rsync_log = tmp / "rsync.log"
            # 模拟会读 stdin 的 ssh：如果脚本没把 stdin 重定向，会吞掉 while read 的后续行
            ssh = bindir / "ssh"
            ssh.write_text(
                "#!/bin/sh\n"
                "cat >/dev/null\n"
                "printf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n", encoding="utf-8")
            ssh.chmod(0o755)
            rsync = bindir / "rsync"
            rsync.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$RSYNC_LOG\"\n", encoding="utf-8")
            rsync.chmod(0o755)

            env = self._env(tmp, home)
            env.update({
                "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                "AMEM_ARCHIVE_TARGET": "server:~/transcript-archive",
                "AMEM_ARCHIVE_SSH_OPTS": "-p 2222",
                "SSH_LOG": str(ssh_log),
                "RSYNC_LOG": str(rsync_log),
            })
            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            ssh_calls = ssh_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(
                "mkdir -p $HOME/transcript-archive/testmac/codex/archived_sessions" in line
                for line in ssh_calls), ssh_calls)
            self.assertTrue(all("-p 2222" in line for line in ssh_calls), ssh_calls)
            rsync_calls = rsync_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rsync_calls), 3, rsync_calls)

    def test_missing_target_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "AMEM_ARCHIVE_CONFIG": str(tmp / "no.env"),
                "AMEM_ARCHIVE_TARGET": "",
                "AMEM_ARCHIVE_HOST": "",
            }
            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh")], env=env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("未配置归档目标", r.stderr)


if __name__ == "__main__":
    unittest.main()
