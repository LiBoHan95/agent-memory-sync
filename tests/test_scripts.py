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

    def test_commit_ignores_global_signing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            vault = make_vault(tmp)
            git(vault, "config", "commit.gpgsign", "true")
            git(vault, "config", "gpg.program", "false")
            (vault / "log" / "2026-11.test.md").write_text(
                "## 2026-11-13 15:00 · test@device\n- [fact] 测试\n", encoding="utf-8")
            env = {"AMEM_VAULT": str(vault)}
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

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "all"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

            claude_md = (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("AGENT_PROTOCOL.md", claude_md)
            self.assertIn("<!-- amem:begin -->", claude_md)
            self.assertIn("<!-- amem:end -->", claude_md)
            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["theme"], "dark")
            self.assertEqual(settings["cleanupPeriodDays"], 36500)
            self.assertIn(str(vault), settings["permissions"]["additionalDirectories"])
            self.assertTrue(list((home / ".claude").glob("settings.json.bak.*")))

            for rel in (".codex/AGENTS.md", ".dsh/AGENTS.md", ".gemini/GEMINI.md",
                        ".pi/agent/AGENTS.md"):
                self.assertIn("AGENT_PROTOCOL.md", (home / rel).read_text(encoding="utf-8"))

            stignore = vault / ".stignore"
            self.assertTrue(stignore.exists())
            self.assertIn(".git", stignore.read_text(encoding="utf-8"))

            before = (home / ".claude" / "settings.json").read_text(encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "all"], env=env)
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

    def test_install_auto_only_touches_installed_agents(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            vault = make_vault(tmp)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh")], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("本次接入: claude-code", r.stdout)

            self.assertIn("AGENT_PROTOCOL.md",
                          (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))
            for rel in (".codex/AGENTS.md", ".dsh/AGENTS.md", ".gemini/GEMINI.md",
                        ".pi/agent/AGENTS.md"):
                self.assertFalse((home / rel).exists(), rel)

    def test_install_updates_block_when_vault_changes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            base_a = tmp / "a"
            base_b = tmp / "b"
            base_a.mkdir()
            base_b.mkdir()
            vault_a = make_vault(base_a)
            vault_b = make_vault(base_b)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault_a),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            claude_md = home / ".claude" / "CLAUDE.md"
            self.assertIn(str(vault_a), claude_md.read_text(encoding="utf-8"))

            env["AMEM_VAULT"] = str(vault_b)
            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = claude_md.read_text(encoding="utf-8")
            self.assertIn(str(vault_b), text)
            self.assertNotIn(str(vault_a), text)
            self.assertEqual(text.count("<!-- amem:begin -->"), 1)
            self.assertEqual(text.count("<!-- amem:end -->"), 1)

    def test_install_ignores_unrelated_protocol_mention(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            claude_md = home / ".claude" / "CLAUDE.md"
            claude_md.write_text("# 笔记\n参考 /work/AGENT_PROTOCOL.md\n", encoding="utf-8")
            vault = make_vault(tmp)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = claude_md.read_text(encoding="utf-8")
            self.assertIn("参考 /work/AGENT_PROTOCOL.md", text)
            self.assertIn(str(vault), text)
            self.assertEqual(text.count("<!-- amem:begin -->"), 1)

    def test_install_updates_legacy_block_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            base_a = tmp / "a"
            base_b = tmp / "b"
            base_a.mkdir()
            base_b.mkdir()
            vault_a = make_vault(base_a)
            vault_b = make_vault(base_b)
            claude_md = home / ".claude" / "CLAUDE.md"
            claude_md.write_text(
                "<!-- amem: 共享记忆 -->\n"
                f"@{vault_a}/AGENT_PROTOCOL.md\n"
                f"@{vault_a}/MEMORY.md\n"
                "用户自己的笔记：参见 /work/vault/MEMORY.md，很重要\n",
                encoding="utf-8",
            )
            settings = home / ".claude" / "settings.json"
            settings.write_text(json.dumps({
                "permissions": {"additionalDirectories": [str(vault_a)]}
            }), encoding="utf-8")
            env = {"HOME": str(home), "AMEM_VAULT": str(vault_b),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            text = claude_md.read_text(encoding="utf-8")
            self.assertNotIn(str(vault_a), text)
            self.assertNotIn("<!-- amem: 共享记忆 -->", text)
            self.assertIn(str(vault_b), text)
            self.assertIn("用户自己的笔记", text)
            self.assertEqual(text.count("<!-- amem:begin -->"), 1)

            data = json.loads(settings.read_text(encoding="utf-8"))
            dirs = data["permissions"]["additionalDirectories"]
            self.assertIn(str(vault_b), dirs)
            self.assertNotIn(str(vault_a), dirs)

    def test_install_does_not_duplicate_tilde_settings(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            (home / ".claude").mkdir(parents=True)
            vault = make_vault(home)
            settings = home / ".claude" / "settings.json"
            settings.write_text(json.dumps({
                "permissions": {"additionalDirectories": ["~/vault"]}
            }), encoding="utf-8")
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["permissions"]["additionalDirectories"], ["~/vault"])

    def test_install_rejects_incomplete_markers(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            claude_md = home / ".claude" / "CLAUDE.md"
            original = "<!-- amem:begin -->\n用户内容\n"
            claude_md.write_text(original, encoding="utf-8")
            vault = make_vault(tmp)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("不配对", r.stderr)
            self.assertEqual(claude_md.read_text(encoding="utf-8"), original)

    def test_install_preserves_crlf_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            (home / ".claude").mkdir(parents=True)
            claude_md = home / ".claude" / "CLAUDE.md"
            original = "用户内容\r\n\r\n".encode()
            claude_md.write_bytes(original)
            vault = make_vault(tmp)
            env = {"HOME": str(home), "AMEM_VAULT": str(vault),
                   "AMEM_PROJECT_ROOTS": "", "AMEM_SKIP_SCHEDULER": "1"}

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            after = claude_md.read_bytes()
            self.assertTrue(after.startswith(original))
            self.assertIn(b"<!-- amem:begin -->\r\n", after)

            r = run([BASH, str(SCRIPTS / "install.sh"), "--agents", "claude-code"], env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(claude_md.read_bytes(), after)

    def test_install_requires_absolute_vault(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            env = {"HOME": str(home), "AMEM_VAULT": "relative/vault",
                   "AMEM_PROJECT_ROOTS": ""}
            r = run([BASH, str(SCRIPTS / "install.sh"), "--dry-run"], env=env)
            self.assertEqual(r.returncode, 2)
            self.assertIn("绝对路径", r.stderr)

    def test_check_reports_missing_vault(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"AMEM_VAULT": str(Path(td) / "missing"), "AMEM_PROJECT_ROOTS": ""}
            r = run([BASH, str(SCRIPTS / "check-agents.sh")], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("vault 不存在", r.stdout)


class ServerSetupScriptTest(unittest.TestCase):
    def _env(self, tmp: Path) -> dict[str, str]:
        home = tmp / "home"
        home.mkdir(exist_ok=True)
        return {
            "HOME": str(home),
            "AMEM_VAULT": str(tmp / "vault"),
            "AMEM_ARCHIVE": str(tmp / "archive"),
        }

    def test_dry_run_covers_vault_web_and_syncthing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._env(tmp)
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--init-vault", "--dry-run"],
                    env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("从 vault-template 建立 vault", r.stdout)
            self.assertIn("amem-web", r.stdout)
            self.assertIn("添加文件夹 memory-vault", r.stdout)
            self.assertIn("Web 令牌", r.stdout)
            self.assertFalse((tmp / "vault").exists())

    def test_refuses_non_vault_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._env(tmp)
            vault = tmp / "vault"
            vault.mkdir()
            (vault / "keep.txt").write_text("x", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--init-vault", "--dry-run"],
                    env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("拒绝初始化", r.stdout)
            self.assertIn("跳过 Syncthing 文件夹添加", r.stdout)
            self.assertNotIn("添加文件夹 memory-vault ->", r.stdout)

    def test_dry_run_skips_commit_for_existing_git_vault(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._env(tmp)
            vault = make_vault(tmp)
            (vault / "pending.md").write_text("未提交\n", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--init-vault", "--dry-run"],
                    env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("vault 已有提交，跳过 git init/commit", r.stdout)
            self.assertNotIn("从 vault-template 建立 vault", r.stdout)

    def test_bootstrap_dry_run_delegates_to_setup(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._env(tmp)
            env["AMEM_REPO"] = str(ROOT)
            r = run([BASH, str(SCRIPTS / "bootstrap-server.sh"), "--dry-run"], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("从 vault-template 建立 vault", r.stdout)

    def test_dry_run_without_vault_skips_web(self):
        with tempfile.TemporaryDirectory() as td:
            env = self._env(Path(td))
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--dry-run"], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("vault 不存在或缺少 AGENT_PROTOCOL.md/MEMORY.md", r.stdout)
            self.assertIn("跳过启用 amem-web", r.stdout)
            self.assertIn("跳过启用 amem-dream.timer", r.stdout)
            self.assertIn("跳过 autocommit", r.stdout)

    def test_dry_run_starts_web_without_git(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = self._env(tmp)
            vault = tmp / "vault"
            vault.mkdir()
            (vault / "AGENT_PROTOCOL.md").write_text("p", encoding="utf-8")
            (vault / "MEMORY.md").write_text("m", encoding="utf-8")
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--dry-run"], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("缺少 .git", r.stdout)
            self.assertNotIn("跳过启用 amem-web", r.stdout)
            self.assertIn("enable amem-web", r.stdout)
            self.assertIn("跳过启用 amem-dream.timer", r.stdout)
            self.assertIn("跳过 autocommit", r.stdout)

    def test_dry_run_brackets_ipv6_web_url(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            base_env = self._env(tmp)
            vault = tmp / "vault"
            vault.mkdir()
            (vault / "AGENT_PROTOCOL.md").write_text("p", encoding="utf-8")
            (vault / "MEMORY.md").write_text("m", encoding="utf-8")
            for host, expected in [("::1", "[::1]"), ("::", "[::1]"),
                                   ("0.0.0.0", "127.0.0.1")]:
                with self.subTest(host=host):
                    env = dict(base_env, AMEM_WEB_HOST=host)
                    r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--dry-run"],
                            env=env)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn(f"curl http://{expected}:8787/healthz", r.stdout)
                    self.assertIn(f"Web:    http://{expected}:8787/", r.stdout)

    def test_dry_run_updates_legacy_autocommit_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            fake = bindir / "crontab"
            fake.write_text(
                "#!/bin/sh\n"
                f'[ "$1" = "-l" ] && echo "0 * * * * {ROOT}/scripts/autocommit.sh"\n'
                "exit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            env = self._env(tmp)
            make_vault(tmp)
            env["PATH"] = f"{bindir}:{os.environ.get('PATH', '')}"
            r = run([BASH, str(SCRIPTS / "setup-server.sh"), "--init-vault", "--dry-run"],
                    env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("crontab 更新:", r.stdout)


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
            (home / ".dsh" / "sessions" / "p").mkdir(parents=True)
            (home / ".dsh" / "sessions" / "p" / "session.v4.jsonl.zstd").write_text(
                "d\n", encoding="utf-8")
            (home / ".pi" / "agent" / "sessions" / "p").mkdir(parents=True)
            (home / ".pi" / "agent" / "sessions" / "p" / "s.jsonl").write_text("p\n", encoding="utf-8")

            r = run([BASH, str(SCRIPTS / "archive-transcripts.sh")], env=self._env(tmp, home))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

            base = tmp / "target" / "testmac"
            self.assertTrue((base / "claude-code" / "p1" / "s1.jsonl").exists())
            self.assertTrue((base / "claude-code" / "transcripts" / "old.jsonl").exists())
            self.assertTrue((base / "codex" / "2026" / "r.jsonl").exists())
            self.assertTrue((base / "codex" / "archived_sessions" / "r.jsonl.zst").exists())
            self.assertTrue((base / "dsh" / "p" / "session.v4.jsonl.zstd").exists())
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
