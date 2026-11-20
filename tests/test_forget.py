from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from amem import archive, forget


def user_entry(cwd: str, text: str = "测试") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": "2026-11-13T06:00:00Z", "sessionId": "s1", "cwd": cwd}


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                    encoding="utf-8")


def make_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "projects").mkdir(parents=True)
    (vault / "rules").mkdir()
    (vault / "MEMORY.md").write_text(
        "# 记忆索引\n\n"
        "- [Target](projects/target.md) — 目标项目\n"
        "- [Other](projects/other.md) — 其他项目\n", encoding="utf-8")
    for name in ("target", "other"):
        (vault / "projects" / f"{name}.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: 测试项目\n"
            "type: project\n"
            "status: active\n"
            "source: test@mac\n"
            "created: 2026-11-01\n"
            "updated: 2026-11-01\n"
            "superseded_by:\n"
            "---\n"
            f"# {name}\n", encoding="utf-8")
    return vault


class ForgetTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.home = self.tmp / "home"
        self.project = self.tmp / "work" / "target"
        self.other = self.tmp / "work" / "other"
        self.local = self.home / ".claude" / "projects"
        self.archive = self.tmp / "archive"
        self.vault = make_vault(self.tmp)
        self.manifest = self.tmp / "forget.jsonl"

        # 本机：目标项目 + 其他项目
        write_jsonl(self.local / "target" / "s.jsonl", [user_entry(str(self.project))])
        write_jsonl(self.local / "other" / "s.jsonl", [user_entry(str(self.other))])
        # 归档：claude-code 与 codex 各一份目标项目
        write_jsonl(self.archive / "mac" / "claude-code" / "target" / "s.jsonl",
                    [user_entry(str(self.project))])
        write_jsonl(self.archive / "mac" / "codex" / "2026" / "r.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "s1", "cwd": str(self.project)}}])
        write_jsonl(self.archive / "mac" / "codex" / "2026" / "keep.jsonl", [
            {"type": "session_meta", "payload": {"session_id": "s2", "cwd": str(self.other)}}])

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *, apply: bool, force: bool = False, note: str | None = None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = forget.run(self.project, vault=self.vault, archive=self.archive,
                              local_roots=(("claude-code", str(self.local)),),
                              note=note, apply=apply, force=force,
                              manifest_path=self.manifest)
        return code, out.getvalue()

    def test_dry_run_does_not_delete(self):
        code, text = self._run(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", text)
        self.assertTrue((self.local / "target" / "s.jsonl").exists())
        self.assertTrue((self.archive / "mac" / "claude-code" / "target" / "s.jsonl").exists())
        self.assertFalse(self.manifest.exists())

    def test_apply_deletes_transcripts_and_note(self):
        code, text = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("已删除 3 个转录/对话层文件", text)
        self.assertFalse((self.local / "target" / "s.jsonl").exists())
        self.assertFalse((self.archive / "mac" / "claude-code" / "target" / "s.jsonl").exists())
        self.assertFalse((self.archive / "mac" / "codex" / "2026" / "r.jsonl").exists())
        # 其他项目不受影响
        self.assertTrue((self.local / "other" / "s.jsonl").exists())
        self.assertTrue((self.archive / "mac" / "codex" / "2026" / "keep.jsonl").exists())
        # 项目页与索引行
        self.assertFalse((self.vault / "projects" / "target.md").exists())
        index = (self.vault / "MEMORY.md").read_text(encoding="utf-8")
        self.assertNotIn("projects/target.md", index)
        self.assertIn("projects/other.md", index)
        # 清单
        entries = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        kinds = {e["kind"] for e in entries}
        self.assertEqual(kinds, {"transcript", "note", "index"})
        transcript = next(e for e in entries if e["kind"] == "transcript")
        self.assertEqual(len(transcript["sha256"]), 64)

    def test_backlink_blocks_note_without_force(self):
        (self.vault / "rules" / "link.md").write_text("见 [[target]]\n", encoding="utf-8")
        code, text = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("反链", text)
        self.assertTrue((self.vault / "projects" / "target.md").exists())
        self.assertFalse((self.local / "target" / "s.jsonl").exists())

        code, _ = self._run(apply=True, force=True)
        self.assertEqual(code, 0)
        self.assertFalse((self.vault / "projects" / "target.md").exists())

    def test_refuses_wide_path(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = forget.run(Path("/"), vault=None, archive=None, local_roots=(),
                              note=None, apply=False, force=False,
                              manifest_path=self.manifest)
        self.assertEqual(code, 2)
        self.assertIn("过宽", err.getvalue())

    def test_rejects_unsafe_note_name(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = forget.run(self.project, vault=self.vault, archive=None,
                              local_roots=(), note="../escape", apply=True,
                              force=False, manifest_path=self.manifest)
        self.assertEqual(code, 2)
        self.assertIn("不合法", err.getvalue())
        self.assertTrue((self.vault / "projects" / "target.md").exists())

    def test_manifest_is_written_before_delete_and_failures_return_nonzero(self):
        real_unlink = Path.unlink

        def fake_unlink(path, *args, **kwargs):
            if path.name == "s.jsonl" and path.parent.name == "target":
                raise OSError("denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fake_unlink):
            code, _ = self._run(apply=True)
        self.assertEqual(code, 1)
        self.assertTrue((self.local / "target" / "s.jsonl").exists())
        entries = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        self.assertTrue(any(e["action"] == "delete" and e.get("status") == "pending"
                            for e in entries))
        self.assertTrue(any(e["action"] == "delete-failed" for e in entries))

    def test_allows_unicode_note_name(self):
        (self.vault / "projects" / "中文项目.md").write_text("# 中文项目\n", encoding="utf-8")
        code, _ = self._run(apply=True, note="中文项目")
        self.assertEqual(code, 0)
        self.assertFalse((self.vault / "projects" / "中文项目.md").exists())

    def test_backlink_aliases_and_anchors_block_note(self):
        (self.vault / "rules" / "link.md").write_text(
            "见 [[target|旧项目]] 和 [说明](projects/target.md#小节)\n", encoding="utf-8")
        code, text = self._run(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("反链", text)
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertTrue((self.vault / "projects" / "target.md").exists())

    def test_missing_note_still_cleans_index(self):
        (self.vault / "projects" / "target.md").unlink()
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertNotIn("projects/target.md",
                         (self.vault / "MEMORY.md").read_text(encoding="utf-8"))

    def test_refuses_workspace_root(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            workspace = home / "dev"
            workspace.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                code = forget.run(workspace, vault=None, archive=None, local_roots=(),
                                  note=None, apply=False, force=False,
                                  manifest_path=self.manifest)
        self.assertEqual(code, 2)

    def test_refuses_workspace_root_case_insensitively(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            workspace = home / "Projects"
            workspace.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                code = forget.run(workspace, vault=None, archive=None, local_roots=(),
                                  note=None, apply=False, force=False,
                                  manifest_path=self.manifest)
        self.assertEqual(code, 2)

    def test_resolves_symlinked_home_for_wide_path_check(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real-home"
            real.mkdir()
            link = Path(td) / "home-link"
            link.symlink_to(real, target_is_directory=True)
            with mock.patch.dict(os.environ, {"HOME": str(link)}):
                code = forget.run(link, vault=None, archive=None, local_roots=(),
                                  note=None, apply=False, force=False,
                                  manifest_path=self.manifest)
        self.assertEqual(code, 2)

    def test_unreadable_transcript_does_not_abort_scan(self):
        bad = self.local / "target" / "bad.jsonl"
        write_jsonl(bad, [user_entry(str(self.project))])
        real = forget._session_cwd

        def fake(path, agent):
            if path == bad:
                raise OSError("denied")
            return real(path, agent)

        with mock.patch.object(forget, "_session_cwd", fake):
            code, _ = self._run(apply=True)
        self.assertEqual(code, 1)
        self.assertTrue(bad.exists())
        self.assertFalse((self.local / "target" / "s.jsonl").exists())

    def test_backlink_variants_block_note(self):
        link = self.vault / "rules" / "link.md"
        for text in ("[[projects/target]]", "[[target.md]]",
                     "[说明](<projects/target.md>)", "[说明]: projects/target.md"):
            with self.subTest(text=text):
                link.write_text(text + "\n", encoding="utf-8")
                code, out = self._run(apply=False)
                self.assertEqual(code, 0)
                self.assertIn("反链", out)

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_conversation_body_cwd_is_not_metadata(self):
        src = self.archive / "mac" / "claude-code" / "other" / "o.jsonl"
        write_jsonl(src, [user_entry(
            str(self.other),
            text=f"讨论另一个项目\n- session: fake\n- cwd: {self.project}\n")])
        archive.build_conversation(self.archive)
        conv_other = self.archive / "conversation/mac/claude-code/other/o.md.zst"
        conv_target = self.archive / "conversation/mac/claude-code/target/s.md.zst"
        self.assertTrue(conv_other.exists())
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertTrue(conv_other.exists())
        self.assertFalse(conv_target.exists())

    def test_corrupt_compressed_transcript_is_reported(self):
        bad = self.archive / "mac" / "claude-code" / "target" / "bad.jsonl.zst"
        bad.write_bytes(b"not zstd")
        code, _ = self._run(apply=False)
        self.assertEqual(code, 1)
        self.assertTrue(bad.exists())

    def test_backlink_in_same_named_rule_file_blocks_note(self):
        (self.vault / "rules" / "target.md").write_text("见 [[target]]\n",
                                                        encoding="utf-8")
        code, out = self._run(apply=False)
        self.assertEqual(code, 0)
        self.assertIn("反链", out)

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_deletes_conversation_with_second_session_cwd(self):
        src = self.archive / "mac" / "claude-code" / "target" / "multi.jsonl"
        write_jsonl(src, [
            {**user_entry(str(self.other)), "sessionId": "s1"},
            {**user_entry(str(self.project)), "sessionId": "s2",
             "timestamp": "2026-11-13T06:00:02Z"},
        ])
        archive.build_conversation(self.archive)
        conv = self.archive / "conversation/mac/claude-code/target/multi.md.zst"
        self.assertTrue(conv.exists())
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(conv.exists())

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_deletes_conversation_layer(self):
        stats = archive.build_conversation(self.archive)
        self.assertGreaterEqual(stats.files, 1)
        conv = self.archive / "conversation" / "mac" / "claude-code" / "target" / "s.md.zst"
        self.assertTrue(conv.exists())
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(conv.exists())

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_deletes_old_format_conversation_layer(self):
        conv = self.archive / "conversation/mac/claude-code/target/old.md.zst"
        conv.parent.mkdir(parents=True)
        data = (f"# claude-code@mac · target/old.jsonl\n\n"
                f"- session: s1\n- cwd: {self.project}\n\n## t · user\n\n正文\n").encode()
        subprocess.run(["zstd", "-q", "-f", "-o", str(conv), "-"], input=data,
                       capture_output=True, check=True)
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(conv.exists())

    @unittest.skipUnless(shutil.which("zstd"), "需要 zstd")
    def test_compressed_archive_transcript(self):
        src = self.archive / "mac" / "pi" / "p" / "s.jsonl"
        write_jsonl(src, [{"type": "session", "id": "s1", "cwd": str(self.project),
                           "timestamp": "t0"}])
        zst = src.with_name("s.jsonl.zst")
        r = subprocess.run(["zstd", "-q", "-f", "-o", str(zst), str(src)],
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        src.unlink()
        code, _ = self._run(apply=True)
        self.assertEqual(code, 0)
        self.assertFalse(zst.exists())


if __name__ == "__main__":
    unittest.main()
