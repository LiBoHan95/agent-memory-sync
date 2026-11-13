import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

from amem import dream, gate
from tests.helpers import NoScanner, make_vault

FAKE = Path(__file__).with_name("fake_agent.py")
LINE = {"type": "user", "message": {"content": "以后都用中文"}, "promptSource": "typed",
        "origin": {"kind": "human"}, "timestamp": "2026-11-13T06:00:00Z", "sessionId": "s1", "cwd": "/w"}


class DreamTest(unittest.TestCase):
    def setUp(self):
        self.vault = make_vault()
        self.archive = self.vault.parent / "archive"
        d = self.archive / "mac" / "claude-code" / "proj"
        d.mkdir(parents=True)
        self.jsonl = d / "s1.jsonl"
        self.jsonl.write_text(json.dumps(LINE, ensure_ascii=False) + "\n")
        (self.archive / "mac" / "cursor").mkdir()
        (self.vault / "log/2026-11.mac.md").write_text(
            "## 2026-11-13 14:20 · codex@mac\n- [preference] 用中文回答\n")
        self.cfg = dream.Config(vault=self.vault, archive=self.archive,
                                agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                                report_dir=self.vault.parent / "reports")
        os.environ.pop("FAKE_MODE", None)

    def run_once(self, scanner=None):
        return dream.run(self.cfg, scanner=scanner or NoScanner(), now=datetime(2026, 11, 14, 4))

    def git_log(self):
        return subprocess.run(["git", "-C", str(self.vault), "log", "--format=%s"],
                              capture_output=True, text=True).stdout.splitlines()

    def test_full_run(self):
        r = self.run_once()
        self.assertEqual(r.status, "ok")
        self.assertTrue((self.vault / "projects/demo.md").exists())
        self.assertFalse((self.vault / "rules/answer-first.md").exists())
        self.assertTrue((self.vault / "AGENT_PROTOCOL.md").exists())
        self.assertFalse((self.vault / "evil.sh").exists())
        self.assertIn("answer-first", (self.vault / "proposals/2026-11-14.md").read_text())
        report = r.report.read_text()
        for s in ("已拒绝", "AGENT_PROTOCOL.md", "evil.sh", "mac/cursor", "转录消息 1 条"):
            self.assertIn(s, report)
        state = json.loads((self.vault / dream.STATE_FILE).read_text())
        self.assertEqual(state["transcripts"]["mac/claude-code/proj/s1.jsonl"], self.jsonl.stat().st_size)
        self.assertEqual(self.run_once().status, "empty")  # 没有新增量

    def test_agent_failure_keeps_state(self):
        os.environ["FAKE_MODE"] = "fail"
        r = self.run_once()
        self.assertEqual(r.status, "failed")
        self.assertFalse((self.vault / dream.STATE_FILE).exists())
        self.assertEqual(len(self.git_log()), 1)

    def test_secret_blocked(self):
        os.environ["FAKE_MODE"] = "secret"
        r = self.run_once(scanner=gate.RegexScanner())
        self.assertFalse((self.vault / "projects/demo.md").exists())
        self.assertIn("疑似密钥", r.report.read_text())

    def test_missing_gitleaks_fails_closed(self):
        r = self.run_once(scanner=gate.GitleaksScanner(binary="definitely-not-gitleaks"))
        self.assertEqual(r.status, "failed")
        self.assertFalse((self.vault / "projects/demo.md").exists())
        self.assertFalse((self.vault / dream.STATE_FILE).exists())

    def test_dry_run_writes_nothing(self):
        self.cfg.dry_run = True
        r = self.run_once()
        self.assertEqual(r.status, "dry-run")
        self.assertFalse((self.vault / "projects/demo.md").exists())
        self.assertFalse((self.vault / dream.STATE_FILE).exists())

    def tearDown(self):
        os.environ.pop("FAKE_MODE", None)


if __name__ == "__main__":
    unittest.main()
