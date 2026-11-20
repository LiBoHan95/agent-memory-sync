import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

from amem import archive, dream, gate
from amem.screen import PassThrough
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

    def _zst(self, path: Path, entries: list[dict], append: bool = False) -> None:
        data = b"".join(json.dumps(e, ensure_ascii=False).encode() + b"\n" for e in entries)
        frame = subprocess.run(["zstd", "-q", "-c", "-"], input=data,
                               capture_output=True, check=True).stdout
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if append else "wb") as f:
            f.write(frame)

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_compressed_append_reads_only_new_lines(self):
        root = self.vault.parent / "archive-zst"
        path = root / "mac/claude-code/z/s.jsonl.zst"
        self._zst(path, [LINE])
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State(), PassThrough())
        self.assertEqual([m.text for m in b.messages], ["以后都用中文"])
        rel = "mac/claude-code/z/s.jsonl.zst"
        self.assertEqual(b.next_state.compressed_lines[rel], 1)

        second = {**LINE, "message": {"content": "第二句"}, "timestamp": "2026-11-13T06:01:00Z"}
        self._zst(path, [second], append=True)
        b2 = dream.collect(cfg, b.next_state, PassThrough())
        self.assertEqual([m.text for m in b2.messages], ["第二句"])

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_keep_raw_compressed_copy_is_not_read_twice(self):
        root = self.vault.parent / "archive-keep"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(LINE, ensure_ascii=False) + "\n", encoding="utf-8")
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State(), PassThrough())
        self.assertEqual(len(b.messages), 1)
        archive.compact(root, older_than_days=0, delete_raw=False)
        self.assertTrue(path.with_name("s.jsonl.zst").exists())
        self.assertTrue(path.exists())
        b2 = dream.collect(cfg, b.next_state, PassThrough())
        self.assertEqual(b2.messages, [])

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_partial_migration_continues_from_unread_tail(self):
        root = self.vault.parent / "archive-partial"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        first = {**LINE, "message": {"content": "第一句"}}
        second = {**LINE, "message": {"content": "第二句"}}
        first_line = json.dumps(first, ensure_ascii=False).encode() + b"\n"
        path.write_bytes(first_line + json.dumps(second, ensure_ascii=False).encode() + b"\n")
        rel = "mac/claude-code/p/s.jsonl"
        dream.State(transcripts={rel: len(first_line)}).save(self.vault)
        archive.compact(root, older_than_days=0, delete_raw=True, vault=self.vault)
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State.load(self.vault), PassThrough())
        self.assertEqual([m.text for m in b.messages], ["第二句"])

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_reuploaded_raw_after_compaction_is_not_replayed(self):
        root = self.vault.parent / "archive-reupload"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        original = json.dumps(LINE, ensure_ascii=False) + "\n"
        path.write_text(original, encoding="utf-8")
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State(), PassThrough())
        self.assertEqual(len(b.messages), 1)
        b.next_state.save(self.vault)

        archive.compact(root, older_than_days=0, delete_raw=True, vault=self.vault)
        self.assertFalse(path.exists())
        # 默认 HOT_DAYS=0 时客户端会把同一份明文重新上传
        path.write_text(original, encoding="utf-8")
        b2 = dream.collect(cfg, dream.State.load(self.vault), PassThrough())
        self.assertEqual(b2.messages, [])

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_partial_migration_progress_not_duplicated_on_reupload(self):
        root = self.vault.parent / "archive-progress"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        first = {**LINE, "message": {"content": "第一句"}}
        second = {**LINE, "message": {"content": "第二句"}}
        first_line = json.dumps(first, ensure_ascii=False).encode() + b"\n"
        original = first_line + json.dumps(second, ensure_ascii=False).encode() + b"\n"
        path.write_bytes(original)
        rel = "mac/claude-code/p/s.jsonl"
        dream.State(transcripts={rel: len(first_line)}).save(self.vault)
        archive.compact(root, older_than_days=0, delete_raw=True, vault=self.vault)
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State.load(self.vault), PassThrough())
        self.assertEqual([m.text for m in b.messages], ["第二句"])
        b.next_state.save(self.vault)

        # 客户端重新上传同一份明文；压缩层已读完第二句，不能再处理一遍
        path.write_bytes(original)
        b2 = dream.collect(cfg, dream.State.load(self.vault), PassThrough())
        self.assertEqual(b2.messages, [])

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_recompact_does_not_roll_back_compressed_progress(self):
        root = self.vault.parent / "archive-progress2"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        first = {**LINE, "message": {"content": "第一句"}}
        second = {**LINE, "message": {"content": "第二句"}}
        first_line = json.dumps(first, ensure_ascii=False).encode() + b"\n"
        original = first_line + json.dumps(second, ensure_ascii=False).encode() + b"\n"
        path.write_bytes(original)
        rel = "mac/claude-code/p/s.jsonl"
        dream.State(transcripts={rel: len(first_line)}).save(self.vault)
        archive.compact(root, older_than_days=0, delete_raw=True, vault=self.vault)
        zst = path.with_name("s.jsonl.zst")
        zrel = "mac/claude-code/p/s.jsonl.zst"

        # 模拟 dreaming 已经通过压缩文件读完第二行
        state = dream.State.load(self.vault)
        state.transcripts[zrel] = zst.stat().st_size
        state.compressed_lines[zrel] = 2
        state.save(self.vault)
        # 明文重新出现后再次 compact；旧偏移只对应第一行，不能把进度回滚
        path.write_bytes(original)
        archive.compact(root, older_than_days=0, delete_raw=True, vault=self.vault)

        state = json.loads((self.vault / dream.STATE_FILE).read_text())
        self.assertEqual(state["compressed_lines"][zrel], 2)
        self.assertEqual(state["transcripts"][zrel], zst.stat().st_size)

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_replaced_raw_is_processed_from_start(self):
        root = self.vault.parent / "archive-replaced"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        old = {**LINE, "message": {"content": "旧一"}}
        old2 = {**LINE, "message": {"content": "旧二"}}
        self._zst(path.with_name("s.jsonl.zst"), [old, old2])
        zrel = "mac/claude-code/p/s.jsonl.zst"
        rel = "mac/claude-code/p/s.jsonl"
        new = [{**LINE, "message": {"content": f"新{i}"}} for i in range(4)]
        path.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in new),
                        encoding="utf-8")
        state = dream.State(transcripts={rel: 0, zrel: path.with_name("s.jsonl.zst").stat().st_size},
                            compressed_lines={zrel: 2})
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, state, PassThrough())
        self.assertEqual([m.text for m in b.messages], ["新0", "新1", "新2", "新3"])

    def test_bad_compressed_state_values_are_ignored(self):
        root = self.vault.parent / "archive-bad-state"
        path = root / "mac/claude-code/p/s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(LINE, ensure_ascii=False) + "\n", encoding="utf-8")
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        state = dream.State(compressed_lines={"mac/claude-code/p/s.jsonl.zst": "2"})
        b = dream.collect(cfg, state, PassThrough())
        self.assertEqual([m.text for m in b.messages], ["以后都用中文"])

    def test_corrupt_compressed_file_does_not_abort_collect(self):
        root = self.vault.parent / "archive-bad"
        good = root / "mac/claude-code/p/s.jsonl"
        good.parent.mkdir(parents=True)
        good.write_text(json.dumps(LINE, ensure_ascii=False) + "\n", encoding="utf-8")
        bad = root / "mac/claude-code/p/bad.jsonl.zst"
        bad.write_bytes(b"not zstd")
        cfg = dream.Config(vault=self.vault, archive=root,
                           agent_cmd=f"{sys.executable} {FAKE} {{prompt}}",
                           report_dir=self.vault.parent / "reports")
        b = dream.collect(cfg, dream.State(), PassThrough())
        self.assertEqual([m.text for m in b.messages], ["以后都用中文"])
        self.assertEqual(len(b.errors), 1)
        self.assertIn("bad.jsonl.zst", b.errors[0])

    def tearDown(self):
        os.environ.pop("FAKE_MODE", None)


if __name__ == "__main__":
    unittest.main()
