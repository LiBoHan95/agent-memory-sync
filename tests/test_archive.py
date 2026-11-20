from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from amem import archive


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                    encoding="utf-8")


@unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "archive"
        self.src = self.root / "mac" / "claude-code" / "p" / "s.jsonl"
        write_jsonl(self.src, [
            {"type": "user", "message": {"role": "user", "content": "用中文"},
             "timestamp": "2026-11-13T06:00:00Z", "sessionId": "s1", "cwd": "/w"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "bash"}]},
             "timestamp": "2026-11-13T06:00:01Z", "sessionId": "s1", "cwd": "/w"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "好的"}]},
             "timestamp": "2026-11-13T06:00:02Z", "sessionId": "s1", "cwd": "/w"},
        ])

    def tearDown(self):
        self.td.cleanup()

    def _decompress(self, path: Path) -> str:
        r = subprocess.run(["zstd", "-dc", str(path)], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8")

    def test_conversation_layer(self):
        stats = archive.build_conversation(self.root)
        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.messages, 2)
        out = self.root / "conversation" / "mac" / "claude-code" / "p" / "s.md.zst"
        self.assertTrue(out.exists())
        text = self._decompress(out)
        self.assertIn("用中文", text)
        self.assertIn("好的", text)
        self.assertNotIn("tool_use", text)

    def test_conversation_header_uses_first_nonempty_cwd(self):
        src = self.root / "mac" / "claude-code" / "p" / "c.jsonl"
        write_jsonl(src, [
            {"type": "user", "message": {"role": "user", "content": "一"},
             "timestamp": "t1", "sessionId": "s1", "cwd": ""},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "好"}]},
             "timestamp": "t2", "sessionId": "s1", "cwd": "/w"},
        ])
        archive.build_conversation(self.root)
        out = self.root / "conversation" / "mac" / "claude-code" / "p" / "c.md.zst"
        self.assertIn("- cwd: /w", self._decompress(out))

    def test_compact_and_state_migration(self):
        vault = Path(self.td.name) / "vault"
        vault.mkdir()
        state_path = vault / ".dream-state"
        old_rel = "mac/claude-code/p/s.jsonl"
        size = self.src.stat().st_size
        state_path.write_text(json.dumps({
            "version": 1, "last_run": "", "logs": {},
            "transcripts": {old_rel: size},
        }, ensure_ascii=False), encoding="utf-8")

        stats = archive.compact(self.root, older_than_days=0, delete_raw=True,
                                vault=vault)
        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.deleted, 1)
        self.assertFalse(self.src.exists())
        zst = self.src.with_name("s.jsonl.zst")
        self.assertTrue(zst.exists())
        self.assertIn("用中文", self._decompress(zst))

        state = json.loads(state_path.read_text(encoding="utf-8"))
        # 明文的旧偏移保留：rsync 重新上传明文时 dreaming 不会从 0 重放
        self.assertEqual(state["transcripts"][old_rel], size)
        self.assertEqual(state["transcripts"]["mac/claude-code/p/s.jsonl.zst"],
                         zst.stat().st_size)
        self.assertEqual(state["compressed_lines"]["mac/claude-code/p/s.jsonl.zst"], 3)

    def test_partial_state_migration_keeps_unread_tail(self):
        vault = Path(self.td.name) / "vault"
        vault.mkdir()
        state_path = vault / ".dream-state"
        old_rel = "mac/claude-code/p/s.jsonl"
        first_line = self.src.read_bytes().split(b"\n", 1)[0] + b"\n"
        state_path.write_text(json.dumps({
            "version": 1, "last_run": "", "logs": {},
            "transcripts": {old_rel: len(first_line)},
        }, ensure_ascii=False), encoding="utf-8")

        archive.compact(self.root, older_than_days=0, delete_raw=True, vault=vault)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        rel = "mac/claude-code/p/s.jsonl.zst"
        self.assertEqual(state["transcripts"][rel], 0)   # 强制继续处理尾部
        self.assertEqual(state["compressed_lines"][rel], 1)

    def test_compact_dry_run_keeps_raw(self):
        stats = archive.compact(self.root, older_than_days=0, delete_raw=True,
                                dry_run=True)
        self.assertEqual(stats.files, 1)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.src.with_name("s.jsonl.zst").exists())
        self.assertFalse((self.root / "conversation").exists())

    def test_old_conversation_layer_is_rebuilt(self):
        archive.build_conversation(self.root)
        out = self.root / "conversation" / "mac" / "claude-code" / "p" / "s.md.zst"
        data = b"# old format\n\n- session: s1\n- cwd: /w\n\n## t \xc2\xb7 user\n\n\xe6\xad\xa3\xe6\x96\x87\n"
        subprocess.run(["zstd", "-q", "-f", "-o", str(out), "-"], input=data,
                       capture_output=True, check=True)
        future = time.time() + 100
        os.utime(out, (future, future))
        archive.build_conversation(self.root)
        self.assertTrue(self._decompress(out).startswith(archive.CONVERSATION_MARKER))

    def test_compact_keeps_raw_if_changed_during_compression(self):
        real = archive._write_zstd

        def write_then_append(path, data, binary, level=19):
            real(path, data, binary, level)
            with open(self.src, "ab") as f:
                f.write(json.dumps({
                    "type": "user", "message": {"role": "user", "content": "追加"},
                    "timestamp": "t4", "sessionId": "s1", "cwd": "/w"}).encode() + b"\n")

        with mock.patch.object(archive, "_write_zstd", write_then_append):
            stats = archive.compact(self.root, older_than_days=0, delete_raw=True)
        self.assertTrue(self.src.exists())
        self.assertEqual(stats.deleted, 0)
        self.assertGreaterEqual(stats.errors, 1)

    def test_migration_resets_progress_when_content_replaced(self):
        vault = Path(self.td.name) / "vault"
        vault.mkdir()
        state_path = vault / ".dream-state"
        old_rel = "mac/claude-code/p/s.jsonl"
        zrel = "mac/claude-code/p/s.jsonl.zst"
        state_path.write_text(json.dumps({
            "version": 1, "last_run": "", "logs": {},
            "transcripts": {old_rel: self.src.stat().st_size},
        }, ensure_ascii=False), encoding="utf-8")
        archive.compact(self.root, older_than_days=0, delete_raw=True, vault=vault)
        zst = self.src.with_name("s.jsonl.zst")

        # 同一路径换成不同内容
        write_jsonl(self.src, [
            {"type": "user", "message": {"role": "user", "content": "新一"},
             "timestamp": "n1", "sessionId": "s9", "cwd": "/w"},
            {"type": "user", "message": {"role": "user", "content": "新二"},
             "timestamp": "n2", "sessionId": "s9", "cwd": "/w"},
        ])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["compressed_lines"][zrel] = 2
        state["transcripts"][zrel] = zst.stat().st_size
        state_path.write_text(json.dumps(state), encoding="utf-8")

        archive.compact(self.root, older_than_days=0, delete_raw=True, vault=vault)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["compressed_lines"][zrel], 0)
        self.assertEqual(state["transcripts"][zrel], 0)
        self.assertEqual(state["transcripts"][old_rel], 0)

    def test_stats(self):
        archive.build_conversation(self.root)
        s = archive.stats(self.root)
        self.assertGreater(s["total"], 0)
        self.assertGreater(s["conversation"], 0)

    def test_cli_conversation_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = archive.main(["conversation", "--archive", str(self.root), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out.getvalue())
        self.assertFalse((self.root / "conversation").exists())


if __name__ == "__main__":
    unittest.main()
